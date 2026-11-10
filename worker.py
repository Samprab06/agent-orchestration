"""Single-task Oracle worker with durable attempt and publication state."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import tempfile
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode

from github_app import InstallationCredentials
from orchestrator import approval_command, is_approved
from sandbox_runner import Sandbox, Stopped
from smoke import BanyanCode, git
from stage1_demo import APPROVER, REPOSITORY, DemoGitHub, git_auth_env, push_commit


BOT = "samprab06-agent-worker[bot]"
TERMINAL = {"review", "failed", "interrupted", "cancelled"}
RETRYABLE = TERMINAL - {"review"}
ERRORS = (OSError, ValueError, RuntimeError, subprocess.SubprocessError, URLError)


LOGGER = logging.getLogger("oracle-worker")


def log(message):
    print(message, flush=True)
    LOGGER.info(message)


class State:
    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "tasks.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (
                issue INTEGER PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS commands (
                issue INTEGER PRIMARY KEY, command TEXT NOT NULL
            );
        """)

    def get(self, issue):
        row = self.db.execute("SELECT data FROM tasks WHERE issue=?", (issue,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM tasks ORDER BY issue")]

    def save(self, task, **changes):
        task.update(changes)
        self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)",
                        (task["issue"], json.dumps(task)))

    def command(self, issue):
        row = self.db.execute("SELECT command FROM commands WHERE issue=?", (issue,)).fetchone()
        return row[0] if row else None

    def request(self, issue, command):
        self.db.execute("INSERT OR REPLACE INTO commands VALUES (?, ?)", (issue, command))

    def clear(self, issue):
        self.db.execute("DELETE FROM commands WHERE issue=?", (issue,))


def projects(path):
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError("Configure at least one repository in the allowlist")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"repository", "branch"}:
            raise ValueError("Each project must specify repository and branch")
        repo, branch = entry["repository"], entry["branch"]
        if not isinstance(repo, str) or not re.fullmatch(
            r"Samprab06/[a-z0-9][a-z0-9_.-]{0,99}", repo, re.IGNORECASE | re.ASCII
        ) or repo.lower().endswith((".git", ".")):
            raise ValueError("Repositories must use Samprab06/NAME")
        if not isinstance(branch, str) or not branch or branch.startswith("-") or (
            subprocess.run(["git", "check-ref-format", "refs/heads/" + branch],
                           capture_output=True, timeout=10).returncode
        ):
            raise ValueError("Invalid configured base branch")
        key = repo.lower()
        if key in result:
            raise ValueError(f"Duplicate repository: {repo}")
        result[key] = (repo, branch)
    return result


def project_paths(state, checkout, repository):
    if repository.lower() == REPOSITORY.lower():
        return state, checkout
    project = state.parent / "projects" / repository.lower()
    return project / "state", project / "checkout"


class Worker:
    def __init__(self, args, state, stop, repository, branch, checkout, root):
        self.args, self.state, self.stop = args, state, stop
        self.checkout = checkout
        self.root = root
        self.repository = repository
        self.branch = branch
        self.remote = f"https://github.com/{repository}.git"
        self.credentials = InstallationCredentials(
            repository, args.app_id, Path.home() / ".config/agent-orchestrator/github-app.pem"
        )
        self.github = DemoGitHub(self.credentials)
        self.runner = Sandbox(BanyanCode(str(args.banyancode.resolve(strict=True)),
                                         args.model), self.checkout, args.timeout)
        self.skipped = set()

    def log(self, message):
        log(f"{self.repository}: {message}")

    def git_remote(self, *arguments):
        return subprocess.check_output(
            ["git", "-C", str(self.checkout), *arguments], text=True, timeout=60,
            env=git_auth_env(self.credentials.token()), stderr=subprocess.STDOUT,
        ).strip()

    def prepare(self):
        token = self.credentials.token()
        if not self.checkout.exists():
            self.checkout.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.checkout.parent) as temp:
                target = Path(temp) / "repo"
                subprocess.run(
                    ["git", "clone", "--no-tags", "--branch", self.branch,
                     self.remote, str(target)],
                    env=git_auth_env(token), check=True, timeout=120,
                )
                target.rename(self.checkout)
        if (self.checkout.is_symlink() or not (self.checkout / ".git").is_dir()
                or git(self.checkout, "remote", "get-url", "origin").lower() != self.remote.lower()):
            raise ValueError("Project checkout does not match the configured repository")

    def cancelled(self, number):
        return self.stop.is_set() or self.state.command(number) == "cancel"

    def recover(self):
        for task in self.state.all():
            if task["phase"] == "running":
                phase = "cancelled" if self.state.command(task["issue"]) == "cancel" else "interrupted"
                self.finish(task, phase, "Worker stopped before the commit was verified. Worktree retained.")

    def finish(self, task, phase, error=None):
        self.state.save(task, phase=phase, error=error, reported=False, report_next=0)
        if phase == "cancelled":
            self.state.clear(task["issue"])
        self.log(f"Issue #{task['issue']} {phase}" + (f": {error}" if error else ""))

    def issue(self, number, allowed_status=()):
        issue = self.github.read(f"/issues/{number}")
        filtered = dict(issue, labels=[
            label for label in issue["labels"] if label["name"] not in allowed_status
        ])
        return issue if is_approved(
            filtered, self.github.comments(number), {APPROVER.casefold()}
        ) else None

    def label(self, number, name, add):
        if add:
            self.github.request("POST", f"/issues/{number}/labels", {"labels": [name]})
        else:
            try:
                self.github.request("DELETE", f"/issues/{number}/labels/{quote(name, safe='')}")
            except HTTPError as error:
                if error.code != 404:
                    raise

    def report(self, task):
        number, phase = task["issue"], task["phase"]
        target = "failed" if phase == "interrupted" else phase
        self.label(number, f"agent:{target}", True)
        for label in ("queued", "running", "failed", "cancelled"):
            if label != target:
                self.label(number, f"agent:{label}", False)
        marker = f"<!-- oracle-worker:{number}:{task['attempt']}:{phase} -->"
        if not any(
            marker in (comment.get("body") or "")
            and (comment.get("user") or {}).get("login", "").casefold() == BOT
            for comment in self.github.comments(number)
        ):
            if phase == "review":
                message = f"Oracle verified the task and opened {task['pr']}."
            else:
                message = f"Oracle marked this attempt **{phase}**. {task['error']} Operator logs identify the retained attempt."
            self.github.request("POST", f"/issues/{number}/comments", {"body": f"{message}\n\n{marker}"})
        self.state.save(task, reported=True)

    def attempt_dir(self, task):
        return self.root / f"issue-{task['issue']}" / f"attempt-{task['attempt']}"

    def prune_logs(self):
        logs = sorted(self.root.glob("issue-*/attempt-*/task.log"),
                      key=lambda path: path.stat().st_mtime, reverse=True)
        for path in logs[self.args.keep_logs:]:
            path.unlink(missing_ok=True)

    def edit(self, task, issue):
        number = task["issue"]
        self.state.save(task, phase="running", started=time.time(), base_branch=self.branch)
        self.log(f"Issue #{number} starting attempt {task['attempt']}")
        directory = self.attempt_dir(task)
        worktree, home, output = directory / "worktree", directory / "home", directory / "task.log"
        try:
            self.label(number, "agent:running", True)
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            home.mkdir(mode=0o700)
            if git(self.checkout, "status", "--porcelain"):
                raise ValueError("The worker checkout is dirty")
            self.git_remote("fetch", "--no-tags", self.remote, f"refs/heads/{self.branch}")
            base = git(self.checkout, "rev-parse", "FETCH_HEAD")
            git(self.checkout, "worktree", "add", "--detach", str(worktree), base)
            current = self.issue(number, ("agent:running",))
            if current is None or approval_command(current) != task["approval"]:
                raise ValueError("Approval changed before execution")
            prompt = (
                "Implement only the approved repository task below. Use file edits only. "
                "The worker handles verification, commits, and publication. Do not run commands.\n\n"
                f"Issue #{number}: {issue['title']}\n{issue.get('body') or ''}"
            )
            stop = lambda: self.cancelled(number)
            self.runner.edit(worktree, home, output, stop, prompt)
            if git(worktree, "rev-parse", "HEAD") != base:
                raise ValueError("The agent changed worktree HEAD")
            git(worktree, "add", "-A")
            paths = git(worktree, "diff", "--cached", "--name-only", "-z").strip("\0").split("\0")
            if paths == [""]:
                raise ValueError("The agent produced no tracked changes")
            if any(path == ".github" or path.startswith(".github/") for path in paths):
                raise ValueError("Worker tasks cannot change GitHub workflows or settings")
            git(worktree, "diff", "--cached", "--check")
            self.runner.verify(worktree, home, output, stop)
            if stop():
                raise Stopped("Cancellation or shutdown requested")
            if git(worktree, "diff", "--name-only") or git(
                worktree, "ls-files", "--others", "--exclude-standard"
            ):
                raise ValueError("Verification modified the proposed changes")
            git(worktree, "-c", "core.hooksPath=/dev/null", "-c", "user.name=oracle-agent-worker",
                "-c", "user.email=oracle-agent-worker@users.noreply.github.com",
                "commit", "-m", f"Implement approved issue #{number}")
            commit = git(worktree, "rev-parse", "HEAD")
            self.state.save(task, phase="publish", commit=commit, failures=0, next_try=0)
            self.log(f"Issue #{number} verified {commit}; task log {output}")
        except Stopped as error:
            phase = "cancelled" if self.state.command(number) == "cancel" else "interrupted"
            self.finish(task, phase, str(error) + ". Worktree retained if created.")
        except ERRORS as error:
            self.finish(task, "failed", f"{type(error).__name__}: {error}. Worktree retained if created.")
        self.prune_logs()

    def existing_pr(self, branch, commit, base):
        query = urlencode({"state": "all", "head": f"{APPROVER}:{branch}", "base": base})
        prs = self.github.read(f"/pulls?{query}")
        if len(prs) > 1 or (prs and prs[0]["head"]["sha"] != commit):
            raise ValueError("Remote pull request does not match the verified commit")
        return prs[0] if prs else None

    def publish(self, task):
        number, commit = task["issue"], task["commit"]
        branch = f"agent/issue-{number}"
        base = task.get("base_branch", "main")
        try:
            pr = self.existing_pr(branch, commit, base)
            if pr is None:
                if base != self.branch:
                    raise ValueError("Verified commit belongs to a different configured base branch")
                current = self.issue(number, ("agent:running",))
                if current is None or approval_command(current) != task["approval"]:
                    raise ValueError("Approval changed before publication")
                if self.cancelled(number):
                    raise Stopped("Cancellation or shutdown requested")
                remote = self.git_remote("ls-remote", self.remote, f"refs/heads/{branch}")
                if not remote:
                    push_commit(self.checkout, commit, branch, self.credentials.token(), self.repository)
                elif remote.split()[0] != commit:
                    raise ValueError("Remote branch exists at a different commit")
                current = self.issue(number, ("agent:running",))
                if current is None or approval_command(current) != task["approval"]:
                    raise ValueError("Approval changed before opening the draft PR")
                if self.cancelled(number):
                    raise Stopped("Cancellation or shutdown requested")
                pr = self.github.request("POST", "/pulls", {
                    "title": f"Implement approved issue #{number}", "head": branch, "base": base,
                    "body": f"Closes #{number}. Oracle verified commit `{commit}`.", "draft": True,
                })
            self.state.save(task, pr=pr["html_url"])
            self.finish(task, "review")
            worktree = self.attempt_dir(task) / "worktree"
            if worktree.exists():
                try:
                    if git(worktree, "status", "--porcelain", "--ignored"):
                        raise ValueError("Worktree still contains local files")
                    git(self.checkout, "worktree", "remove", str(worktree))
                except ERRORS:
                    self.log(f"Issue #{number}: kept worktree at {worktree}")
        except Stopped:
            if self.state.command(number) == "cancel":
                self.finish(task, "cancelled", "Operator cancelled publication. Verified commit retained.")
        except ValueError as error:
            self.finish(task, "failed", str(error))
        except ERRORS as error:
            failures = task["failures"] + 1
            self.state.save(task, failures=failures)
            if failures >= self.args.publish_attempts:
                self.finish(task, "failed", f"Publication exhausted {failures} attempts: {error}")
            else:
                wait = min(600, self.args.poll * 2 ** (failures - 1))
                self.state.save(task, next_try=time.time() + wait)
                self.log(f"Issue #{number}: publication retry {failures + 1} in {wait}s ({error})")

    def retry(self, task):
        number = task["issue"]
        # Failed tasks require the owner to requeue and approve their current content.
        current = self.issue(number, ("agent:failed", "agent:cancelled", "agent:running"))
        if current is None:
            self.log(f"Issue #{number}: retry requires an open, queued issue with valid approval")
            return
        digest = approval_command(current)
        if task.get("commit") and digest != task["approval"]:
            self.log(f"Issue #{number}: verified commit belongs to an older approval. Use a new issue.")
            self.state.clear(number)
            return
        if not task.get("commit") and shutil.disk_usage(self.root).free < 2 * 1024**3:
            self.log("Under 2 GiB free; pausing coding retry")
            return
        for name in ("failed", "cancelled"):
            self.label(number, "agent:" + name, False)
        self.state.clear(number)
        if task.get("commit"):
            self.state.save(task, phase="publish", failures=0, next_try=0)
        else:
            self.state.save(task, attempt=task["attempt"] + 1, approval=digest)
            self.edit(task, current)

    def tick(self):
        for task in self.state.all():
            if self.stop.is_set():
                return
            number, phase = task["issue"], task["phase"]
            if phase in TERMINAL and not task.get("reported") and time.time() >= task.get("report_next", 0):
                try:
                    self.report(task)
                except ERRORS as error:
                    self.state.save(task, report_next=time.time() + 300)
                    self.log(f"Issue #{number}: status reporting will retry: {error}")
            if phase in RETRYABLE and task.get("reported") and self.state.command(number) == "retry":
                self.retry(task)
                return
            if phase == "publish" and (
                self.state.command(number) == "cancel" or time.time() >= task["next_try"]
            ):
                self.publish(task)
                return
        # Retain uncertain work, but stop admitting new attempts if disk runs low.
        if shutil.disk_usage(self.root).free < 2 * 1024**3:
            self.log("Under 2 GiB free; pausing new tasks")
            return
        for page in range(1, 11):
            if self.stop.is_set():
                return
            issues = self.github.read(
                f"/issues?state=open&labels=agent%3Aqueued&sort=created&direction=asc&per_page=50&page={page}"
            )
            for item in issues:
                if self.stop.is_set():
                    return
                number = item["number"]
                if "pull_request" in item or self.state.get(number):
                    continue
                issue = self.issue(number)
                if issue is None:
                    if number not in self.skipped:
                        self.log(f"Issue #{number} skipped: no current approval from {APPROVER}")
                        self.skipped.add(number)
                    continue
                self.edit({"issue": number, "attempt": 1, "approval": approval_command(issue)}, issue)
                task = self.state.get(number)
                if task["phase"] == "publish" and not self.stop.is_set():
                    self.publish(task)
                if task["phase"] in TERMINAL and not self.stop.is_set():
                    self.report(task)
                return
            if len(issues) < 50:
                return

def run_workers(workers, stop, poll):
    for worker in workers:
        worker.recover()
    log(f"Worker online; polling {len(workers)} configured repositories every {poll}s")
    failures = {}
    due = {}
    while not stop.is_set():
        for worker in workers:
            if stop.is_set():
                break
            name = worker.repository
            if time.monotonic() < due.get(name, 0):
                continue
            try:
                worker.prepare()
                worker.tick()
                failures[name] = 0
            except ERRORS as error:
                failures[name] = min(failures.get(name, 0) + 1, 5)
                delay = min(600, poll * 2 ** failures[name])
                due[name] = time.monotonic() + delay
                log(f"{name}: poll failed: {error}; retry in {delay}s")
        stop.wait(poll)
    log("Worker stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repository", help="Select a configured repository for status or controls")
    commands = parser.add_subparsers(dest="action", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--checkout", type=Path, required=True)
    serve.add_argument("--banyancode", type=Path, required=True)
    serve.add_argument("--app-id", type=int, required=True)
    serve.add_argument("--model", default="opencode/beam-free")
    serve.add_argument("--poll", type=int, choices=range(30, 61), default=45, metavar="30ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Å“60")
    serve.add_argument("--timeout", type=int, default=240)
    serve.add_argument("--publish-attempts", type=int, default=3)
    serve.add_argument("--keep-logs", type=int, default=20)
    commands.add_parser("status")
    for action in ("cancel", "retry"):
        commands.add_parser(action).add_argument("issue", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        configured = projects(args.config)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    if args.repository and args.repository.lower() not in configured:
        parser.error("Repository is outside the configured allowlist")
    args.state = args.state.resolve()
    checkout = getattr(args, "checkout", args.state.parent / "checkout").absolute()
    if args.action == "status":
        for key, (repo, _) in configured.items():
            if args.repository and args.repository.lower() != key:
                continue
            root, _ = project_paths(args.state, checkout, repo)
            for task in State(root).all():
                print(json.dumps({"repository": repo, **task}))
        return 0
    if args.action in ("cancel", "retry"):
        if not args.repository and len(configured) != 1:
            parser.error("--repository is required when more than one repository is configured")
        repo, _ = configured[args.repository.lower()] if args.repository else next(iter(configured.values()))
        root, _ = project_paths(args.state, checkout, repo)
        state = State(root)
        task = state.get(args.issue)
        phases = {"running", "publish"} if args.action == "cancel" else RETRYABLE
        if task is None or task["phase"] not in phases:
            parser.error(f"Issue #{args.issue} is not eligible for {args.action}")
        state.request(args.issue, args.action)
        log(f"Requested {args.action} for issue #{args.issue}")
        return 0
    if not (1 <= args.publish_attempts <= 10 and 10 <= args.timeout <= 900 and args.keep_logs > 0):
        parser.error("Use 1ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Å“10 publication attempts, a 10ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Å“900s timeout and at least one log")
    import fcntl
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (args.state / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handler = RotatingFileHandler(
            args.state / "worker.log", maxBytes=2 * 1024 * 1024, backupCount=4,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        LOGGER.setLevel(logging.INFO)
        LOGGER.addHandler(handler)
        workers = []
        for repo, branch in configured.values():
            root, path = project_paths(args.state, checkout, repo)
            workers.append(Worker(args, State(root), stop, repo, branch, path, root))
        run_workers(workers, stop, args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
