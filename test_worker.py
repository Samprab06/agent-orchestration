import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError

from orchestrator import approval_command
from worker import State, Worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = State(self.root)
        self.addCleanup(self.state.db.close)
        self.worker = Worker.__new__(Worker)
        self.worker.state = self.state
        self.worker.root = self.root
        self.worker.checkout = self.root
        self.worker.stop = Mock()
        self.worker.stop.is_set.return_value = False
        self.worker.args = SimpleNamespace(poll=45, publish_attempts=2)
        self.worker.github = Mock()
        self.worker.credentials = Mock()
        self.worker.skipped = set()
        self.issue = {"number": 42, "node_id": "I_example", "state": "open",
                      "title": "Example", "body": "Approved task", "labels": [{"name": "agent:queued"}]}
        self.digest = approval_command(self.issue)
        self.task = {"issue": 42, "attempt": 1, "approval": self.digest,
                     "phase": "publish", "commit": "123", "failures": 0, "next_try": 0}

    def test_recovery_keeps_uncommitted_files_and_does_not_rerun(self):
        path = self.root / "issue-42/attempt-1/worktree/edit.txt"
        path.parent.mkdir(parents=True)
        path.write_text("uncommitted")
        self.state.save(self.task, phase="running", commit=None)
        restarted = State(self.root)
        self.addCleanup(restarted.db.close)
        self.worker.state = restarted
        self.worker.recover()
        self.assertEqual(restarted.get(42)["phase"], "interrupted")
        self.assertEqual(path.read_text(), "uncommitted")

    def test_restart_reuses_existing_pull_request_without_push(self):
        self.state.save(self.task)
        self.worker.github.read.return_value = [{"head": {"sha": "123"}, "html_url": "existing-pr"}]
        with patch("worker.push_commit") as push:
            self.worker.publish(self.state.get(42))
        push.assert_not_called()
        self.worker.github.request.assert_not_called()
        self.assertEqual(self.state.get(42)["phase"], "review")
        self.assertEqual(self.state.get(42)["pr"], "existing-pr")

    def test_retry_after_branch_push_creates_pr_without_repeating_edit(self):
        self.state.save(self.task)
        self.worker.github.read.return_value = []
        self.worker.github.request.return_value = {"html_url": "new-pr"}
        self.worker.issue = Mock(return_value=self.issue)
        with patch("worker.git", return_value="123 refs/heads/agent/issue-42"), patch("worker.push_commit") as push:
            self.worker.publish(self.state.get(42))
        push.assert_not_called()
        self.worker.github.request.assert_called_once()
        self.assertEqual(self.state.get(42)["pr"], "new-pr")

    def test_publishing_rechecks_current_approval(self):
        self.state.save(self.task)
        self.worker.github.read.return_value = []
        self.worker.issue = Mock(return_value=dict(self.issue, body="Different task"))
        with patch("worker.push_commit") as push:
            self.worker.publish(self.state.get(42))
        push.assert_not_called()
        self.worker.github.request.assert_not_called()
        self.assertEqual(self.state.get(42)["phase"], "failed")

    def test_publication_failures_back_off_then_stop(self):
        self.state.save(self.task)
        self.worker.github.read.side_effect = URLError("transient")
        self.worker.publish(self.state.get(42))
        self.assertGreater(self.state.get(42)["next_try"], 0)
        self.assertEqual(self.state.get(42)["phase"], "publish")
        self.worker.publish(self.state.get(42))
        self.assertEqual(self.state.get(42)["phase"], "failed")
        self.assertEqual(self.state.get(42)["failures"], 2)

    def test_unapproved_issue_never_creates_a_task(self):
        self.worker.github.read.side_effect = [[self.issue], self.issue]
        self.worker.github.comments.return_value = [{
            "user": {"login": "samprab06-agent-worker[bot]"},
            "author_association": "OWNER", "body": self.digest,
        }]
        self.worker.edit = Mock()
        self.worker.tick()
        self.worker.edit.assert_not_called()
        self.assertIsNone(self.state.get(42))
