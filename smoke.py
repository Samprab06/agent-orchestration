"""Run a real BanyanCode edit and verification in a disposable Git worktree."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Protocol


class Runner(Protocol):
    def run(self, worktree: Path, home: Path, log: Path) -> None:
        ...


class BanyanCode:
    def __init__(self, executable, model):
        self.executable = executable
        self.model = model

    def run(self, worktree, home, log):
        home.mkdir(mode=0o700)
        env = {
            "PATH": os.defpath,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CACHE_HOME": str(home / "cache"),
        }
        prompt = (
            "Use the edit tool to replace the contents of smoke.txt with "
            "ORACLE_RUNNER_OK followed by one newline. This is a local smoke "
            "test. Do not use the shell or touch other files."
        )
        command = [
            "timeout", "--kill-after=5s", "90s", self.executable,
            "--standalone", "run", "--format", "json", "--model", self.model, prompt,
        ]
        with log.open("w", encoding="utf-8") as output:
            subprocess.run(
                command, cwd=worktree, env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, check=True, timeout=100,
            )


def git(directory, *args):
    return subprocess.check_output(
        ["git", "-C", str(directory), *args],
        text=True, stderr=subprocess.STDOUT, timeout=30,
    ).strip()


def smoke_test(runner: Runner, log: Path):
    with tempfile.TemporaryDirectory(prefix="orchestrator-smoke-") as directory:
        scratch = Path(directory)
        base = scratch / "repo"
        base.mkdir()
        git(base, "init", "--initial-branch=main")
        git(base, "config", "user.name", "Orchestrator Smoke Test")
        git(base, "config", "user.email", "smoke@example.invalid")
        (base / "smoke.txt").write_bytes(b"before\n")
        (base / "banyancode.json").write_text(json.dumps({
            "permission": {"*": "deny", "read": "allow", "edit": "allow"},
            "banyancode_max_subagents": 1,
            "banyancode_disable_websearch": True,
        }), encoding="utf-8")
        git(base, "add", "--", "smoke.txt", "banyancode.json")
        git(base, "commit", "-m", "Initialize smoke fixture")
        initial_commit = git(base, "rev-parse", "HEAD")

        worktree = scratch / "worktree"
        git(base, "worktree", "add", "-b", "agent/issue-1", str(worktree))
        runner.run(worktree, scratch / "home", log)

        if (worktree / "smoke.txt").read_bytes() != b"ORACLE_RUNNER_OK\n":
            raise RuntimeError("runner did not produce the expected file contents")
        if git(worktree, "status", "--porcelain", "--untracked-files=all") != "M smoke.txt":
            raise RuntimeError("runner changed files outside the fixture scope")
        if git(worktree, "rev-parse", "HEAD") != initial_commit:
            raise RuntimeError("runner moved the task branch")
        git(worktree, "diff", "--check")
        git(worktree, "add", "--", "smoke.txt")
        git(worktree, "commit", "-m", "Verify coding agent smoke edit")
        if git(base, "status", "--porcelain") or git(base, "rev-parse", "HEAD") != initial_commit:
            raise RuntimeError("base checkout changed")
        return git(worktree, "rev-parse", "HEAD")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--banyancode", type=Path, required=True)
    parser.add_argument("--model", default="opencode/beam-free")
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    runner = BanyanCode(str(args.banyancode.resolve(strict=True)), args.model)
    commit = smoke_test(runner, args.log.resolve())
    print(f"Verified isolated edit and commit {commit}")
    print(f"Runner output saved at {args.log.resolve()}")


if __name__ == "__main__":
    main()
