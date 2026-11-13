"""Linear-driven orchestrator: runs server sub-agents on Linear tickets in parallel.

Each sub-agent in ``subagents`` owns the Linear label ``agent:<name>``. A ticket
in Todo with that label and no unfinished blockers is claimed (moved to In
Progress), run in its own sandboxed worktree with the sub-agent's CLI and model,
verified, pushed to a branch named after the ticket, and opened as a draft PR
that says ``Fixes <ID>`` so Linear links it. The ticket then moves to In
Review, or back to Backlog with the error if the attempt failed.

Up to ``worker.max_parallel`` tickets run at once. Git operations on the shared
clone are serialised; the agents themselves run concurrently.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agentorch import agents
from agentorch.config import Config, Repository
from agentorch.github import NETWORK_ERRORS, GitHub, GitHubError, git_auth_env, provider_for
from agentorch.linear import (AGENT_PREFIX, Linear, LinearBoard, LinearError, blockers, branch_name,
                              from_settings)
from agentorch.sandbox import Runner, Stopped
from agentorch.worker import LOG, git

TERMINAL = {"review", "failed", "cancelled"}
TRANSIENT = (GitHubError, LinearError, OSError, subprocess.SubprocessError) + NETWORK_ERRORS


def log(message: str) -> None:
    LOG.info(message)


class TicketState:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(directory / "tickets.sqlite3"), isolation_level=None,
                                  check_same_thread=False)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)

    def get(self, ident: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT data FROM tickets WHERE id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT data FROM tickets").fetchall()
        return [json.loads(r[0]) for r in rows]

    def save(self, task: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
        task.update(changes)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO tickets VALUES (?, ?)", (task["id"], json.dumps(task)))
        return task

    def close(self) -> None:
        self.db.close()


class Checkout:
    """One clean clone per code repository, shared by all sub-agents."""

    def __init__(self, root: Path, repo: Repository, github: GitHub):
        self.repo = repo
        self.github = github
        self.path = root / "repos" / repo.key.replace("/", "__") / "checkout"
        self.remote = f"https://github.com/{repo.name}.git"
        self.lock = threading.Lock()

    def env(self) -> Dict[str, str]:
        return git_auth_env(self.github.credentials.token(),
                            {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home())})

    def prepare(self) -> None:
        with self.lock:
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=str(self.path.parent)) as temp:
                    target = Path(temp) / "repo"
                    subprocess.run(["git", "clone", "--no-tags", "--branch", self.repo.branch, self.remote,
                                    str(target)], env=self.env(), check=True, timeout=600, capture_output=True)
                    target.rename(self.path)
            if git(self.path, "remote", "get-url", "origin").lower() != self.remote.lower():
                raise ValueError("Checkout origin does not match the configured repository")

    def new_worktree(self, worktree: Path) -> str:
        with self.lock:
            git(self.path, "fetch", "--no-tags", self.remote, f"refs/heads/{self.repo.branch}", env=self.env())
            base = git(self.path, "rev-parse", "FETCH_HEAD")
            git(self.path, "worktree", "add", "--detach", str(worktree), base)
            return base

    def read_file(self, relative: str) -> str:
        """The file at the tip of the base branch."""
        with self.lock:
            git(self.path, "fetch", "--no-tags", self.remote, f"refs/heads/{self.repo.branch}", env=self.env())
            return git(self.path, "show", f"FETCH_HEAD:{relative}")

    def push(self, commit: str, branch: str) -> None:
        with self.lock:
            remote = git(self.path, "ls-remote", self.remote, f"refs/heads/{branch}", env=self.env())
            if not remote:
                git(self.path, "push", f"--force-with-lease=refs/heads/{branch}:", self.remote,
                    f"{commit}:refs/heads/{branch}", env=self.env())
            elif remote.split()[0] != commit:
                raise ValueError(f"Remote branch {branch} exists at a different commit")


class Orchestrator:
    def __init__(self, config: Config, stop: threading.Event, notify: Callable[[str, str], Any],
                 linear: Optional[Linear] = None, github: Optional[Callable[[str], GitHub]] = None,
                 runner: Optional[Runner] = None):
        if not config.linear:
            raise ValueError("The orchestrator needs a linear block in the config")
        if not config.subagents:
            raise ValueError("Define at least one sub-agent under subagents")
        self.config = config
        self.stop = stop
        self.notify = notify
        self.linear = linear or from_settings(config.linear)
        self.github_for = github or (lambda name: GitHub(name, provider_for(config, name)))
        self.runner = runner or Runner(config.sandbox_type, config.sandbox_binds)
        self.root = config.state_dir / "linear"
        self.state = TicketState(self.root)
        self.checkouts: Dict[str, Checkout] = {}
        self.threads: Dict[str, threading.Thread] = {}
        self.review_state = config.linear.get("review_state", "In Review")
        self.board: Optional[LinearBoard] = None
        if config.linear.get("board_issue"):
            self.board = LinearBoard(self.linear, config.linear["board_issue"], cursor="orchestrator",
                                     state_dir=self.root / "board", signature=config.signature)

    # helpers -------------------------------------------------------------------
    def repo_for(self, agent: str) -> Repository:
        return self.config.repository(self.config.subagents[agent].get("repository"))

    def checkout(self, repo: Repository) -> Checkout:
        if repo.key not in self.checkouts:
            self.checkouts[repo.key] = Checkout(self.root, repo, self.github_for(repo.name))
        return self.checkouts[repo.key]

    def say(self, event: str, message: str) -> None:
        self.notify(event, message)

    def recover(self) -> None:
        for task in self.state.all():
            if task["phase"] in ("running", "publish"):
                self.state.save(task, phase="failed", reported=False,
                                error="The server stopped during the attempt; its worktree is kept.")

    # one ticket ----------------------------------------------------------------
    def run(self, issue: Dict[str, Any], agent: str) -> None:
        ident = issue["identifier"]
        previous = self.state.get(ident) or {}
        task = self.state.save({
            "id": ident, "issue_id": issue["id"], "title": issue["title"], "url": issue["url"],
            "agent": agent, "attempt": previous.get("attempt", 0) + 1, "started": time.time(),
            "description": issue.get("description") or "",
        }, phase="running", reported=False)
        directory = self.root / "attempts" / ident.lower() / f"attempt-{task['attempt']}"
        worktree, home, output = directory / "worktree", directory / "home", directory / "task.log"
        repo = self.repo_for(agent)
        spec = self.config.subagents[agent]
        try:
            checkout = self.checkout(repo)
            checkout.prepare()
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            home.mkdir(mode=0o700)
            base = checkout.new_worktree(worktree)
            backend = agents.build(spec, sandboxed=self.runner.sandboxed)
            backend.populate_home(home)
            preamble = "\n\n".join(x for x in (repo.prompt_preamble, spec.get("instructions", "")) if x)
            prompt = agents.task_prompt(repo.name, ident, issue["title"], issue.get("description") or "",
                                        preamble, repo.verify)
            prompt = prompt.replace(f"Task #{ident}", f"Linear ticket {ident}")
            stop = lambda: self.stop.is_set()  # noqa: E731
            git_dirs = [worktree / ".git", checkout.path / ".git"]
            self.runner.run(backend.argv(prompt), worktree, home, output, stop, backend.timeout,
                            network=True, env=backend.env(), git_dirs=git_dirs,
                            config_files=backend.config_files())
            if git(worktree, "rev-parse", "HEAD") != base:
                raise ValueError("The agent moved HEAD; only file edits are allowed")
            git(worktree, "add", "-A")
            changed = [p for p in git(worktree, "diff", "--cached", "--name-only", "-z").split("\0") if p]
            if not changed:
                raise ValueError("The agent made no changes")
            if not repo.allow_workflow_changes and any(p == ".github" or p.startswith(".github/") for p in changed):
                raise ValueError("Tickets may not change .github/ (set allow_workflow_changes to permit)")
            git(worktree, "diff", "--cached", "--check")
            if repo.verify:
                self.runner.run(list(repo.verify), worktree, home, output, stop,
                                self.config.verify_timeout, network=False, git_dirs=git_dirs)
                if git(worktree, "diff", "--name-only") or git(worktree, "ls-files", "--others",
                                                               "--exclude-standard"):
                    raise ValueError("Verification changed or created files")
            git(worktree, "-c", "core.hooksPath=" + os.devnull, "-c", f"user.name={self.config.git_name}",
                "-c", f"user.email={self.config.git_email}", "commit", "-q", "-m",
                f"{ident}: {issue['title']}\n\nFixes {ident}. Agent: {agent}.")
            commit = git(worktree, "rev-parse", "HEAD")
            self.state.save(task, phase="publish", commit=commit, files=len(changed))
            self.publish(task, issue, repo)
        except Stopped as error:
            self.state.save(task, phase="failed", error=f"{error}; worktree kept on the server")
        except (ValueError, RuntimeError, TimeoutError) + TRANSIENT as error:
            self.state.save(task, phase="failed", error=f"{type(error).__name__}: {error}")
        finally:
            task = self.state.get(ident) or task
            log(f"{ident} [{agent}] {task['phase']}" + (f": {task.get('error')}" if task.get("error") else ""))

    def publish(self, task: Dict[str, Any], issue: Dict[str, Any], repo: Repository) -> None:
        checkout = self.checkout(repo)
        branch = branch_name(issue, task["attempt"])
        github = checkout.github
        owner = repo.name.split("/")[0]
        existing = github.get("/pulls", state="all", head=f"{owner}:{branch}")
        if existing:
            pr = existing[0]
        else:
            checkout.push(task["commit"], branch)
            body = (f"Fixes {task['id']}: [{task['title']}]({task['url']})\n\n"
                    f"Agent `{task['agent']}`, attempt {task['attempt']}, commit `{task['commit'][:12]}`"
                    + (f", verified with `{' '.join(repo.verify)}`." if repo.verify else "."))
            pr = github.request("POST", "/pulls", {
                "title": f"[{task['id']}] {task['title']}", "head": branch, "base": repo.branch,
                "body": body, "draft": True,
            })
        self.state.save(task, phase="review", pr=pr["html_url"], branch=branch, finished=time.time())
        worktree = self.root / "attempts" / task["id"].lower() / f"attempt-{task['attempt']}" / "worktree"
        try:
            with checkout.lock:
                git(checkout.path, "worktree", "remove", str(worktree))
        except TRANSIENT:
            pass

    # reporting -----------------------------------------------------------------
    def report(self, task: Dict[str, Any]) -> None:
        issue = self.linear.issue(task["id"])
        if task["phase"] == "review":
            self.linear.move(issue, "started", self.review_state)
            self.linear.comment(issue["id"], f"Draft PR ready from `{task['agent']}`: {task['pr']}")
            text = f"{task['id']} {task['title']}: draft PR {task['pr']}"
            self.say("task_review", text)
            if self.board:
                self.board.post(f"@{task['agent']} finished {task['id']} ({task['title']}): {task['pr']}")
        else:
            self.linear.move(issue, "backlog")
            self.linear.comment(issue["id"], f"Attempt {task['attempt']} by `{task['agent']}` failed: "
                                f"{task.get('error')}\n\nMove the ticket back to Todo to retry.")
            self.say("task_failed", f"{task['id']} {task['title']} failed ({task['agent']}): {task.get('error')}")
        self.state.save(task, reported=True)

    # board -----------------------------------------------------------------
    def watch_board(self) -> None:
        if self.board is None:
            return
        if self.board.cursor() is None:
            self.board.skip_to_latest()
            return
        watched = list(self.config.notify_mentions) + ["all"]
        for post in self.board.unread():
            if any(f"@{w}".casefold() in post.body.casefold() for w in watched):
                snippet = post.body if len(post.body) <= 400 else post.body[:400] + "..."
                self.say("board_mention", f"Board, {post.login}: {snippet}")

    # main tick -----------------------------------------------------------------
    def tick(self) -> None:
        for ident, thread in list(self.threads.items()):
            if not thread.is_alive():
                del self.threads[ident]
        for task in self.state.all():
            if task["phase"] in TERMINAL and not task.get("reported") and task["id"] not in self.threads:
                if time.time() < task.get("report_next", 0):
                    continue
                try:
                    self.report(task)
                except TRANSIENT as error:
                    self.state.save(task, report_next=time.time() + 60)
                    log(f"{task['id']}: report will retry: {error}")
        try:
            self.watch_board()
        except TRANSIENT as error:
            log(f"board watch failed: {error}")
        for agent in self.config.subagents:
            if self.stop.is_set() or len(self.threads) >= self.config.max_parallel:
                return
            if any(t.name == agent for t in self.threads.values()):
                continue  # one ticket per sub-agent at a time
            for issue in self.linear.issues(label=AGENT_PREFIX + agent):
                ident = issue["identifier"]
                known = self.state.get(ident)
                if ident in self.threads or (known and known["phase"] not in TERMINAL):
                    continue
                if known and not known.get("reported"):
                    continue
                waiting = blockers(issue)
                if waiting:
                    continue
                self.linear.move(issue, "started")
                self.linear.comment(issue["id"], f"Picked up by `{agent}` on the server.")
                self.say("task_started", f"{ident} {issue['title']}: started by {agent}")
                thread = threading.Thread(target=self.run, args=(issue, agent), name=agent, daemon=True)
                self.threads[ident] = thread
                thread.start()
                break

    def wait_idle(self, timeout: float = 0) -> None:
        start = time.monotonic()
        for thread in list(self.threads.values()):
            remaining = None if not timeout else max(0.1, timeout - (time.monotonic() - start))
            thread.join(remaining)


def status_lines(config: Config) -> List[str]:
    root = config.state_dir / "linear"
    lines = ["Linear tickets (team %s)" % config.linear.get("team")]
    if not (root / "tickets.sqlite3").exists():
        return lines + ["  nothing run yet"]
    state = TicketState(root)
    rows = state.all()
    state.close()
    for task in sorted(rows, key=lambda t: t.get("started", 0), reverse=True)[:10]:
        extra = task.get("pr") if task["phase"] == "review" else (task.get("error") or "")[:120]
        lines.append(f"  {task['phase'].upper():8}{task['id']} [{task['agent']}] {task['title']} {extra}".rstrip())
    return lines


def serve_linear(config: Config, stop: threading.Event, notify: Callable[[str, str], Any],
                 once: bool = False) -> None:
    orchestrator = Orchestrator(config, stop, notify)
    orchestrator.recover()
    log(f"orchestrator online: Linear team {config.linear['team']}, sub-agents "
        f"{', '.join(config.subagents)}, up to {config.max_parallel} in parallel")
    failures = 0
    while not stop.is_set():
        try:
            orchestrator.tick()
            failures = 0
        except (ValueError, RuntimeError) + TRANSIENT as error:
            failures = min(failures + 1, 6)
            log(f"orchestrator tick failed: {error}")
            if failures == 3:
                notify("worker_error", f"Orchestrator keeps failing: {error}")
        if once:
            orchestrator.wait_idle()
            orchestrator.tick()  # report what just finished
            break
        stop.wait(min(config.poll, 30) * (2 ** failures if failures else 1))
    orchestrator.wait_idle(timeout=30)
    orchestrator.state.close()
