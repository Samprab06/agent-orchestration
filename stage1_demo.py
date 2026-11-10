"""Run the approved, fixed-scope Stage 1 GitHub demonstration once (Linux)."""
import argparse
from base64 import b64encode
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from github_app import InstallationCredentials, request_json

from orchestrator import approval_command, is_approved
from smoke import BanyanCode, git


REPOSITORY = "Samprab06/agent-orchestration"
APPROVER = "Samprab06"
TITLE = "Stage 1 Oracle smoke test"
BODY = "Use BanyanCode to add smoke.txt containing ORACLE_RUNNER_OK and open a draft PR."


class DemoGitHub:
    def __init__(self, credentials):
        self.credentials = credentials

    def request(self, method, path, data=None):
        return request_json(
            method, f"/repos/{self.credentials.repository}{path}", self.credentials.token(), data
        )

    def read(self, path):
        return self.request("GET", path)

    def comments(self, issue_number):
        for page in range(1, 11):
            comments = self.read(
                f"/issues/{issue_number}/comments?per_page=100&page={page}"
            )
            yield from comments
            if len(comments) < 100:
                return
        raise ValueError("approval scan exceeded 1,000 comments")


def check_task(github, number):
    issue = github.read(f"/issues/{number}")
    if issue["title"] != TITLE or (issue.get("body") or "").strip() != BODY:
        raise ValueError("Issue does not match the fixed Stage 1 demo task")
    if not is_approved(issue, github.comments(number), {APPROVER.casefold()}):
        raise ValueError(f"Issue #{number} lacks current approval from {APPROVER}")
    print(f"Approved issue: {issue['html_url']}", flush=True)


def create_issue(github):
    for label, color in (("agent:queued", "1d76db"), ("agent:review", "5319e7")):
        try:
            github.read(f"/labels/{label}")
        except HTTPError as error:
            if error.code != 404:
                raise
            github.request("POST", "/labels", {"name": label, "color": color})
    for issue in github.read("/issues?state=open&labels=agent%3Aqueued&per_page=100"):
        if "pull_request" not in issue and issue["title"] == TITLE and issue.get("body") == BODY:
            break
    else:
        issue = github.request("POST", "/issues", {
            "title": TITLE, "body": BODY, "labels": ["agent:queued"],
        })
    print(issue["html_url"])
    print(f"Post this exact comment as {APPROVER}:")
    print(approval_command(issue))


def discover_task(github):
    issues = github.read("/issues?state=open&labels=agent%3Aqueued&per_page=100")
    for issue in issues:
        if (
            "pull_request" not in issue and issue["title"] == TITLE
            and (issue.get("body") or "").strip() == BODY
            and is_approved(issue, github.comments(issue["number"]), {APPROVER.casefold()})
        ):
            return issue["number"]
    raise ValueError("No approved Stage 1 issue found among the first 100 queued issues")


@contextmanager
def runner_checkout(checkout, base):
    with tempfile.TemporaryDirectory(prefix="stage1-") as directory:
        scratch = Path(directory)
        worktree = scratch / "worktree"
        git(checkout, "worktree", "add", "--detach", str(worktree), base)
        try:
            yield scratch, worktree
        finally:
            git(checkout, "worktree", "remove", "--force", str(worktree))


def run_edit(checkout, base, runner, log, login):
    with runner_checkout(checkout, base) as (scratch, worktree):
        marker = worktree / "smoke.txt"
        config = worktree / "banyancode.json"
        with marker.open("xb") as file:
            file.write(b"before\n")
        with config.open("x", encoding="utf-8") as file:
            json.dump({
                "permission": {"*": "deny", "read": "allow", "edit": "allow"},
                "banyancode_max_subagents": 1,
                "banyancode_disable_websearch": True,
            }, file)
        config_original = config.read_bytes()
        runner.run(worktree, scratch / "home", log)
        if marker.is_symlink() or marker.read_bytes() != b"ORACLE_RUNNER_OK\n":
            raise ValueError("BanyanCode did not produce the expected marker")
        if config.is_symlink() or config.read_bytes() != config_original:
            raise ValueError("BanyanCode changed the runner configuration")
        config.unlink()
        if git(worktree, "rev-parse", "HEAD") != base:
            raise ValueError("BanyanCode moved the worktree HEAD")
        if git(worktree, "status", "--porcelain", "--untracked-files=all") != "?? smoke.txt":
            raise ValueError("Worktree contains changes outside the demo scope")
        git(worktree, "add", "--", "smoke.txt")
        git(worktree, "diff", "--cached", "--check")
        subprocess.run(
            [sys.executable, "-m", "unittest", "-v"], cwd=worktree, check=True, timeout=30
        )
        git(
            worktree, "-c", f"user.name={login}",
            "-c", f"user.email={login}@users.noreply.github.com",
            "commit", "-m", "Add Oracle runner smoke marker",
        )
        commit = git(worktree, "rev-parse", "HEAD")
        if git(worktree, "diff", "--name-status", base, commit) != "A\tsmoke.txt":
            raise ValueError("Commit contains changes outside the demo scope")
        return commit


def git_auth_env(token):
    # Git receives the credential through its environment, never through argv or a URL.
    auth = b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        "PATH": os.defpath,
        "HOME": str(Path.home()),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
    }
    return env


def push_commit(checkout, commit, branch, token, repository=REPOSITORY):
    # An empty lease requires that the remote branch does not already exist.
    ref = f"refs/heads/{branch}"
    subprocess.run(
        ["git", "-C", str(checkout), "push", f"--force-with-lease={ref}:",
         f"https://github.com/{repository}.git", f"{commit}:{ref}"],
        env=git_auth_env(token), timeout=60, check=True,
    )


def run(checkout, binary, logs, credentials, number):
    github = DemoGitHub(credentials)
    login = "oracle-agent-worker"
    if number is None:
        number = discover_task(github)
    check_task(github, number)
    branch = f"agent/stage1-issue-{number}"
    if git(checkout, "status", "--porcelain"):
        raise ValueError("Use a clean Oracle checkout")
    git(checkout, "fetch", "--no-tags", f"https://github.com/{REPOSITORY}.git", "main")
    base = git(checkout, "rev-parse", "FETCH_HEAD")
    head = git(checkout, "rev-parse", "HEAD")
    if git(checkout, "ls-remote", f"https://github.com/{REPOSITORY}.git", f"refs/heads/{branch}"):
        raise ValueError(f"Branch {branch} already exists. Check GitHub for a prior draft PR before retrying.")
    log = logs / f"issue-{number}.jsonl"
    commit = run_edit(checkout, base, BanyanCode(str(binary), "opencode/beam-free"), log, login)
    if git(checkout, "status", "--porcelain") or git(checkout, "rev-parse", "HEAD") != head:
        raise ValueError("Original checkout changed during the demo")
    check_task(github, number)
    push_commit(checkout, commit, branch, credentials.token())
    pr = github.request("POST", "/pulls", {
        "title": "Stage 1: Oracle BanyanCode smoke test",
        "head": branch,
        "base": "main",
        "body": f"Closes #{number}. BanyanCode wrote the smoke marker in an isolated worktree. Local tests passed.",
        "draft": True,
    })
    print(f"Draft PR: {pr['html_url']}", flush=True)
    github.request("POST", f"/issues/{number}/comments", {
        "body": f"Oracle verified the BanyanCode edit and opened {pr['html_url']}.",
    })
    github.request("POST", f"/issues/{number}/labels", {"labels": ["agent:review"]})
    github.request("DELETE", f"/issues/{number}/labels/agent%3Aqueued")
    print(f"Verified commit {commit}; runner log: {log}", flush=True)


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--banyancode", type=Path)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--app-id", type=int, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--issue", type=int)
    mode.add_argument("--create-issue", action="store_true")
    args = parser.parse_args()
    if not args.create_issue and (args.checkout is None or args.banyancode is None):
        parser.error("--checkout and --banyancode are required to run an approved issue")
    if args.issue is not None and args.issue < 1:
        parser.error("--issue must be positive")
    if args.app_id < 1:
        parser.error("--app-id must be positive")
    credentials = InstallationCredentials(
        REPOSITORY, args.app_id, Path.home() / ".config/agent-orchestrator/github-app.pem"
    )
    try:
        args.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (args.logs / "stage1.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.create_issue:
                github = DemoGitHub(credentials)
                credentials.token()
                create_issue(github)
            else:
                run(args.checkout.resolve(strict=True), args.banyancode.resolve(strict=True),
                    args.logs.resolve(), credentials, args.issue)
    except (OSError, ValueError, HTTPError, URLError, subprocess.SubprocessError) as error:
        print(f"Stage 1 stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
