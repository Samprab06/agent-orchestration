"""Agent-to-agent message board on a single pinned GitHub issue.

Every agent posts with a ``[<human>'s <agent>]`` signature and mentions
teammates by GitHub login. Each agent keeps its own read cursor, so several
agents can share one GitHub account on one machine as long as they use
different cursor names (``--cursor`` or ``AGENTORCH_BOARD_CURSOR``).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from agentorch.config import project_settings, state_home
from agentorch.github import GitHub, GitHubError

SIGNATURE_RE = re.compile(r"^\[[^\]\n]+'s [^\]\n]+\]")
BOARD_LABEL = "board"


@dataclass
class Post:
    id: int
    login: str
    created_at: str
    body: str
    mention: bool = False
    url: str = ""
    association: str = ""

    def render(self) -> str:
        flag = " ** MENTION **" if self.mention else ""
        return f"--- @{self.login} {self.created_at} (#{self.id}){flag}\n{self.body}"


def mentions(body: str, handles: Iterable[str]) -> bool:
    names = [re.escape(h) for h in handles if h]
    if not names:
        return False
    pattern = r"(^|[^A-Za-z0-9_-])@(" + "|".join(names) + r")(?=$|[^A-Za-z0-9_-])"
    return re.search(pattern, body, re.IGNORECASE) is not None


def mentioned_logins(body: str) -> List[str]:
    found = re.findall(r"(?:^|[^A-Za-z0-9_-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?=$|[^A-Za-z0-9_-])", body)
    seen, result = set(), []
    for login in found:
        if login.casefold() not in seen:
            seen.add(login.casefold())
            result.append(login)
    return result


def resolve_issue(github: GitHub, explicit: Optional[int] = None, configured: Optional[int] = None) -> int:
    """Find the board issue: flag, env, project file, operator config, then the ``board`` label."""
    if explicit:
        return explicit
    for name in ("AGENTORCH_BOARD_ISSUE", "BOARD_ISSUE"):
        value = os.environ.get(name, "").strip()
        if value:
            if not value.isdigit():
                raise ValueError(f"{name} must be an issue number")
            return int(value)
    project = project_settings().get("board_issue")
    if isinstance(project, int) and project > 0:
        return project
    if configured:
        return configured
    boards = [i for i in github.get("/issues", labels=BOARD_LABEL, state="open", per_page=10)
              if "pull_request" not in i]
    if len(boards) == 1:
        return boards[0]["number"]
    if not boards:
        raise ValueError("No board issue found. Run agentorch init or set AGENTORCH_BOARD_ISSUE.")
    raise ValueError("Several open issues have the board label; set AGENTORCH_BOARD_ISSUE")


class Board:
    def __init__(self, github: GitHub, issue: int, login: Optional[str] = None,
                 cursor: Optional[str] = None, handles: Sequence[str] = (),
                 state_dir: Optional[Path] = None):
        self.github = github
        self.issue = issue
        self._login = login
        self.cursor_name = cursor or os.environ.get("AGENTORCH_BOARD_CURSOR") or "default"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.cursor_name):
            raise ValueError("Cursor names may use letters, digits, dot, dash and underscore")
        self.extra_handles = [h for h in handles if h]
        env_handles = os.environ.get("AGENTORCH_HANDLES", "")
        self.extra_handles += [h.strip().lstrip("@") for h in env_handles.split(",") if h.strip()]
        self.state_dir = state_dir or state_home() / "board"

    # identity ---------------------------------------------------------------
    @property
    def login(self) -> str:
        if self._login is None:
            self._login = os.environ.get("AGENTORCH_LOGIN") or self.github.login()
        return self._login

    def handles(self) -> List[str]:
        own = self.login[:-5] if self.login.endswith("[bot]") else self.login
        return [own, "all", *self.extra_handles]

    # cursor -------------------------------------------------------------------
    def _cursor_path(self) -> Path:
        repo = self.github.repository.replace("/", "_").lower()
        return self.state_dir / f"{repo}-{self.issue}-{self.cursor_name}.json"

    def cursor(self) -> Optional[dict]:
        try:
            data = json.loads(self._cursor_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(data, dict) and isinstance(data.get("id"), int) and data.get("created_at"):
            return data
        return None

    def _set_cursor(self, post: Post) -> None:
        current = self.cursor()
        if current and current["id"] >= post.id:
            return
        path = self._cursor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": post.id, "created_at": post.created_at}), encoding="utf-8")
        os.replace(tmp, path)

    # reading ------------------------------------------------------------------
    def _post(self, raw: dict, flag: bool = True) -> Post:
        login = (raw.get("user") or {}).get("login", "ghost")
        body = (raw.get("body") or "").replace("\r", "")
        mention = flag and login.casefold() != self.login.casefold() and mentions(body, self.handles())
        return Post(int(raw["id"]), login, raw["created_at"], body, mention, raw.get("html_url", ""),
                    raw.get("author_association", ""))

    def last(self, count: int = 10) -> List[Post]:
        total = self.github.issue(self.issue).get("comments", 0)
        if not total or count <= 0:
            return []
        page = (total + 99) // 100
        rows: List[dict] = []
        while page >= 1 and len(rows) < count:
            rows = self.github.get(f"/issues/{self.issue}/comments", per_page=100, page=page) + rows
            page -= 1
        return [self._post(r) for r in rows[-count:]]

    def since(self, timestamp: str) -> List[Post]:
        rows = self.github.paginate(f"/issues/{self.issue}/comments", since=timestamp)
        return [self._post(r) for r in rows]

    def skip_to_latest(self) -> None:
        """Mark everything already on the board as read."""
        latest = self.last(1)
        self._set_cursor(latest[-1] if latest else Post(0, "", "1970-01-01T00:00:00Z", ""))

    def unread(self, mark: bool = True) -> List[Post]:
        """Posts by others since the last check. The first call shows the last 10 for context."""
        cursor = self.cursor()
        if cursor is None:
            posts = self.last(10)
        else:
            posts = [p for p in self.since(cursor["created_at"]) if p.id > cursor["id"]]
        posts.sort(key=lambda p: p.id)
        if posts and mark:
            self._set_cursor(posts[-1])
        me = self.login.casefold()
        return [p for p in posts if p.login.casefold() != me]

    def wait(self, interval: int = 30, timeout: int = 0, mentions_only: bool = False,
             sleep: Callable[[float], None] = time.sleep) -> List[Post]:
        start = time.monotonic()
        while True:
            try:
                posts = self.unread()
            except (GitHubError, OSError, ValueError):
                posts = []  # transient; try again next tick
            if mentions_only:
                posts = [p for p in posts if p.mention]
            if posts:
                return posts
            if timeout and time.monotonic() - start >= timeout:
                raise TimeoutError(f"No new posts after {timeout}s")
            sleep(interval)

    # writing ------------------------------------------------------------------
    def post(self, text: str, signature: Optional[str] = None) -> Post:
        text = text.strip()
        if not text:
            raise ValueError("Empty post")
        signature = signature or os.environ.get("AGENTORCH_SIGNATURE")
        if not SIGNATURE_RE.match(text):
            if not signature:
                raise ValueError(
                    "Posts must start with [<human>'s <agent>], e.g. [Sam's Claude]. "
                    "Set AGENTORCH_SIGNATURE to add it automatically."
                )
            if not signature.startswith("["):
                signature = f"[{signature}]"
            text = f"{signature} {text}"
        raw = self.github.comment(self.issue, text)
        post = self._post(raw, flag=False)
        return post


def session_start_message(board: Board, supervised: bool = False) -> str:
    lines = [f"Team board: GitHub issue #{board.issue} in {board.github.repository}. Rules are in AGENTS.md."]
    try:
        posts = board.unread()
        lines.append("Unread posts:\n" + "\n".join(p.render() for p in posts) if posts else "No unread posts.")
    except (GitHubError, OSError, ValueError) as error:
        lines.append(f"(board unreachable: {error}). Run `agentorch board unread` later.")
    if supervised:
        lines.append(
            "Board loop: a supervisor relays posts that affect you. Do not start a background wait. "
            "Still run `agentorch board unread` before each commit and after merges."
        )
    else:
        lines.append(
            "Board loop: start ONE background command now: `agentorch board wait`. When it returns, "
            "handle the posts (a MENTION means act and reply on the board; otherwise adjust your own "
            "work only if it is affected, and do not post acknowledgements), then start it again. "
            "Never run two waits at once."
        )
    return "\n".join(lines)
