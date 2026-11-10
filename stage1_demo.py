"""Run the approved, fixed-scope Stage 1 GitHub demonstration once (Linux)."""
import argparse
from base64 import b64encode
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from orchestrator import GitHub, approval_command, is_approved
from smoke import BanyanCode, git


REPOSITORY = "Samprab06/agent-orchestration"
APPROVER = "Samprab06"
TITLE = "Stage 1 Oracle smoke test"
BODY = "Use BanyanCode to add smoke.txt containing ORACLE_RUNNER_OK and open a draft PR."


class DemoGitHub(GitHub):
    def request(self, method, path, data=None, account=False):
        url = ("https://api.github.com" if account else self.base) + path
        headers = {key: value for key, value in self.headers.items() if key != "Authorization"}
        body = None if data is None else json.dumps(data).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        request.add_unredirected_header("Authorization", self.headers["Authorization"])
        with urlopen(request, timeout=20) as response:
            return json.load(response)

    def read(self, path):
        return self.request("GET", path)


def verify_worker(github):
    try:
        login = github.request("GET", "/user", account=True)["login"]
    except HTTPError as error:
        if error.code != 403:
            raise
        installation = github.request("GET", "/installation/repositories", account=True)
        repos = [repo["full_name"].casefold() for repo in installation["repositories"]]
        if installation["total_count"] != 1 or repos != [REPOSITORY.casefold()]:
            raise ValueError("The worker app token must be scoped to this repository only")
        return "oracle-agent-worker"
    if login.casefold() == APPROVER.casefold():
        raise ValueError("The demo requires a worker credential separate from Samprab06")
    return login


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


def push_commit(checkout, commit, branch, token):
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
    # An empty lease requires that the remote branch does not already exist.
    ref = f"refs/heads/{branch}"
    subprocess.run(
        ["git", "-C", str(checkout), "push", f"--force-with-lease={ref}:",
         f"https://github.com/{REPOSITORY}.git", f"{commit}:{ref}"],
        env=env, timeout=60, check=True,
    )


def run(checkout, binary, logs, token, number):
    github = DemoGitHub(REPOSITORY, token)
    login = verify_worker(github)
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
    push_commit(checkout, commit, branch, token)
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
    print(f"Verified commit {commit}; runner log: {log}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--banyancode", type=Path)
    parser.add_argument("--logs", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--issue", type=int)
    mode.add_argument("--create-issue", action="store_true")
    args = parser.parse_args()
    if not args.create_issue and (args.checkout is None or args.banyancode is None):
        parser.error("--checkout and --banyancode are required to run an approved issue")
    if args.issue is not None and args.issue < 1:
        parser.error("--issue must be positive")
    token_file = Path.home() / ".config/agent-orchestrator/github-token"
    try:
        if token_file.stat().st_mode & 0o077:
            raise ValueError("Worker token file must be accessible only to its owner")
        token = token_file.read_text().strip()
        if not token:
            raise ValueError("Worker token file is empty")
        args.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (args.logs / "stage1.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.create_issue:
                github = DemoGitHub(REPOSITORY, token)
                verify_worker(github)
                create_issue(github)
            else:
                run(args.checkout.resolve(strict=True), args.banyancode.resolve(strict=True),
                    args.logs.resolve(), token, args.issue)
    except (OSError, ValueError, HTTPError, URLError, subprocess.SubprocessError) as error:
        print(f"Stage 1 stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
