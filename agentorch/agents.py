"""Headless coding agent backends.

Every backend gets the same contract: a prompt, a disposable home directory
and a git worktree. The agent may only edit files; the worker runs the
verification command, commits and publishes.

Spec keys (``agent`` in the config, overridable per repository):

``type``       banyancode | opencode | claude | codex | command
``command``    executable path or name on PATH (defaults to the type's CLI)
``model``      model id passed to the CLI, if the CLI takes one
``timeout``    seconds before the attempt is stopped (default 900)
``env``        names of environment variables to pass through, e.g. API keys
``copy_home``  files under the operator's home to copy into the disposable
               home, e.g. ``.codex/auth.json`` for a subscription login
``args``       extra arguments appended before the prompt
``argv``       for ``command``: the full argv, with ``{prompt}`` replaced
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

EDIT_ONLY = {"*": "deny", "read": "allow", "edit": "allow", "external_directory": "deny"}

DEFAULT_ENV = {
    "claude": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],
    "codex": ["OPENAI_API_KEY", "CODEX_API_KEY"],
    "opencode": ["OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
    "banyancode": ["OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
}


class Agent:
    type = "agent"
    default_command = ""

    def __init__(self, spec: Dict[str, Any], sandboxed: bool = True):
        self.spec = spec
        self.sandboxed = sandboxed
        self.model: Optional[str] = spec.get("model")
        self.timeout = int(spec.get("timeout", 900))
        if not 10 <= self.timeout <= 4 * 3600:
            raise ValueError("agent.timeout must be between 10 seconds and 4 hours")
        self.extra_args: List[str] = list(spec.get("args") or [])
        names = spec.get("env")
        self.env_names: List[str] = list(DEFAULT_ENV.get(self.type, []) if names is None else names)
        self.copy_home: List[str] = list(spec.get("copy_home") or [])

    # --- resolution -----------------------------------------------------------
    def executable(self) -> str:
        name = self.spec.get("command") or self.default_command
        path = shutil.which(os.path.expanduser(name)) if name else None
        if not path:
            raise ValueError(f"{self.type}: cannot find executable {name!r}; set agent.command")
        return os.path.realpath(path)

    def argv(self, prompt: str) -> List[str]:
        raise NotImplementedError

    def config_files(self) -> Dict[str, str]:
        """Files (relative to the worktree) the runner places read-only for the run."""
        return {}

    def env(self) -> Dict[str, str]:
        return {name: os.environ[name] for name in self.env_names if os.environ.get(name)}

    def populate_home(self, home: Path) -> None:
        real_home = Path.home()
        for relative in self.copy_home:
            source = (real_home / relative).resolve()
            if real_home.resolve() not in source.parents or not source.is_file():
                raise ValueError(f"copy_home entry must be a file under your home: {relative}")
            target = home / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, target)
            target.chmod(0o600)


class BanyanCode(Agent):
    type = "banyancode"
    default_command = "banyancode"

    def argv(self, prompt: str) -> List[str]:
        argv = [self.executable(), "--standalone", "run", "--format", "json"]
        if self.model:
            argv += ["--model", self.model]
        return argv + self.extra_args + [prompt]

    def config_files(self) -> Dict[str, str]:
        return {"banyancode.json": json.dumps({
            "permission": EDIT_ONLY,
            # BanyanCode can fan a ticket out to its own sub-agents (up to 25).
            "banyancode_max_subagents": max(1, min(25, int(self.spec.get("max_subagents", 1)))),
            "banyancode_disable_websearch": True,
            "banyancode_codegraph_watch_enabled": False,
        })}


class OpenCode(Agent):
    type = "opencode"
    default_command = "opencode"

    def argv(self, prompt: str) -> List[str]:
        argv = [self.executable(), "run"]
        if self.model:
            argv += ["--model", self.model]
        return argv + self.extra_args + [prompt]

    def config_files(self) -> Dict[str, str]:
        return {"opencode.json": json.dumps({"$schema": "https://opencode.ai/config.json",
                                             "permission": EDIT_ONLY})}


class Claude(Agent):
    type = "claude"
    default_command = "claude"

    def argv(self, prompt: str) -> List[str]:
        argv = [self.executable(), "-p", "--output-format", "json",
                "--permission-mode", "acceptEdits",
                "--allowedTools", "Read,Edit,Write,MultiEdit,Glob,Grep,LS",
                "--disallowedTools", "Bash,WebFetch,WebSearch"]
        if self.model:
            argv += ["--model", self.model]
        return argv + self.extra_args + [prompt]


class Codex(Agent):
    type = "codex"
    default_command = "codex"

    def argv(self, prompt: str) -> List[str]:
        argv = [self.executable(), "exec", "--skip-git-repo-check"]
        # Codex's own Linux sandbox cannot nest inside bubblewrap; the outer
        # sandbox already confines it to the worktree.
        argv += (["--dangerously-bypass-approvals-and-sandbox"] if self.sandboxed
                 else ["--sandbox", "workspace-write"])
        if self.model:
            argv += ["--model", self.model]
        return argv + self.extra_args + [prompt]


class Command(Agent):
    type = "command"

    def __init__(self, spec: Dict[str, Any], sandboxed: bool = True):
        super().__init__(spec, sandboxed)
        template = spec.get("argv")
        if not isinstance(template, list) or not template or "{prompt}" not in template:
            raise ValueError("agent.argv must be a list containing a \"{prompt}\" element")
        self.template = template

    def executable(self) -> str:
        path = shutil.which(os.path.expanduser(self.template[0]))
        if not path:
            raise ValueError(f"command: cannot find executable {self.template[0]!r}")
        return os.path.realpath(path)

    def argv(self, prompt: str) -> List[str]:
        return [self.executable()] + [prompt if a == "{prompt}" else a for a in self.template[1:]]


TYPES = {cls.type: cls for cls in (BanyanCode, OpenCode, Claude, Codex, Command)}


def build(spec: Dict[str, Any], sandboxed: bool = True) -> Agent:
    kind = spec.get("type", "banyancode")
    if kind not in TYPES:
        raise ValueError(f"Unknown agent type {kind!r}")
    return TYPES[kind](spec, sandboxed)


def task_prompt(repository: str, number: int, title: str, body: str, preamble: str = "",
                verify: Optional[List[str]] = None) -> str:
    parts = [
        "You are working on an approved task in a git checkout of " + repository + ".",
        "Make the change by editing files only. Do not run shell commands, do not commit, "
        "and do not touch files unrelated to the task. The worker verifies, commits and "
        "opens the pull request after you finish.",
    ]
    if verify:
        parts.append("Afterwards the worker runs `" + " ".join(verify) + "`; make sure it would pass, "
                     "adding or updating tests when the task calls for it.")
    if preamble:
        parts.append(preamble.strip())
    parts.append(f"Task #{number}: {title}\n\n{body.strip()}")
    return "\n\n".join(parts)
