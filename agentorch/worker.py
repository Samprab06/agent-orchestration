"""The always-on worker: runs approved tasks one at a time and reports back.

For each configured repository the worker keeps a clean clone and a SQLite
record of every attempt. A task goes through these phases:

``running``  the agent is editing a fresh worktree, then verification runs
``publish``  a verified commit exists; push the branch and open a draft PR
``review``   the draft PR is open (terminal)
``failed`` / ``interrupted`` / ``cancelled``  (terminal, retryable)

Every phase change is recorded before acting on GitHub, so a restart never
repeats a push or opens a second PR, and an interrupted coding attempt is
never restarted on its own. Reporting (labels, issue comment, board post and
phone notification) retries until it succeeds.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from agentorch import agents, labels, tasks
from agentorch.board import Board, mentions
from agentorch.config import Config, Repository
from agentorch.github import NETWORK_ERRORS, GitHub, GitHubError, git_auth_env, provider_for
from agentorch.notify import Dispatcher, build as build_notifiers
from agentorch.sandbox import Runner, Stopped

LOG = logging.getLogger("agentorch.worker")
TERMINAL = {"review", "failed", "interrupted", "cancelled"}
RETRYABLE = TERMINAL
TRANSIENT = (GitHubError, OSError, subprocess.SubprocessError) + NETWORK_ERRORS
COLLABORATORS = {"OWNER", "MEMBER", "COLLABORATOR"}


def log(message: str) -> None:
    LOG.info(message)


def git(cwd: Path, *args: str, env: Optional[Dict[str, str]] = None, timeout: int = 120) -> str:
    return subprocess.check_output(
        ["git", "-C", str(cwd), *args], text=True, stderr=subprocess.STDOUT, timeout=timeout,
        env=env if env is not None else {**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).strip()


class State:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(directory / "tasks.sqlite3"), isolation_level=None)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (issue INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def get(self, issue: int) -> Optional[Dict[str, Any]]:
        row = self.db.execute("SELECT data FROM tasks WHERE issue=?", (issue,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self) -> List[Dict[str, Any]]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM tasks ORDER BY issue")]

    def save(self, task: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
        task.update(changes)
        self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (task["issue"], json.dumps(task)))
        return task

    def flag(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM kv WHERE key=?", (key,)).fetchone() is not None

    def set_flag(self, key: str, value: str = "1") -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))

    def close(self) -> None:
        self.db.close()


class RepoWorker:
    def __init__(self, config: Config, repo: Repository, stop: threading.Event,
                 notify: Callable[[str, str], Any], github: Optional[GitHub] = None,
                 runner: Optional[Runner] = None):
        self.config = config
        self.repo = repo
        self.stop = stop
        self.notify = notify
        self.root = config.state_dir / "repos" / repo.key.replace("/", "__")
        self.checkout = self.root / "checkout"
        self.state = State(self.root)
        self.github = github or GitHub(repo.name, provider_for(config, repo.name))
        self.runner = runner or Runner(config.sandbox_type, config.sandbox_binds)
        self.remote = f"https://github.com/{repo.name}.git"
        self._closed_checked = 0.0
        self._closed = False

    # helpers ------------------------------------------------------------------
    def log(self, message: str) -> None:
        log(f"{self.repo.name}: {message}")

    def self_login(self) -> str:
        return self.github.login()

    def git_env(self) -> Dict[str, str]:
        return git_auth_env(self.github.credentials.token(),
                            {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home())})

    def say(self, event: str, message: str) -> None:
        self.notify(event, f"{self.repo.name} {message}")

    def board(self) -> Optional[Board]:
        if not self.repo.board_issue:
            return None
        return Board(self.github, self.repo.board_issue, login=self.self_login(), cursor="worker",
                     state_dir=self.root / "board")

    def attempt_dir(self, task: Dict[str, Any]) -> Path:
        return self.root / "attempts" / f"issue-{task['issue']}" / f"attempt-{task['attempt']}"

    # lifecycle ------------------------------------------------------------------
    def prepare(self) -> None:
        if not self.checkout.exists():
            self.checkout.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=str(self.checkout.parent)) as temp:
                target = Path(temp) / "repo"
                subprocess.run(["git", "clone", "--no-tags", "--branch", self.repo.branch, self.remote,
                                str(target)], env=self.git_env(), check=True, timeout=600,
                               capture_output=True)
                target.rename(self.checkout)
            self.log(f"cloned into {self.checkout}")
        if self.checkout.is_symlink() or not (self.checkout / ".git").is_dir():
            raise ValueError(f"{self.checkout} is not a git checkout")
        if git(self.checkout, "remote", "get-url", "origin").lower() != self.remote.lower():
            raise ValueError("Checkout origin does not match the configured repository")

    def recover(self) -> None:
        for task in self.state.all():
            if task["phase"] == "running":
                self.finish(task, "interrupted", "The worker stopped before the change was verified. "
                            "The worktree is kept on the server.")

    def finish(self, task: Dict[str, Any], phase: str, error: Optional[str] = None) -> None:
        self.state.save(task, phase=phase, error=error, reported=False, report_next=0,
                        finished=time.time())
        self.log(f"#{task['issue']} {phase}" + (f": {error}" if error else ""))

    # reporting -----------------------------------------------------------------
    def report(self, task: Dict[str, Any]) -> None:
        number, phase = task["issue"], task["phase"]
        target = {"interrupted": labels.FAILED, "review": labels.REVIEW,
                  "failed": labels.FAILED, "cancelled": labels.CANCELLED}[phase]
        self.github.add_labels(number, target)
        for name in (labels.QUEUED, labels.RUNNING, labels.FAILED, labels.CANCELLED, labels.REVIEW):
            if name != target:
                self.github.remove_label(number, name)
        marker = f"<!-- agentorch:{number}:{task['attempt']}:{phase} -->"
        me = self.self_login().casefold()
        if not any(marker in (c.get("body") or "") and (c.get("user") or {}).get("login", "").casefold() == me
                   for c in self.github.comments(number)):
            if phase == "review":
                text = f"Draft PR ready: {task['pr']}"
            else:
                text = f"Attempt {task['attempt']} **{phase}**: {task.get('error') or 'no detail'}"
                text += "\n\nAdd `agent:queued` again to retry."
            self.github.comment(number, f"{text}\n\n{marker}")
        requester = task.get("requester") or ""
        mention = f" @{requester}" if requester and requester.casefold() != me else ""
        board = self.board()
        key = f"board:{number}:{task['attempt']}:{phase}"
        if board is not None and not self.state.flag(key):
            if phase == "review":
                text = f"Draft PR for #{number} ({task['title']}) is ready: {task['pr']}{mention}"
            else:
                text = f"#{number} ({task['title']}) {phase}: {task.get('error') or ''}{mention}"
            board.post(text.strip(), signature=self.config.signature)
            self.state.set_flag(key)
        if phase == "review":
            self.say("task_review", f"#{number} {task['title']}: draft PR {task['pr']}")
        elif phase == "cancelled":
            self.say("task_cancelled", f"#{number} {task['title']}: cancelled")
        else:
            self.say("task_failed", f"#{number} {task['title']}: {phase}. {task.get('error') or ''}".strip())
        self.state.save(task, reported=True)

    # cancellation ----------------------------------------------------------------
    def stopper(self, number: int) -> Callable[[], bool]:
        self._closed_checked, self._closed = 0.0, False

        def stop() -> bool:
            if self.stop.is_set():
                return True
            if not self._closed and time.monotonic() - self._closed_checked > 15:
                self._closed_checked = time.monotonic()
                try:
                    self._closed = self.github.issue(number).get("state") != "open"
                except TRANSIENT:
                    pass
            return self._closed
        return stop

    def stopped_phase(self) -> str:
        return "cancelled" if self._closed else "interrupted"

    # running -------------------------------------------------------------------
    def still_approved(self, task: Dict[str, Any]) -> Dict[str, Any]:
        issue = self.github.issue(task["issue"])
        if issue.get("state") != "open":
            self._closed = True
            raise Stopped("The issue was closed")
        if tasks.fingerprint(issue) != task["fingerprint"]:
            raise ValueError("The issue was edited after approval; approve the new version")
        return issue

    def run(self, issue: Dict[str, Any], approval: tasks.Approval) -> Dict[str, Any]:
        number = issue["number"]
        previous = self.state.get(number) or {}
        task = {
            "issue": number, "attempt": previous.get("attempt", 0) + 1, "title": issue["title"],
            "fingerprint": tasks.fingerprint(issue), "requester": tasks.requested_by(issue, self.self_login()),
            "approval": approval.describe(), "base_branch": self.repo.branch, "failures": 0,
            "next_try": 0, "started": time.time(), "agent": self.repo.agent.get("type"),
        }
        self.state.save(task, phase="running")
        self.log(f"#{number} attempt {task['attempt']} started ({approval.describe()})")
        stop = self.stopper(number)
        directory = self.attempt_dir(task)
        worktree, home, output = directory / "worktree", directory / "home", directory / "task.log"
        try:
            self.github.add_labels(number, labels.RUNNING)
            for name in (labels.QUEUED, labels.FAILED, labels.CANCELLED, labels.REVIEW):
                self.github.remove_label(number, name)
            self.say("task_started", f"#{number} {issue['title']}: started (attempt {task['attempt']})")
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            home.mkdir(mode=0o700)
            if git(self.checkout, "status", "--porcelain"):
                raise ValueError("The worker's clone has local changes")
            git(self.checkout, "fetch", "--no-tags", self.remote, f"refs/heads/{self.repo.branch}",
                env=self.git_env())
            base = git(self.checkout, "rev-parse", "FETCH_HEAD")
            git(self.checkout, "worktree", "add", "--detach", str(worktree), base)
            self.state.save(task, base=base)
            issue = self.still_approved(task)
            agent = agents.build(self.repo.agent, sandboxed=self.runner.sandboxed)
            agent.populate_home(home)
            prompt = agents.task_prompt(self.repo.name, number, issue["title"], issue.get("body") or "",
                                        self.repo.prompt_preamble, self.repo.verify)
            git_dirs = [worktree / ".git", self.checkout / ".git"]
            self.runner.run(agent.argv(prompt), worktree, home, output, stop, agent.timeout,
                            network=True, env=agent.env(), git_dirs=git_dirs,
                            config_files=agent.config_files())
            if git(worktree, "rev-parse", "HEAD") != base:
                raise ValueError("The agent moved HEAD; only file edits are allowed")
            git(worktree, "add", "-A")
            changed = [p for p in git(worktree, "diff", "--cached", "--name-only", "-z").split("\0") if p]
            if not changed:
                raise ValueError("The agent made no changes")
            if not self.repo.allow_workflow_changes and any(p == ".github" or p.startswith(".github/")
                                                            for p in changed):
                raise ValueError("Tasks may not change .github/ (set allow_workflow_changes to permit)")
            git(worktree, "diff", "--cached", "--check")
            if self.repo.verify:
                self.runner.run(list(self.repo.verify), worktree, home, output, stop,
                                self.config.verify_timeout, network=False, git_dirs=git_dirs)
                if git(worktree, "diff", "--name-only") or git(
                        worktree, "ls-files", "--others", "--exclude-standard"):
                    raise ValueError("Verification changed or created files; commit build outputs to "
                                     ".gitignore or make the command side-effect free")
            if stop():
                raise Stopped("Cancelled")
            git(worktree, "-c", "core.hooksPath=" + os.devnull, "-c", f"user.name={self.config.git_name}",
                "-c", f"user.email={self.config.git_email}", "commit", "-q", "-m",
                f"{issue['title']}\n\nCloses #{number}. {approval.describe()}.")
            commit = git(worktree, "rev-parse", "HEAD")
            self.state.save(task, phase="publish", commit=commit, files=len(changed))
            self.log(f"#{number} verified {commit[:12]} ({len(changed)} files)")
        except Stopped as error:
            self.finish(task, self.stopped_phase(), f"{error}. Worktree kept on the server.")
        except (ValueError, RuntimeError, TimeoutError) + TRANSIENT as error:
            self.finish(task, "failed", f"{type(error).__name__}: {error}")
        self.prune_logs()
        return task

    def prune_logs(self) -> None:
        logs = sorted(self.root.glob("attempts/issue-*/attempt-*/task.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for path in logs[self.config.keep_logs:]:
            path.unlink()

    # publishing ------------------------------------------------------------------
    def branch_name(self, task: Dict[str, Any]) -> str:
        suffix = "" if task["attempt"] == 1 else f"-attempt-{task['attempt']}"
        return f"agent/issue-{task['issue']}{suffix}"

    def existing_pr(self, branch: str, commit: str) -> Optional[Dict[str, Any]]:
        owner = self.repo.name.split("/")[0]
        prs = self.github.get("/pulls", state="all", head=f"{owner}:{branch}")
        if len(prs) > 1 or (prs and prs[0]["head"].get("sha") not in (None, commit)):
            raise ValueError(f"A pull request for {branch} exists at a different commit")
        return prs[0] if prs else None

    def publish(self, task: Dict[str, Any]) -> None:
        number, commit = task["issue"], task["commit"]
        branch = self.branch_name(task)
        try:
            pr = self.existing_pr(branch, commit)
            if pr is None:
                self.still_approved(task)
                remote = git(self.checkout, "ls-remote", self.remote, f"refs/heads/{branch}", env=self.git_env())
                if not remote:
                    git(self.checkout, "push", f"--force-with-lease=refs/heads/{branch}:", self.remote,
                        f"{commit}:refs/heads/{branch}", env=self.git_env())
                elif remote.split()[0] != commit:
                    raise ValueError(f"Remote branch {branch} exists at a different commit")
                body = (f"Closes #{number}.\n\nAgent: {task.get('agent')}. {task.get('approval', '')}. "
                        f"Verified commit `{commit[:12]}`"
                        + (f" with `{' '.join(self.repo.verify)}`." if self.repo.verify else "."))
                if task.get("requester"):
                    body += f"\n\nRequested by @{task['requester']}."
                pr = self.github.request("POST", "/pulls", {
                    "title": task["title"], "head": branch, "base": task["base_branch"],
                    "body": body, "draft": True,
                })
            self.state.save(task, pr=pr["html_url"], branch=branch)
            self.finish(task, "review")
            worktree = self.attempt_dir(task) / "worktree"
            if worktree.exists():
                try:
                    if git(worktree, "status", "--porcelain", "--ignored"):
                        raise ValueError("untracked files remain")
                    git(self.checkout, "worktree", "remove", str(worktree))
                except (ValueError,) + TRANSIENT:
                    self.log(f"#{number}: kept worktree {worktree}")
        except Stopped:
            self.finish(task, "cancelled", "The issue was closed before publishing. Commit kept on the server.")
        except ValueError as error:
            self.finish(task, "failed", str(error))
        except TRANSIENT as error:
            failures = task.get("failures", 0) + 1
            if failures >= self.config.publish_attempts:
                self.finish(task, "failed", f"Publishing failed {failures} times: {error}")
            else:
                wait = min(600, self.config.poll * 2 ** (failures - 1))
                self.state.save(task, failures=failures, next_try=time.time() + wait)
                self.log(f"#{number}: publish retry in {wait}s ({error})")

    # board ---------------------------------------------------------------------
    def watch_board(self) -> None:
        board = self.board()
        if board is None or not self.config.watch_board:
            return
        if board.cursor() is None:
            board.skip_to_latest()  # never act on history
            return
        for post in board.unread():
            if self.stop.is_set():
                return
            request = tasks.parse_board_task(post.body, self.config.task_trigger)
            if request and post.association in COLLABORATORS:
                issue = tasks.create_task(self.github, request["title"], request["body"],
                                          requester=post.login, source=post.url)
                self.log(f"board: @{post.login} queued #{issue['number']}")
                approval = tasks.find_approval(self.github, issue, self.config.approvers,
                                               self.repo.auto_approve, self.self_login())
                status = "Queued" if approval else "Queued and waiting for approval:"
                board.post(f"{status} #{issue['number']} {issue['html_url']} @{post.login}",
                           signature=self.config.signature)
            elif request:
                self.log(f"board: ignored /task from non-collaborator @{post.login}")
            watched = [m for m in self.config.notify_mentions if mentions(post.body, [m])]
            if watched or mentions(post.body, ["all"]):
                snippet = post.body if len(post.body) <= 400 else post.body[:400] + "..."
                self.say("board_mention", f"board, @{post.login}: {snippet}\n{post.url}")

    # main tick -----------------------------------------------------------------
    def tick(self) -> None:
        for task in self.state.all():
            if self.stop.is_set():
                return
            if task["phase"] in TERMINAL and not task.get("reported") \
                    and time.time() >= task.get("report_next", 0):
                try:
                    self.report(task)
                except TRANSIENT as error:
                    self.state.save(task, report_next=time.time() + 120)
                    self.log(f"#{task['issue']}: reporting will retry: {error}")
            if task["phase"] == "publish" and time.time() >= task.get("next_try", 0):
                self.publish(task)
        try:
            self.watch_board()
        except TRANSIENT as error:
            self.log(f"board watch failed: {error}")
        if self.stop.is_set():
            return
        if shutil.disk_usage(str(self.root)).free < self.config.min_free_bytes:
            self.log("low disk space; not starting new tasks")
            return
        for issue in tasks.queued_issues(self.github):
            if self.stop.is_set():
                return
            number = issue["number"]
            known = self.state.get(number)
            if known and known["phase"] not in RETRYABLE:
                continue
            if known and not known.get("reported"):
                continue  # finish reporting the last attempt first
            approval = tasks.find_approval(self.github, issue, self.config.approvers,
                                           self.repo.auto_approve, self.self_login())
            if approval is None:
                key = f"waiting:{number}:{tasks.fingerprint(issue)}"
                if not self.state.flag(key):
                    self.state.set_flag(key)
                    who = tasks.requested_by(issue, self.self_login())
                    self.say("task_waiting", f"#{number} {issue['title']} (from @{who}) needs approval. "
                             f"Reply \"approve {number}\". {issue['html_url']}")
                continue
            task = self.run(issue, approval)
            if task["phase"] == "publish" and not self.stop.is_set():
                self.publish(task)
            if task["phase"] in TERMINAL and not self.stop.is_set():
                try:
                    self.report(task)
                except TRANSIENT as error:
                    self.state.save(task, report_next=time.time() + 120)
                    self.log(f"#{number}: reporting will retry: {error}")
            return  # one task per tick keeps every repository responsive


def configure_logging(state_dir: Path, verbose: bool = True) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = logging.getLogger("agentorch")
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = RotatingFileHandler(str(state_dir / "worker.log"), maxBytes=2 * 1024 * 1024,
                                      backupCount=4, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        root.addHandler(handler)
        if verbose:
            stream = logging.StreamHandler()
            stream.setFormatter(logging.Formatter("%(message)s"))
            root.addHandler(stream)


def _mtime(path: Optional[Path]) -> float:
    try:
        return path.stat().st_mtime if path else 0.0
    except OSError:
        return 0.0


def serve(config: Config, once: bool = False) -> int:
    """Poll every configured repository. Reloads the config file when it changes."""
    import fcntl
    import signal as signals

    configure_logging(config.state_dir)
    stop = threading.Event()
    signals.signal(signals.SIGTERM, lambda *_: stop.set())
    signals.signal(signals.SIGINT, lambda *_: stop.set())
    with (config.state_dir / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another worker is already running with this state directory") from None
        workers: List[RepoWorker] = []
        loaded = -1.0

        def load(new: Config) -> List[RepoWorker]:
            for worker in workers:
                worker.state.close()
            notify = Dispatcher(build_notifiers(new.notify), new.notify_events)
            result = [RepoWorker(new, repo, stop, notify) for repo in new.repositories.values()]
            for worker in result:
                worker.recover()
            names = ", ".join(r.name for r in new.repositories.values()) or "none yet"
            log(f"worker online: repositories: {names}; polling every {new.poll}s")
            return result

        workers, loaded = load(config), _mtime(config.path)
        failures: Dict[str, int] = {}
        due: Dict[str, float] = {}
        while not stop.is_set():
            current = _mtime(config.path)
            if config.path and current != loaded:
                loaded = current
                try:
                    fresh = Config.load(config.path)
                except (ValueError, OSError) as error:
                    log(f"config change ignored: {error}")
                else:
                    if fresh.state_dir != config.state_dir:
                        log("config change: worker.state_dir needs a restart; keeping the old one")
                        fresh.state_dir = config.state_dir
                    config = fresh
                    workers = load(config)
                    failures.clear()
                    due.clear()
            for worker in workers:
                name = worker.repo.name
                if stop.is_set() or time.monotonic() < due.get(name, 0):
                    continue
                try:
                    worker.prepare()
                    worker.tick()
                    if failures.get(name, 0) >= 3:
                        worker.say("worker_error", "recovered")
                    failures[name] = 0
                except (ValueError, RuntimeError) + TRANSIENT as error:
                    failures[name] = min(failures.get(name, 0) + 1, 6)
                    delay = min(900, config.poll * 2 ** failures[name])
                    due[name] = time.monotonic() + delay
                    log(f"{name}: poll failed: {error}; retry in {delay}s")
                    if failures[name] == 3:
                        worker.say("worker_error", f"keeps failing: {error}")
            if once:
                break
            stop.wait(config.poll)
        for worker in workers:
            worker.state.close()
    log("worker stopped")
    return 0


def status(config: Config, include_queue: bool = True) -> List[Dict[str, Any]]:
    """Local attempts plus the GitHub queue for every configured repository."""
    rows: List[Dict[str, Any]] = []
    for repo in config.repositories.values():
        root = config.state_dir / "repos" / repo.key.replace("/", "__")
        local: Dict[int, Dict[str, Any]] = {}
        if (root / "tasks.sqlite3").exists():
            state = State(root)
            local = {t["issue"]: t for t in state.all()}
            state.close()
        entry: Dict[str, Any] = {"repository": repo.name, "tasks": list(local.values()), "queue": []}
        if include_queue:
            try:
                github = GitHub(repo.name, provider_for(config, repo.name))
                for issue in tasks.queued_issues(github):
                    known = local.get(issue["number"])
                    if known and known["phase"] not in RETRYABLE:
                        continue
                    approval = tasks.find_approval(github, issue, config.approvers, repo.auto_approve,
                                                   github.login())
                    entry["queue"].append({"issue": issue["number"], "title": issue["title"],
                                           "approved": approval.describe() if approval else None,
                                           "url": issue["html_url"]})
            except (ValueError,) + TRANSIENT as error:
                entry["error"] = str(error)
        rows.append(entry)
    return rows


def format_status(rows: List[Dict[str, Any]], recent: int = 5) -> str:
    lines: List[str] = []
    for entry in rows:
        lines.append(entry["repository"])
        start = len(lines)
        active = [t for t in entry["tasks"] if t["phase"] in ("running", "publish")]
        for t in active:
            lines.append(f"  RUNNING #{t['issue']} {t['title']} ({t['phase']}, attempt {t['attempt']})")
        for q in entry["queue"]:
            state = "ready" if q["approved"] else "needs approval"
            lines.append(f"  QUEUED  #{q['issue']} {q['title']} ({state})")
        done = sorted((t for t in entry["tasks"] if t["phase"] in TERMINAL),
                      key=lambda t: t.get("finished", 0), reverse=True)[:recent]
        for t in done:
            extra = t.get("pr") if t["phase"] == "review" else (t.get("error") or "")[:120]
            lines.append(f"  {t['phase'].upper():8}#{t['issue']} {t['title']} {extra}".rstrip())
        if entry.get("error"):
            lines.append(f"  (GitHub unavailable: {entry['error']})")
        if len(lines) == start:
            lines.append("  idle, nothing queued")
    return "\n".join(lines)
