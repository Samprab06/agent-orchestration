"""Read-only Stage 1 preflight for GitHub tasks."""
import argparse
import hashlib
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def approval_command(issue):
    payload = json.dumps(
        [issue["node_id"], issue["number"], issue["title"], issue.get("body") or ""],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "/agent approve " + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_approved(issue, comments, approvers):
    labels = {label["name"] for label in issue["labels"]}
    disqualifying = {
        "agent:running", "agent:blocked", "agent:review", "agent:done",
        "agent:failed", "agent:cancelled",
    }
    if (
        issue["state"] != "open" or "pull_request" in issue
        or "agent:queued" not in labels or labels & disqualifying
    ):
        return False
    expected = approval_command(issue)
    return any(
        (comment.get("user") or {}).get("login", "").casefold() in approvers
        and comment.get("author_association") in {"OWNER", "MEMBER", "COLLABORATOR"}
        and (comment.get("body") or "").strip() == expected
        for comment in comments
    )


class GitHub:
    def __init__(self, repository, token=None):
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("repository must use OWNER/REPO format")
        self.base = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "hackathon-agent-orchestrator",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def read(self, path):
        request = Request(self.base + path, headers=self.headers)
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    def comments(self, issue_number):
        for page in range(1, 11):
            comments = self.read(
                f"/issues/{issue_number}/comments?per_page=100&page={page}"
            )
            yield from comments
            if len(comments) < 100:
                return
        raise ValueError("approval scan exceeded 1,000 comments")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("issue", type=int)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.issue < 1:
        parser.error("issue number must be positive")
    approvers = {
        login.strip().casefold()
        for login in os.getenv("AGENT_APPROVERS", "").split(",")
        if login.strip()
    }
    if args.check and not approvers:
        parser.error("set AGENT_APPROVERS to the trusted GitHub logins")
    try:
        github = GitHub(args.repository, os.getenv("GH_TOKEN"))
        issue = github.read(f"/issues/{args.issue}")
        if "pull_request" in issue:
            raise ValueError("the number identifies a pull request")
        if not args.check:
            print(approval_command(issue))
            return 0
        approved = is_approved(issue, github.comments(args.issue), approvers)
        print("Eligible for execution" if approved else "Issue is ineligible")
        return 0 if approved else 1
    except (HTTPError, URLError, ValueError, TimeoutError) as error:
        print(f"GitHub preflight failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
