"""Linear as the ticket system and message board (GraphQL API, stdlib only).

Conventions:

* Tickets for an agent carry the label ``agent:<name>``. A ticket in a state of
  type ``unstarted`` (Todo) with that label is ready for the agent.
* The server moves a ticket to the first ``started`` state when it picks it up,
  to a state named ``In Review`` (if the team has one) when the draft PR is
  open, and back to ``backlog`` when an attempt fails. Moving it to Todo again
  retries it.
* "Blocked by" relations hold a ticket back until its blockers are completed.
* The board is one ticket labelled ``board``; posts are its comments.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agentorch.board import SIGNATURE_RE, Post, mentions
from agentorch.config import expand, project_settings, state_home

API = "https://api.linear.app/graphql"
AGENT_PREFIX = "agent:"
BOARD_LABEL = "board"
NETWORK = (URLError, TimeoutError, ConnectionError)


class LinearError(RuntimeError):
    pass


def api_key(key_file: Optional[str] = None, env: str = "LINEAR_API_KEY") -> str:
    if os.environ.get(env):
        return os.environ[env].strip()
    path = expand(key_file or "~/.config/agentorch/linear-key")
    if path.is_file():
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise ValueError(f"{path} must be owner-only (chmod 600)")
        return path.read_text(encoding="utf-8").strip()
    raise ValueError(f"No Linear API key: set {env} or save one to {path} (chmod 600)")


class Linear:
    def __init__(self, key: str, team_key: str, api: str = API):
        if not key:
            raise ValueError("Empty Linear API key")
        self._key = key
        self.team_key = team_key.upper()
        self.api = api
        self._team: Optional[Dict[str, Any]] = None
        self._viewer: Optional[Dict[str, Any]] = None

    # transport ---------------------------------------------------------------
    def query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        request = Request(self.api, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": self._key, "User-Agent": "agentorch",
        })
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
        except HTTPError as error:
            try:
                detail = json.loads(error.read() or b"{}")
                message = "; ".join(e.get("message", "?") for e in detail.get("errors", [])) or error.reason
            except ValueError:
                message = error.reason
            raise LinearError(f"Linear API {error.code}: {message}") from None
        if result.get("errors"):
            raise LinearError("Linear: " + "; ".join(e.get("message", "?") for e in result["errors"]))
        return result["data"]

    # workspace ---------------------------------------------------------------
    def viewer(self) -> Dict[str, Any]:
        if self._viewer is None:
            self._viewer = self.query("{ viewer { id name displayName email } }")["viewer"]
        return self._viewer

    def team(self, refresh: bool = False) -> Dict[str, Any]:
        if self._team is None or refresh:
            data = self.query("""query($key: String!) { teams(filter: {key: {eq: $key}}) { nodes {
                id key name
                states(first: 50) { nodes { id name type position } }
                labels(first: 250) { nodes { id name } }
            } } }""", {"key": self.team_key})
            nodes = data["teams"]["nodes"]
            if not nodes:
                raise ValueError(f"No Linear team with key {self.team_key}")
            self._team = nodes[0]
        return self._team

    def state(self, kind: str, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        states = sorted(self.team()["states"]["nodes"], key=lambda s: s["position"])
        if name:
            for state in states:
                if state["name"].casefold() == name.casefold():
                    return state
        for state in states:
            if state["type"] == kind:
                return state
        return None

    def label_id(self, name: str, create: bool = True, color: str = "#5e6ad2") -> Optional[str]:
        for label in self.team()["labels"]["nodes"]:
            if label["name"].casefold() == name.casefold():
                return label["id"]
        if not create:
            return None
        data = self.query("""mutation($input: IssueLabelCreateInput!) {
            issueLabelCreate(input: $input) { issueLabel { id name } } }""",
                          {"input": {"name": name, "teamId": self.team()["id"], "color": color}})
        label = data["issueLabelCreate"]["issueLabel"]
        self.team()["labels"]["nodes"].append(label)
        return label["id"]

    # issues ------------------------------------------------------------------
    ISSUE_FIELDS = """id identifier title description url priority updatedAt createdAt branchName
        state { id name type } labels { nodes { id name } }
        creator { name displayName } assignee { name displayName }
        inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }"""

    def issue(self, ref: str) -> Dict[str, Any]:
        data = self.query(f"query($id: String!) {{ issue(id: $id) {{ {self.ISSUE_FIELDS} }} }}", {"id": ref})
        if not data.get("issue"):
            raise ValueError(f"No Linear issue {ref}")
        return data["issue"]

    def issues(self, label: Optional[str] = None, state_types: Sequence[str] = ("unstarted",),
               first: int = 50) -> List[Dict[str, Any]]:
        flt: Dict[str, Any] = {"team": {"id": {"eq": self.team()["id"]}}}
        if state_types:
            flt["state"] = {"type": {"in": list(state_types)}}
        if label:
            flt["labels"] = {"some": {"name": {"eqIgnoreCase": label}}}
        data = self.query(f"""query($filter: IssueFilter, $first: Int) {{
            issues(filter: $filter, first: $first) {{ nodes {{ {self.ISSUE_FIELDS} }} }} }}""",
                          {"filter": flt, "first": first})
        rows = data["issues"]["nodes"]
        # Urgent (1) first, "no priority" (0) last, then oldest first.
        rows.sort(key=lambda i: (i.get("priority") or 5, i.get("createdAt", "")))
        return rows

    def update_issue(self, issue_id: str, **fields: Any) -> None:
        self.query("""mutation($id: String!, $input: IssueUpdateInput!) {
            issueUpdate(id: $id, input: $input) { success } }""", {"id": issue_id, "input": fields})

    def move(self, issue: Dict[str, Any], kind: str, name: Optional[str] = None) -> Optional[str]:
        state = self.state(kind, name)
        if state and issue.get("state", {}).get("id") != state["id"]:
            self.update_issue(issue["id"], stateId=state["id"])
            issue["state"] = state
        return state["name"] if state else None

    def set_labels(self, issue: Dict[str, Any], add: Sequence[str] = (), remove: Sequence[str] = ()) -> None:
        current = {l["name"].casefold(): l["id"] for l in issue["labels"]["nodes"]}
        for name in remove:
            current.pop(name.casefold(), None)
        for name in add:
            current.setdefault(name.casefold(), self.label_id(name))
        self.update_issue(issue["id"], labelIds=list(current.values()))

    def comment(self, issue_id: str, body: str) -> Dict[str, Any]:
        data = self.query("""mutation($input: CommentCreateInput!) {
            commentCreate(input: $input) { comment { id body createdAt url user { name displayName } } } }""",
                          {"input": {"issueId": issue_id, "body": body}})
        return data["commentCreate"]["comment"]

    def comments(self, issue_id: str, since: Optional[str] = None, first: int = 100) -> List[Dict[str, Any]]:
        flt = {"createdAt": {"gt": since}} if since else None
        data = self.query("""query($id: String!, $first: Int, $filter: CommentFilter) {
            issue(id: $id) { comments(first: $first, filter: $filter) {
                nodes { id body createdAt url user { name displayName } } } } }""",
                          {"id": issue_id, "first": first, "filter": flt})
        rows = (data.get("issue") or {}).get("comments", {}).get("nodes", [])
        rows.sort(key=lambda c: c["createdAt"])
        return rows

    def create_issue(self, title: str, description: str = "", labels: Sequence[str] = (),
                     state_kind: str = "unstarted", priority: Optional[int] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"teamId": self.team()["id"], "title": title, "description": description,
                                   "labelIds": [self.label_id(l) for l in labels]}
        state = self.state(state_kind)
        if state:
            payload["stateId"] = state["id"]
        if priority is not None:
            payload["priority"] = int(priority)
        data = self.query("""mutation($input: IssueCreateInput!) {
            issueCreate(input: $input) { issue { id identifier title url } } }""", {"input": payload})
        return data["issueCreate"]["issue"]

    def block(self, blocker_id: str, blocked_id: str) -> None:
        self.query("""mutation($input: IssueRelationCreateInput!) {
            issueRelationCreate(input: $input) { success } }""",
                   {"input": {"issueId": blocker_id, "relatedIssueId": blocked_id, "type": "blocks"}})


def agent_labels(issue: Dict[str, Any]) -> List[str]:
    return [l["name"][len(AGENT_PREFIX):] for l in issue["labels"]["nodes"]
            if l["name"].lower().startswith(AGENT_PREFIX)]


def blockers(issue: Dict[str, Any]) -> List[str]:
    """Identifiers of unfinished issues that block this one."""
    result = []
    for rel in (issue.get("inverseRelations") or {}).get("nodes", []):
        other = rel.get("issue") or {}
        if rel.get("type") == "blocks" and (other.get("state") or {}).get("type") not in ("completed", "canceled"):
            result.append(other.get("identifier", "?"))
    return result


def branch_name(issue: Dict[str, Any], attempt: int = 1) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", issue["title"].lower()).strip("-")[:40].strip("-") or "task"
    base = f"{issue['identifier'].lower()}-{slug}"
    return base if attempt == 1 else f"{base}-attempt-{attempt}"


def from_settings(settings: Dict[str, Any]) -> Linear:
    team = settings.get("team")
    if not team:
        raise ValueError("Linear settings need a team key, e.g. {\"team\": \"ENG\"}")
    return Linear(api_key(settings.get("api_key_file"), settings.get("api_key_env", "LINEAR_API_KEY")), team)


def project_linear() -> Optional[Dict[str, Any]]:
    """The ``linear`` block of the project's .agentorch.json, if any."""
    data = project_settings().get("linear")
    return data if isinstance(data, dict) else None


# --- board ---------------------------------------------------------------------

class LinearBoard:
    """The message board on a Linear ticket. Same interface as the GitHub board."""

    def __init__(self, linear: Linear, issue: str, cursor: Optional[str] = None,
                 handles: Sequence[str] = (), state_dir: Optional[Path] = None,
                 signature: Optional[str] = None):
        self.linear = linear
        self.issue_ref = issue
        self._issue: Optional[Dict[str, Any]] = None
        self.cursor_name = cursor or os.environ.get("AGENTORCH_BOARD_CURSOR") or "default"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.cursor_name):
            raise ValueError("Cursor names may use letters, digits, dot, dash and underscore")
        env_handles = [h.strip().lstrip("@") for h in os.environ.get("AGENTORCH_HANDLES", "").split(",") if h.strip()]
        self.handles_list = [h for h in handles if h] + env_handles
        self.state_dir = state_dir or state_home() / "board"
        self.signature = signature or os.environ.get("AGENTORCH_SIGNATURE")

    @property
    def issue(self) -> str:
        return self.issue_ref

    @property
    def login(self) -> str:
        viewer = self.linear.viewer()
        return viewer.get("displayName") or viewer.get("name") or "me"

    def handles(self) -> List[str]:
        own = []
        if self.signature:
            match = re.match(r"^\[?([^\]']+)'s ([^\]]+)\]?$", self.signature.strip())
            if match:
                own = [match.group(2).strip().replace(" ", "-")]
        return [self.login, "all", *own, *self.handles_list]

    def _issue_id(self) -> str:
        if self._issue is None:
            self._issue = self.linear.issue(self.issue_ref)
        return self._issue["id"]

    def _own(self, post: Post) -> bool:
        if self.signature:
            sig = self.signature if self.signature.startswith("[") else f"[{self.signature}]"
            return post.body.startswith(sig)
        return post.login == self.login

    def _post(self, raw: Dict[str, Any]) -> Post:
        user = raw.get("user") or {}
        login = user.get("displayName") or user.get("name") or "app"
        body = (raw.get("body") or "").replace("\r", "")
        post = Post(raw["id"], login, raw["createdAt"], body, False, raw.get("url", ""))
        post.mention = not self._own(post) and mentions(body, self.handles())
        return post

    # cursor
    def _cursor_path(self) -> Path:
        return self.state_dir / f"linear-{self.issue_ref.lower()}-{self.cursor_name}.json"

    def cursor(self) -> Optional[dict]:
        try:
            data = json.loads(self._cursor_path().read_text(encoding="utf-8"))
            return data if data.get("created_at") else None
        except (OSError, ValueError):
            return None

    def _set_cursor(self, post: Post) -> None:
        path = self._cursor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": post.id, "created_at": post.created_at}), encoding="utf-8")
        os.replace(tmp, path)

    def last(self, count: int = 10) -> List[Post]:
        rows = self.linear.comments(self._issue_id(), first=250)
        return [self._post(r) for r in rows[-count:]] if count > 0 else []

    def skip_to_latest(self) -> None:
        latest = self.last(1)
        self._set_cursor(latest[-1] if latest else Post("0", "", "1970-01-01T00:00:00.000Z", ""))

    def unread(self, mark: bool = True) -> List[Post]:
        cursor = self.cursor()
        if cursor is None:
            posts = self.last(10)
        else:
            posts = [self._post(r) for r in self.linear.comments(self._issue_id(), since=cursor["created_at"])]
        if posts and mark:
            self._set_cursor(posts[-1])
        return [p for p in posts if not self._own(p)]

    def wait(self, interval: int = 30, timeout: int = 0, mentions_only: bool = False,
             sleep: Callable[[float], None] = time.sleep) -> List[Post]:
        start = time.monotonic()
        while True:
            try:
                posts = self.unread()
            except (LinearError, OSError, ValueError) + NETWORK:
                posts = []
            if mentions_only:
                posts = [p for p in posts if p.mention]
            if posts:
                return posts
            if timeout and time.monotonic() - start >= timeout:
                raise TimeoutError(f"No new posts after {timeout}s")
            sleep(interval)

    def post(self, text: str, signature: Optional[str] = None) -> Post:
        text = text.strip()
        if not text:
            raise ValueError("Empty post")
        signature = signature or self.signature
        if not SIGNATURE_RE.match(text):
            if not signature:
                raise ValueError("Posts must start with [<human>'s <agent>]; set AGENTORCH_SIGNATURE")
            text = f"{signature if signature.startswith('[') else '[' + signature + ']'} {text}"
        raw = self.linear.comment(self._issue_id(), text)
        post = self._post(raw)
        post.mention = False
        return post


def find_board(linear: Linear) -> str:
    rows = linear.issues(label=BOARD_LABEL, state_types=("backlog", "unstarted", "started", "triage"), first=5)
    if len(rows) == 1:
        return rows[0]["identifier"]
    if not rows:
        raise ValueError("No Linear board ticket. Run agentorch init --linear TEAM.")
    raise ValueError("Several open Linear tickets have the board label; set linear.board_issue")
