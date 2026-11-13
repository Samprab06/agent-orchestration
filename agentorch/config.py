"""Configuration loading and validation.

There are two kinds of configuration:

* The operator config (JSON, default ``~/.config/agentorch/config.json``) is
  only needed on a machine that runs the worker or acts on tasks with the
  operator's credentials, such as a server that Hermes controls.
* The project file ``.agentorch.json`` is committed to each project
  repository. It names the board issue so that every teammate's agent finds
  the same board without extra setup.

Board commands work without an operator config: they use the repository from
``git remote`` and the caller's own GitHub login.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]+(\[bot\])?$")
AUTO_APPROVE = ("none", "approvers", "all")
AGENT_TYPES = ("banyancode", "opencode", "claude", "codex", "command")
SANDBOX_TYPES = ("bwrap", "none")
NOTIFY_TYPES = ("command", "signal-cli", "stdout")
EVENTS = (
    "task_waiting", "task_started", "task_review",
    "task_failed", "task_cancelled", "board_mention", "worker_error",
)
PROJECT_FILE = ".agentorch.json"


class ConfigError(ValueError):
    """The configuration is missing or invalid."""


def expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def default_config_path() -> Path:
    env = os.environ.get("AGENTORCH_CONFIG")
    if env:
        return expand(env)
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return expand(base) / "agentorch" / "config.json"


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return expand(base) / "agentorch"


def valid_repo(value: Any) -> bool:
    return isinstance(value, str) and bool(REPO_RE.match(value)) and not value.endswith((".git", "."))


def valid_login(value: Any) -> bool:
    return isinstance(value, str) and bool(LOGIN_RE.match(value))


def _check_branch(branch: Any) -> None:
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise ConfigError(f"Invalid branch name: {branch!r}")
    result = subprocess.run(
        ["git", "check-ref-format", "refs/heads/" + branch], capture_output=True, timeout=10,
    )
    if result.returncode:
        raise ConfigError(f"Invalid branch name: {branch!r}")


def _argv(value: Any, field: str) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        raise ConfigError(f"{field} must be a list of arguments, not a shell string")
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{field} must be a non-empty list of strings")
    return list(value)


class Repository:
    def __init__(self, data: Dict[str, Any], defaults: "Config"):
        allowed = {"repository", "branch", "board_issue", "auto_approve", "verify", "agent",
                   "allow_workflow_changes", "prompt_preamble"}
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(f"Unknown repository keys: {', '.join(sorted(unknown))}")
        if not valid_repo(data.get("repository")):
            raise ConfigError(f"Repository must use OWNER/NAME: {data.get('repository')!r}")
        self.name: str = data["repository"]
        self.branch: str = data.get("branch", "main")
        _check_branch(self.branch)
        board = data.get("board_issue")
        if board is not None and (not isinstance(board, int) or board < 1):
            raise ConfigError(f"{self.name}: board_issue must be a positive issue number")
        self.board_issue: Optional[int] = board
        self.auto_approve: str = data.get("auto_approve", defaults.auto_approve)
        if self.auto_approve not in AUTO_APPROVE:
            raise ConfigError(f"auto_approve must be one of {', '.join(AUTO_APPROVE)}")
        self.verify = _argv(data.get("verify"), f"{self.name}: verify")
        self.agent: Dict[str, Any] = dict(defaults.agent, **(data.get("agent") or {}))
        self.allow_workflow_changes: bool = bool(data.get("allow_workflow_changes", False))
        self.prompt_preamble: str = data.get("prompt_preamble", "")

    @property
    def key(self) -> str:
        return self.name.lower()


class Config:
    def __init__(self, data: Dict[str, Any], path: Optional[Path] = None):
        if not isinstance(data, dict):
            raise ConfigError("The config file must contain a JSON object")
        self.path = path
        self.raw = data
        github = data.get("github") or {}
        self.github_auth: str = github.get("auth", "token")
        if self.github_auth not in ("app", "token", "gh"):
            raise ConfigError("github.auth must be app, token or gh")
        self.app_id: Optional[int] = github.get("app_id")
        self.private_key = expand(github.get("private_key", "~/.config/agentorch/github-app.pem"))
        self.token_env: str = github.get("token_env", "GH_TOKEN")
        if self.github_auth == "app" and not isinstance(self.app_id, int):
            raise ConfigError("github.app_id is required when github.auth is app")

        approvers = data.get("approvers") or []
        if not isinstance(approvers, list) or not all(valid_login(a) for a in approvers):
            raise ConfigError("approvers must be a list of GitHub logins")
        self.approvers = {a.casefold() for a in approvers}
        self.auto_approve: str = data.get("auto_approve", "none")
        if self.auto_approve not in AUTO_APPROVE:
            raise ConfigError(f"auto_approve must be one of {', '.join(AUTO_APPROVE)}")

        identity = data.get("identity") or {}
        self.human: str = identity.get("human", "Operator")
        self.agent_name: str = identity.get("agent", "Server")

        self.agent: Dict[str, Any] = dict({"type": "banyancode", "timeout": 900}, **(data.get("agent") or {}))
        if self.agent["type"] not in AGENT_TYPES:
            raise ConfigError(f"agent.type must be one of {', '.join(AGENT_TYPES)}")

        sandbox = data.get("sandbox") or {}
        self.sandbox_type: str = sandbox.get("type", "bwrap")
        if self.sandbox_type not in SANDBOX_TYPES:
            raise ConfigError("sandbox.type must be bwrap or none")
        self.sandbox_binds: List[str] = list(sandbox.get("binds") or [])
        self.verify_timeout: int = int(sandbox.get("verify_timeout", 300))

        worker = data.get("worker") or {}
        self.state_dir = expand(worker.get("state_dir", "~/.local/share/agentorch"))
        self.poll: int = int(worker.get("poll", 45))
        if not 10 <= self.poll <= 3600:
            raise ConfigError("worker.poll must be between 10 and 3600 seconds")
        self.publish_attempts: int = int(worker.get("publish_attempts", 3))
        self.keep_logs: int = int(worker.get("keep_logs", 20))
        self.min_free_bytes: int = int(float(worker.get("min_free_gb", 2)) * 1024 ** 3)
        self.task_trigger: str = worker.get("task_trigger", "/task")
        self.watch_board: bool = bool(worker.get("watch_board", True))
        self.git_name: str = worker.get("git_name", "agentorch")
        self.git_email: str = worker.get("git_email", "agentorch@users.noreply.github.com")

        notify = data.get("notify") or []
        if not isinstance(notify, list):
            raise ConfigError("notify must be a list")
        for item in notify:
            if not isinstance(item, dict) or item.get("type") not in NOTIFY_TYPES:
                raise ConfigError(f"Each notifier needs a type: {', '.join(NOTIFY_TYPES)}")
        self.notify: List[Dict[str, Any]] = notify
        events = data.get("notify_events", list(EVENTS))
        if not isinstance(events, list) or set(events) - set(EVENTS):
            raise ConfigError(f"notify_events may contain: {', '.join(EVENTS)}")
        self.notify_events = set(events)
        mentions = data.get("notify_mentions", sorted(self.approvers))
        if not isinstance(mentions, list):
            raise ConfigError("notify_mentions must be a list of logins")
        self.notify_mentions = {m.casefold() for m in mentions}

        repos = data.get("repositories") or []
        if not isinstance(repos, list):
            raise ConfigError("repositories must be a list")
        self.repositories: Dict[str, Repository] = {}
        for entry in repos:
            if not isinstance(entry, dict):
                raise ConfigError("Each repository entry must be an object")
            repo = Repository(entry, self)
            if repo.key in self.repositories:
                raise ConfigError(f"Duplicate repository: {repo.name}")
            self.repositories[repo.key] = repo

    @property
    def signature(self) -> str:
        return f"[{self.human}'s {self.agent_name}]"

    def repository(self, name: Optional[str]) -> Repository:
        if name:
            repo = self.repositories.get(name.lower())
            if repo is None:
                raise ConfigError(f"{name} is not in the configured repositories")
            return repo
        if len(self.repositories) == 1:
            return next(iter(self.repositories.values()))
        raise ConfigError("Pass --repo OWNER/NAME; more than one repository is configured")

    @classmethod
    def load(cls, path: Optional[Path] = None, required: bool = True) -> Optional["Config"]:
        path = path or default_config_path()
        if not path.exists():
            if required:
                raise ConfigError(f"No config at {path}. Copy deploy/config.example.json there.")
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ConfigError(f"{path}: {error}") from error
        return cls(data, path)


def git_root(start: Optional[Path] = None) -> Optional[Path]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True,
            text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def repo_from_remote(start: Optional[Path] = None) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=start, capture_output=True,
            text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode:
        return None
    return parse_remote(out.stdout.strip())


def parse_remote(url: str) -> Optional[str]:
    path = None
    if "://" in url:
        rest = url.split("://", 1)[1]
        path = rest.split("/", 1)[1] if "/" in rest else None
    elif url.startswith("git@") and ":" in url:
        path = url.split(":", 1)[1]
    if not path:
        return None
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path if valid_repo(path) else None


def project_settings(start: Optional[Path] = None) -> Dict[str, Any]:
    root = git_root(start)
    if root is None:
        return {}
    path = root / PROJECT_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
