"""Command line interface: ``agentorch <command>``."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from agentorch import __version__, labels
from agentorch.board import BOARD_LABEL, Board, resolve_issue, session_start_message
from agentorch.config import (
    PROJECT_FILE, Config, ConfigError, git_root, project_settings, repo_from_remote, valid_repo,
)
from agentorch.github import NETWORK_ERRORS, GitHub, GitHubError, provider_for

TEMPLATES = Path(__file__).parent / "templates"
ERRORS = (ConfigError, GitHubError, ValueError, OSError) + NETWORK_ERRORS


def fail(message: str, code: int = 1) -> int:
    print(f"agentorch: {message}", file=sys.stderr)
    return code


def load_config(args: argparse.Namespace, required: bool) -> Optional[Config]:
    path = Path(args.config).expanduser() if args.config else None
    return Config.load(path, required=required)


def target_repo(args: argparse.Namespace, config: Optional[Config]) -> str:
    """Repository from --repo, the project file, git remote, or a single configured repo."""
    name = getattr(args, "repo", None) or os.environ.get("AGENTORCH_REPO")
    if not name:
        name = project_settings().get("repository") or repo_from_remote()
    if not name and config is not None and len(config.repositories) == 1:
        name = next(iter(config.repositories.values())).name
    if not name:
        raise ConfigError("Cannot tell which repository to use; pass --repo OWNER/NAME")
    if not valid_repo(name):
        raise ConfigError(f"Repository must use OWNER/NAME: {name!r}")
    return name


def client(args: argparse.Namespace, config: Optional[Config], repo: str) -> GitHub:
    # Board and task commands from a laptop use the caller's own login. The
    # operator config only applies to repositories it lists.
    if config is not None and repo.lower() in config.repositories:
        return GitHub(repo, provider_for(config, repo))
    return GitHub(repo, provider_for(None, repo))


# --- board ---------------------------------------------------------------------

def make_board(args: argparse.Namespace) -> Board:
    config = load_config(args, required=False)
    repo = target_repo(args, config)
    github = client(args, config, repo)
    configured = None
    if config is not None and repo.lower() in config.repositories:
        configured = config.repositories[repo.lower()].board_issue
    issue = resolve_issue(github, getattr(args, "issue", None), configured)
    return Board(github, issue, cursor=getattr(args, "cursor", None))


def cmd_board(args: argparse.Namespace) -> int:
    if args.board_cmd == "hook":
        return board_hook(args)
    try:
        board = make_board(args)
        if args.board_cmd == "post":
            text = " ".join(args.text) if args.text else sys.stdin.read()
            post = board.post(text, signature=args.signature)
            print(post.url or f"posted #{post.id}")
        elif args.board_cmd == "read":
            for post in board.last(args.n):
                print(post.render())
        elif args.board_cmd == "unread":
            for post in board.unread():
                print(post.render())
        elif args.board_cmd == "wait":
            try:
                posts = board.wait(args.interval, args.timeout, args.mentions)
            except TimeoutError as error:
                return fail(str(error), 124)
            for post in posts:
                print(post.render())
        elif args.board_cmd == "whoami":
            print(json.dumps({"login": board.login, "repository": board.github.repository,
                              "board_issue": board.issue, "cursor": board.cursor_name}))
        return 0
    except ERRORS as error:
        return fail(str(error))


def board_hook(args: argparse.Namespace) -> int:
    """Claude Code hooks. They never fail the session."""
    try:
        board = make_board(args)
    except ERRORS as error:
        if args.event == "session-start":
            print(f"Team board unavailable ({error}). See AGENTS.md.")
        return 0
    if args.event == "session-start":
        print(session_start_message(board, supervised=os.environ.get("AGENTORCH_BOARD_WAIT") == "off"))
        return 0
    try:
        posts = board.unread()
    except ERRORS:
        return 0
    if posts:
        print(f"New board posts (issue #{board.issue}):")
        for post in posts:
            print(post.render())
    return 0


# --- init ----------------------------------------------------------------------

def copy_template(source: Path, target: Path, force: bool) -> str:
    if target.exists() and not force:
        return f"kept     {target} (exists; --force overwrites)"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return f"wrote    {target}"


def cmd_init(args: argparse.Namespace) -> int:
    """Prepare a project repository: labels, a pinned board issue, and agent instructions."""
    try:
        config = load_config(args, required=False)
        repo = target_repo(args, config)
        github = client(args, config, repo)
        for name, (color, description) in labels.ALL.items():
            created = github.ensure_label(name, color, description)
            print(f"{'created' if created else 'exists '}  label {name}")
        issue = None
        try:
            issue = resolve_issue(github, args.issue)
        except ValueError:
            pass
        if issue is None:
            body = (TEMPLATES / "github/ISSUE_TEMPLATE/message-board.md").read_text(encoding="utf-8")
            body = body.split("---", 2)[2].strip()
            created = github.request("POST", "/issues", {
                "title": "Message Board", "body": body, "labels": [BOARD_LABEL],
            })
            issue = created["number"]
            print(f"created  board issue #{issue}: {created['html_url']}")
            try:
                github.graphql(
                    "mutation($id: ID!) { pinIssue(input: {issueId: $id}) { issue { number } } }",
                    {"id": created["node_id"]},
                )
                print(f"pinned   board issue #{issue}")
            except ERRORS as error:
                print(f"note     could not pin #{issue} ({error}); pin it in the GitHub UI")
        else:
            print(f"exists   board issue #{issue}")
        if args.no_files:
            print(f"Set AGENTORCH_BOARD_ISSUE={issue} or add {PROJECT_FILE} to the repository.")
            return 0
        root = git_root()
        if root is None or (repo_from_remote() or "").lower() != repo.lower():
            print(f"Not inside a checkout of {repo}; skipped project files. Run init from the checkout.")
            return 0
        project = root / PROJECT_FILE
        settings = project_settings()
        settings.update({"repository": repo, "board_issue": issue})
        project.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        print(f"wrote    {project}")
        pairs = [
            ("AGENTS.md", "AGENTS.md"), ("CLAUDE.md", "CLAUDE.md"),
            ("claude/settings.json", ".claude/settings.json"),
            ("github/ISSUE_TEMPLATE/agent-task.md", ".github/ISSUE_TEMPLATE/agent-task.md"),
            ("github/ISSUE_TEMPLATE/message-board.md", ".github/ISSUE_TEMPLATE/message-board.md"),
        ]
        pairs += [(f"github/workflows/{p.name}", f".github/workflows/{p.name}")
                  for p in sorted((TEMPLATES / "github/workflows").glob("*.yml"))]
        for source, target in pairs:
            print(copy_template(TEMPLATES / source, root / target, args.force))
        print("Commit these files so every teammate's agent picks them up.")
        return 0
    except ERRORS as error:
        return fail(str(error))


# --- doctor --------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"[{'ok' if passed else '!!'}] {label}{': ' + detail if detail else ''}")

    check("python >= 3.9", sys.version_info >= (3, 9), sys.version.split()[0])
    check("git installed", shutil.which("git") is not None)
    try:
        config = load_config(args, required=False)
        check("operator config", True, str(config.path) if config else "none (laptop mode)")
    except ConfigError as error:
        config = None
        check("operator config", False, str(error))
    try:
        repo = target_repo(args, config)
        check("repository", True, repo)
        github = client(args, config, repo)
        check("GitHub login", True, github.login())
        board = make_board(args)
        check("board issue", True, f"#{board.issue}")
    except ERRORS as error:
        check("GitHub access", False, str(error))
    if config is not None:
        for repo in config.repositories.values():
            try:
                GitHub(repo.name, provider_for(config, repo.name)).get("")
                check(f"worker access {repo.name}", True)
            except ERRORS as error:
                check(f"worker access {repo.name}", False, str(error))
    return 0 if ok else 1


# --- parser ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentorch", description=__doc__)
    parser.add_argument("--version", action="version", version=f"agentorch {__version__}")
    parser.add_argument("--config", help="Operator config (default ~/.config/agentorch/config.json)")
    commands = parser.add_subparsers(dest="command", required=True)

    def repo_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", help="OWNER/NAME (default: the current checkout's origin)")

    board = commands.add_parser("board", help="Read and post on the team message board")
    board_cmds = board.add_subparsers(dest="board_cmd", required=True)
    for name, help_text in (
        ("post", "Post a message"), ("read", "Show the latest posts"),
        ("unread", "Show posts since your last check"), ("wait", "Block until someone posts"),
        ("whoami", "Show your login and board"), ("hook", "Claude Code hook entry point"),
    ):
        sub = board_cmds.add_parser(name, help=help_text)
        repo_arg(sub)
        sub.add_argument("--issue", type=int, help="Board issue number")
        sub.add_argument("--cursor", help="Read-cursor name when agents share a login")
        if name == "post":
            sub.add_argument("text", nargs="*", help="Message (default: stdin)")
            sub.add_argument("--signature", help="e.g. \"Sam's Codex\" if the text lacks one")
        elif name == "read":
            sub.add_argument("-n", type=int, default=10)
        elif name == "wait":
            sub.add_argument("--interval", type=int, default=30)
            sub.add_argument("--timeout", type=int, default=0)
            sub.add_argument("--mentions", action="store_true", help="Only wake on mentions")
        elif name == "hook":
            sub.add_argument("event", choices=("session-start", "prompt"))
    board.set_defaults(func=cmd_board)

    init = commands.add_parser("init", help="Set up labels, the board and agent files in a project")
    repo_arg(init)
    init.add_argument("--issue", type=int, help="Use this existing issue as the board")
    init.add_argument("--no-files", action="store_true", help="Only change GitHub, write no files")
    init.add_argument("--force", action="store_true", help="Overwrite existing project files")
    init.set_defaults(func=cmd_init)

    doctor = commands.add_parser("doctor", help="Check setup and access")
    repo_arg(doctor)
    doctor.add_argument("--issue", type=int)
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
