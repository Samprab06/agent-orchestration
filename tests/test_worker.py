"""End-to-end worker runs against local git repositories and a fake GitHub."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agentorch.config import Config
from agentorch.sandbox import Runner
from agentorch.worker import RepoWorker, format_status, git
from tests.fakes import FakeGitHub

AGENT = r"""
import os, sys, time, pathlib
mode = os.environ.get("FAKE_AGENT_MODE", "write")
if mode == "write":
    pathlib.Path("hello.txt").write_text("hi\n")
elif mode == "workflow":
    pathlib.Path(".github/workflows").mkdir(parents=True, exist_ok=True)
    pathlib.Path(".github/workflows/x.yml").write_text("name: x\n")
elif mode == "slow":
    time.sleep(3)
    pathlib.Path("hello.txt").write_text("hi\n")
elif mode == "fail":
    sys.exit(3)
"""


def sh(cwd, *args):
    return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.STDOUT).strip()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        seed = root / "seed"
        seed.mkdir()
        sh(seed, "git", "init", "-q", "-b", "main")
        (seed / "README.md").write_text("app\n")
        sh(seed, "git", "add", ".")
        sh(seed, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        sh(root, "git", "clone", "-q", "--bare", str(seed), str(self.origin))
        script = root / "agent.py"
        script.write_text(AGENT)
        self.config = Config({
            "approvers": ["sam"],
            "identity": {"human": "Sam", "agent": "Server"},
            "agent": {"type": "command", "argv": [sys.executable, str(script), "{prompt}"],
                      "env": ["FAKE_AGENT_MODE"], "timeout": 30},
            "sandbox": {"type": "none", "verify_timeout": 30},
            "worker": {"state_dir": str(root / "state"), "poll": 10},
            "repositories": [{
                "repository": "acme/app", "branch": "main", "board_issue": 1,
                "verify": [sys.executable, "-c",
                           "import pathlib; assert pathlib.Path('hello.txt').read_text() == 'hi\\n'"],
            }],
        })
        self.gh = FakeGitHub("acme/app", login="server[bot]")
        self.gh.add_issue(1, "Message Board", labels=["board"])
        self.events = []
        self.stop = threading.Event()
        os.environ["FAKE_AGENT_MODE"] = "write"
        self.addCleanup(os.environ.pop, "FAKE_AGENT_MODE", None)
        self.worker = self.make_worker()

    def make_worker(self):
        worker = RepoWorker(self.config, self.config.repository(None), self.stop,
                            lambda e, m: self.events.append((e, m)), github=self.gh, runner=Runner("none"))
        worker.remote = str(self.origin)
        worker.git_env = lambda: dict(os.environ, GIT_TERMINAL_PROMPT="0")
        self.addCleanup(worker.state.close)
        return worker

    def queue(self, number=5, approve=True, author="bob"):
        self.gh.add_issue(number, "Say hi", "Create hello.txt", labels=["agent:queued"], author=author)
        if approve:
            self.gh.add_comment(number, "sam", "approved")

    def run_tick(self):
        self.worker.prepare()
        self.worker.tick()

    def event_names(self):
        return [e for e, _ in self.events]

    def test_approved_task_becomes_draft_pr(self):
        self.queue()
        self.run_tick()
        task = self.worker.state.get(5)
        self.assertEqual(task["phase"], "review", task.get("error"))
        self.assertTrue(task["reported"])
        self.assertEqual(self.gh.label_names(5), ["agent:review"])
        branch = sh(self.origin, "git", "show", "agent/issue-5:hello.txt")
        self.assertEqual(branch, "hi")
        self.assertTrue(self.gh.pulls[0]["draft"])
        board = [c["body"] for c in self.gh.comments_by_issue[1]]
        self.assertTrue(board[-1].startswith("[Sam's Server] Draft PR for #5"))
        self.assertIn("@bob", board[-1])
        self.assertEqual(self.event_names(), ["task_started", "task_review"])
        self.assertFalse((self.worker.attempt_dir(task) / "worktree").exists())
        self.assertIn("REVIEW", format_status([{"repository": "acme/app", "tasks": [task], "queue": []}]))

    def test_unapproved_task_notifies_once(self):
        self.queue(approve=False)
        self.run_tick()
        self.run_tick()
        self.assertIsNone(self.worker.state.get(5))
        self.assertEqual(self.event_names(), ["task_waiting"])
        self.gh.add_comment(5, "sam", "approved")
        self.run_tick()
        self.assertEqual(self.worker.state.get(5)["phase"], "review")

    def test_no_changes_fails_and_retry_starts_new_attempt(self):
        os.environ["FAKE_AGENT_MODE"] = "noop"
        self.queue()
        self.run_tick()
        task = self.worker.state.get(5)
        self.assertEqual(task["phase"], "failed")
        self.assertIn("no changes", task["error"])
        self.assertEqual(self.gh.label_names(5), ["agent:failed"])
        self.assertIn("task_failed", self.event_names())
        os.environ["FAKE_AGENT_MODE"] = "write"
        self.gh.add_labels(5, "agent:queued")
        self.run_tick()
        task = self.worker.state.get(5)
        self.assertEqual((task["phase"], task["attempt"]), ("review", 2))
        sh(self.origin, "git", "rev-parse", "agent/issue-5-attempt-2")

    def test_agent_error_and_workflow_changes_fail(self):
        for number, mode, text in ((5, "fail", "status 3"), (6, "workflow", ".github")):
            os.environ["FAKE_AGENT_MODE"] = mode
            self.queue(number)
            self.run_tick()
            task = self.worker.state.get(number)
            self.assertEqual(task["phase"], "failed")
            self.assertIn(text, task["error"])

    def test_verification_failure(self):
        self.config.repositories["acme/app"].verify = [sys.executable, "-c", "raise SystemExit(1)"]
        self.queue()
        self.run_tick()
        self.assertIn("status 1", self.worker.state.get(5)["error"])

    def test_edit_before_publish_blocks_push(self):
        self.queue()
        self.worker.prepare()
        task = self.worker.run(self.gh.issue(5), __import__("agentorch.tasks").tasks.Approval("sam", "", "comment"))
        self.assertEqual(task["phase"], "publish")
        self.gh.issues[5]["body"] = "Something else entirely"
        self.worker.publish(task)
        self.assertEqual(self.worker.state.get(5)["phase"], "failed")
        self.assertEqual(sh(self.origin, "git", "branch", "--list", "agent/*"), "")

    def test_publish_resumes_after_restart_without_second_pr(self):
        self.queue()
        self.worker.prepare()
        from agentorch.tasks import Approval
        task = self.worker.run(self.gh.issue(5), Approval("sam", "", "comment"))
        self.worker.publish(task)
        self.assertEqual(len(self.gh.pulls), 1)
        # Simulate a crash after the PR was opened but before the state was saved.
        self.worker.state.save(task, phase="publish", pr=None)
        restarted = self.make_worker()
        restarted.publish(restarted.state.get(5))
        self.assertEqual(len(self.gh.pulls), 1)
        self.assertEqual(restarted.state.get(5)["phase"], "review")

    def test_closing_the_issue_cancels_a_running_attempt(self):
        os.environ["FAKE_AGENT_MODE"] = "slow"
        self.queue()
        original = self.worker.stopper

        def fast_stopper(number):
            stop = original(number)
            self.worker._closed_checked = -1e9
            return lambda: (setattr(self.worker, "_closed_checked", -1e9), stop())[1]
        self.worker.stopper = fast_stopper
        timer = threading.Timer(0.8, lambda: self.gh.issues[5].update(state="closed"))
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        self.run_tick()
        self.assertLess(time.monotonic() - started, 3)
        task = self.worker.state.get(5)
        self.assertEqual(task["phase"], "cancelled")
        self.assertIn("agent:cancelled", self.gh.label_names(5))

    def test_recover_marks_running_as_interrupted(self):
        self.worker.state.save({"issue": 5, "attempt": 1, "title": "x"}, phase="running")
        self.make_worker().recover()
        self.assertEqual(self.worker.state.get(5)["phase"], "interrupted")

    def test_board_task_requests(self):
        self.gh.add_comment(1, "old", "[Old's Bot] /task ancient history")
        self.run_tick()  # first pass only marks history as read
        self.assertEqual([i for i in self.gh.issues if i > 1], [])
        post = self.gh.add_comment(1, "bob", "[Bob's Codex] /task Add hello\nCreate hello.txt")
        stranger = self.gh.add_comment(1, "eve", "[Eve's Bot] /task Delete everything")
        stranger["author_association"] = "NONE"
        self.run_tick()
        created = [i for i in self.gh.issues.values() if i["number"] > 1]
        self.assertEqual([i["title"] for i in created], ["Add hello"])
        self.assertIn(post["html_url"], created[0]["body"])
        replies = [c["body"] for c in self.gh.comments_by_issue[1] if c["user"]["login"] == "server[bot]"]
        self.assertIn("waiting for approval", replies[0])
        self.assertIn("task_waiting", self.event_names())

    def test_board_mentions_notify_the_operator(self):
        self.run_tick()
        self.gh.add_comment(1, "bob", "[Bob's Codex] @sam the API is merged")
        self.gh.add_comment(1, "bob", "[Bob's Codex] note to self")
        self.run_tick()
        mentions = [m for e, m in self.events if e == "board_mention"]
        self.assertEqual(len(mentions), 1)
        self.assertIn("API is merged", mentions[0])


def bwrap_works():
    import shutil
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-user", "--", "true"],
                          capture_output=True).returncode == 0


@unittest.skipUnless(bwrap_works(), "bubblewrap unavailable")
class BubblewrapTests(WorkerTests):
    """The same end-to-end flow inside the bubblewrap sandbox."""

    def make_worker(self):
        worker = super().make_worker()
        python_home = os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))
        worker.runner = Runner("bwrap", [str(Path(self.tmp.name)), python_home])
        return worker

    def test_verification_has_no_network(self):
        self.config.repositories["acme/app"].verify = [
            sys.executable, "-c",
            "import socket\ntry:\n socket.create_connection(('1.1.1.1', 53), 2)\nexcept OSError:\n raise SystemExit(0)\nraise SystemExit(9)",
        ]
        self.queue()
        self.run_tick()
        self.assertEqual(self.worker.state.get(5)["phase"], "review", self.worker.state.get(5).get("error"))

    def test_agent_cannot_write_outside_worktree(self):
        outside = Path(self.tmp.name) / "outside.txt"
        script = Path(self.tmp.name) / "agent.py"
        script.write_text(AGENT + f"\nopen({str(outside)!r}, 'w').write('x')\n")
        self.queue()
        self.run_tick()
        self.assertFalse(outside.exists())
        self.assertEqual(self.worker.state.get(5)["phase"], "failed")


if __name__ == "__main__":
    unittest.main()
