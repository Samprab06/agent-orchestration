"""In-memory stand-in for the GitHub client used by the tests."""
from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional

from agentorch.github import GitHubError


class FakeGitHub:
    def __init__(self, repository: str = "acme/app", login: str = "me"):
        self.repository = repository
        self._login = login
        self.issues: Dict[int, Dict[str, Any]] = {}
        self.comments_by_issue: Dict[int, List[Dict[str, Any]]] = {}
        self.events: Dict[int, List[Dict[str, Any]]] = {}
        self.last_edited: Dict[int, Optional[str]] = {}
        self.pulls: List[Dict[str, Any]] = []
        self.labels: Dict[str, Dict[str, Any]] = {}
        self.calls: List[tuple] = []
        self._ids = itertools.count(1000)
        self._clock = itertools.count(1)

    # helpers for tests -------------------------------------------------------
    def now(self) -> str:
        tick = next(self._clock)
        return f"2026-01-01T{tick // 3600:02d}:{tick // 60 % 60:02d}:{tick % 60:02d}Z"

    def add_issue(self, number: int, title: str = "Task", body: str = "Do it", labels=(),
                  author: str = "alice", state: str = "open") -> Dict[str, Any]:
        issue = {
            "number": number, "node_id": f"I_{number}", "title": title, "body": body,
            "state": state, "labels": [{"name": l} for l in labels], "user": {"login": author},
            "comments": 0, "html_url": f"https://github.com/{self.repository}/issues/{number}",
            "created_at": self.now(),
        }
        self.issues[number] = issue
        self.comments_by_issue.setdefault(number, [])
        self.events.setdefault(number, [])
        return issue

    def add_comment(self, number: int, login: str, body: str) -> Dict[str, Any]:
        cid = next(self._ids)
        comment = {
            "id": cid, "user": {"login": login}, "body": body, "created_at": self.now(),
            "html_url": f"https://github.com/{self.repository}/issues/{number}#issuecomment-{cid}",
            "author_association": "COLLABORATOR",
        }
        comment["updated_at"] = comment["created_at"]
        self.comments_by_issue[number].append(comment)
        self.issues[number]["comments"] += 1
        return comment

    def label_names(self, number: int) -> List[str]:
        return [l["name"] for l in self.issues[number]["labels"]]

    # client interface ----------------------------------------------------------
    def login(self) -> str:
        return self._login

    def issue(self, number: int) -> Dict[str, Any]:
        if number not in self.issues:
            raise GitHubError(404, "Not Found")
        return dict(self.issues[number], labels=list(self.issues[number]["labels"]))

    def get(self, path: str, **params: Any) -> Any:
        self.calls.append(("GET", path, params))
        if path.startswith("/issues/") and path.endswith("/comments"):
            number = int(path.split("/")[2])
            rows = self.comments_by_issue.get(number, [])
            if "since" in params:
                rows = [c for c in rows if c["updated_at"] >= params["since"]]
            per = params.get("per_page", 30)
            page = params.get("page", 1)
            return rows[(page - 1) * per: page * per]
        if path.startswith("/issues/") and path.endswith("/events"):
            number = int(path.split("/")[2])
            per = params.get("per_page", 30)
            page = params.get("page", 1)
            return self.events.get(number, [])[(page - 1) * per: page * per]
        if path.startswith("/issues/"):
            return self.issue(int(path.split("/")[2]))
        if path == "/issues":
            rows = [i for i in self.issues.values() if i["state"] == params.get("state", "open")]
            if params.get("labels"):
                wanted = set(params["labels"].split(","))
                rows = [i for i in rows if wanted <= {l["name"] for l in i["labels"]}]
            rows.sort(key=lambda i: i["number"])
            per = params.get("per_page", 30)
            page = params.get("page", 1)
            return rows[(page - 1) * per: page * per]
        if path == "/pulls":
            return [p for p in self.pulls if p["head"]["ref"] == params.get("head", "").split(":")[-1]]
        raise AssertionError(f"unexpected GET {path}")

    def paginate(self, path: str, max_pages: int = 20, **params: Any):
        params.setdefault("per_page", 100)
        for page in range(1, max_pages + 1):
            items = self.get(path, page=page, **params)
            yield from items
            if len(items) < params["per_page"]:
                return

    def comments(self, number: int, **params: Any):
        return list(self.paginate(f"/issues/{number}/comments", **params))

    def comment(self, number: int, body: str) -> Dict[str, Any]:
        self.calls.append(("COMMENT", number, body))
        return self.add_comment(number, self._login, body)

    def add_labels(self, number: int, *labels: str) -> None:
        self.calls.append(("LABEL+", number, labels))
        names = self.label_names(number)
        for label in labels:
            if label not in names:
                self.issues[number]["labels"].append({"name": label})

    def remove_label(self, number: int, label: str) -> None:
        self.calls.append(("LABEL-", number, label))
        self.issues[number]["labels"] = [l for l in self.issues[number]["labels"] if l["name"] != label]

    def ensure_label(self, name: str, color: str, description: str = "") -> bool:
        created = name not in self.labels
        self.labels[name] = {"color": color}
        return created

    def graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        if "lastEditedAt" in query:
            number = variables["number"]
            return {"repository": {"issue": {"lastEditedAt": self.last_edited.get(number)}}}
        return {}

    def request(self, method: str, path: str, data: Any = None, params: Any = None) -> Any:
        self.calls.append((method, path, data))
        if method == "GET":
            return self.get(path, **(params or {}))
        if method == "POST" and path == "/issues":
            number = max(self.issues, default=0) + 1
            issue = self.add_issue(number, data["title"], data.get("body", ""), data.get("labels", ()),
                                   author=self._login)
            return issue
        if method == "POST" and path == "/pulls":
            pr = {"number": 900 + len(self.pulls), "head": {"ref": data["head"], "sha": None},
                  "html_url": f"https://github.com/{self.repository}/pull/{900 + len(self.pulls)}",
                  "draft": data.get("draft")}
            self.pulls.append(pr)
            return pr
        if method == "PATCH" and path.startswith("/issues/"):
            number = int(path.split("/")[2])
            self.issues[number].update(data)
            return self.issues[number]
        raise AssertionError(f"unexpected {method} {path}")
