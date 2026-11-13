"""Tasks are GitHub issues labelled ``agent:queued``.

A task runs only once it is approved:

* an approver (or the worker's own identity, which the server's operator
  controls) comments ``approved`` after the issue was last edited, or
* an approver applies the ``agent:approved`` label after the last edit, or
* the repository's ``auto_approve`` policy covers it: ``all`` approves
  every queued task, ``approvers`` approves tasks requested by an approver.

Editing the issue's title or body after approval voids the approval, and the
worker checks again right before it runs the agent and before it publishes.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from agentorch import labels

APPROVE_WORDS = {"approved", "approve", "/approve"}
REQUESTED_RE = re.compile(r"<!--\s*agentorch:requested-by=([A-Za-z0-9-]+)\s*-->")
LAST_EDITED = "query($owner: String!, $name: String!, $number: Int!) {" \
    " repository(owner: $owner, name: $name) { issue(number: $number) { lastEditedAt } } }"


@dataclass
class Approval:
    by: str
    at: str
    how: str  # comment, label, auto

    def describe(self) -> str:
        return {"auto": f"auto-approved for @{self.by}"}.get(self.how, f"approved by @{self.by} ({self.how})")


def is_approval_text(body: str) -> bool:
    first = (body or "").strip().splitlines()[0:1]
    if not first:
        return False
    word = first[0].strip().rstrip(".!").casefold()
    return word in APPROVE_WORDS


def requested_by(issue: Dict[str, Any], self_login: Optional[str] = None) -> str:
    """The person a task is for.

    For tasks the worker opened from a board post, the poster recorded in the
    body. The marker only counts on issues the worker itself authored, so
    nobody can claim to be someone else. Otherwise, the issue author.
    """
    author = (issue.get("user") or {}).get("login", "")
    match = REQUESTED_RE.search(issue.get("body") or "")
    if match and self_login and author.casefold() == self_login.casefold():
        return match.group(1)
    return author


def fingerprint(issue: Dict[str, Any]) -> str:
    """Identity of the approved content. A change means the task was edited."""
    payload = json.dumps([issue.get("node_id"), issue["number"], issue.get("title", ""),
                          issue.get("body") or ""], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def label_names(issue: Dict[str, Any]) -> Set[str]:
    return {l["name"] if isinstance(l, dict) else l for l in issue.get("labels", [])}


def last_edited(github: Any, number: int) -> Optional[str]:
    owner, name = github.repository.split("/", 1)
    data = github.graphql(LAST_EDITED, {"owner": owner, "name": name, "number": number})
    issue = (data.get("repository") or {}).get("issue") or {}
    return issue.get("lastEditedAt")


def _after(timestamp: str, edited: Optional[str]) -> bool:
    return edited is None or timestamp > edited


def find_approval(github: Any, issue: Dict[str, Any], approvers: Iterable[str],
                  auto_approve: str = "none", self_login: Optional[str] = None) -> Optional[Approval]:
    approvers = {a.casefold() for a in approvers}
    requester = requested_by(issue, self_login)
    if self_login and requester.casefold() == self_login.casefold():
        requester = ""  # the worker's own tasks never approve themselves
    if auto_approve == "all":
        return Approval(requester or "anyone", issue.get("created_at", ""), "auto")
    if auto_approve == "approvers" and requester.casefold() in approvers:
        return Approval(requester, issue.get("created_at", ""), "auto")
    trusted = set(approvers)
    if self_login:
        trusted.add(self_login.casefold())
    number = issue["number"]
    edited = last_edited(github, number)
    for comment in reversed(github.comments(number)):
        login = (comment.get("user") or {}).get("login", "")
        if (login.casefold() in trusted and is_approval_text(comment.get("body", ""))
                and _after(comment["created_at"], edited)):
            return Approval(login, comment["created_at"], "comment")
    if labels.APPROVED in label_names(issue):
        for event in reversed(list(github.paginate(f"/issues/{number}/events"))):
            login = (event.get("actor") or {}).get("login", "")
            if (event.get("event") == "labeled" and (event.get("label") or {}).get("name") == labels.APPROVED
                    and login.casefold() in trusted and _after(event["created_at"], edited)):
                return Approval(login, event["created_at"], "label")
    return None


def queued_issues(github: Any, limit_pages: int = 5) -> List[Dict[str, Any]]:
    rows = github.paginate("/issues", max_pages=limit_pages, state="open", labels=labels.QUEUED,
                           sort="created", direction="asc")
    return [i for i in rows if "pull_request" not in i]


def create_task(github: Any, title: str, body: str = "", requester: Optional[str] = None,
                source: Optional[str] = None, approve: bool = False) -> Dict[str, Any]:
    title = " ".join(title.split())
    if not title:
        raise ValueError("A task needs a title")
    if len(title) > 200:
        raise ValueError("Keep task titles under 200 characters")
    parts = [body.strip()] if body.strip() else []
    if source:
        parts.append(f"Requested on the board: {source}")
    if requester:
        parts.append(f"<!-- agentorch:requested-by={requester} -->")
    issue = github.request("POST", "/issues", {
        "title": title, "body": "\n\n".join(parts), "labels": [labels.QUEUED],
    })
    if approve:
        approve_task(github, issue["number"], note="created and approved by the operator")
    return issue


def approve_task(github: Any, number: int, note: str = "") -> None:
    issue = github.issue(number)
    if issue.get("state") != "open":
        raise ValueError(f"#{number} is closed")
    if labels.QUEUED not in label_names(issue):
        github.add_labels(number, labels.QUEUED)
    github.comment(number, "approved" + (f"\n\n_{note}_" if note else ""))


def cancel_task(github: Any, number: int, note: str = "") -> None:
    """Closing the issue cancels it. A running worker stops its attempt."""
    github.remove_label(number, labels.QUEUED)
    github.add_labels(number, labels.CANCELLED)
    if note:
        github.comment(number, note)
    github.request("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "not_planned"})


def retry_task(github: Any, number: int) -> None:
    issue = github.issue(number)
    if issue.get("state") != "open":
        github.request("PATCH", f"/issues/{number}", {"state": "open"})
    for name in (labels.FAILED, labels.CANCELLED):
        github.remove_label(number, name)
    github.add_labels(number, labels.QUEUED)


def parse_board_task(body: str, trigger: str = "/task") -> Optional[Dict[str, str]]:
    """``[Sam's Codex] /task Title`` with optional following lines as the body."""
    lines = (body or "").replace("\r", "").split("\n")
    for index, line in enumerate(lines):
        stripped = re.sub(r"^\[[^\]\n]+'s [^\]\n]+\]\s*", "", line.strip())
        if stripped.casefold().startswith(trigger.casefold() + " ") or stripped.casefold() == trigger.casefold():
            title = stripped[len(trigger):].strip()
            rest = "\n".join(lines[index + 1:]).strip()
            if not title and rest:
                title, _, rest = rest.partition("\n")
                rest = rest.strip()
            if title:
                return {"title": title.strip(), "body": rest}
    return None
