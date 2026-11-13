import unittest

from agentorch import tasks
from tests.fakes import FakeGitHub


class ApprovalTextTests(unittest.TestCase):
    def test_words(self):
        for text in ("approved", "Approved.", " /approve ", "approve!\n\nlooks fine"):
            self.assertTrue(tasks.is_approval_text(text), text)
        for text in ("not approved", "", "approved?", "I approve this"):
            self.assertFalse(tasks.is_approval_text(text), text)


class FindApprovalTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub(login="server[bot]")
        self.issue = self.gh.add_issue(5, labels=["agent:queued"], author="bob")

    def approval(self, policy="none"):
        return tasks.find_approval(self.gh, self.gh.issue(5), ["sam"], policy, "server[bot]")

    def test_needs_approval_by_default(self):
        self.assertIsNone(self.approval())

    def test_approver_comment(self):
        self.gh.add_comment(5, "Sam", "approved")
        approval = self.approval()
        self.assertEqual((approval.by, approval.how), ("Sam", "comment"))

    def test_non_approver_comment_is_ignored(self):
        self.gh.add_comment(5, "bob", "approved")
        self.assertIsNone(self.approval())

    def test_server_identity_can_approve(self):
        self.gh.add_comment(5, "server[bot]", "approved\n\n_from Signal_")
        self.assertEqual(self.approval().by, "server[bot]")

    def test_edit_after_approval_voids_it(self):
        comment = self.gh.add_comment(5, "sam", "approved")
        self.gh.last_edited[5] = "2026-01-02T00:00:00Z"
        self.assertGreater(self.gh.last_edited[5], comment["created_at"])
        self.assertIsNone(self.approval())
        self.gh.add_comment(5, "sam", "approved")
        self.gh.comments_by_issue[5][-1]["created_at"] = "2026-01-03T00:00:00Z"
        self.assertIsNotNone(self.approval())

    def test_label_by_approver(self):
        self.gh.add_labels(5, "agent:approved")
        self.gh.events[5].append({"event": "labeled", "label": {"name": "agent:approved"},
                                  "actor": {"login": "bob"}, "created_at": "2026-01-01T01:00:00Z"})
        self.assertIsNone(self.approval())
        self.gh.events[5].append({"event": "labeled", "label": {"name": "agent:approved"},
                                  "actor": {"login": "sam"}, "created_at": "2026-01-01T02:00:00Z"})
        self.assertEqual(self.approval().how, "label")

    def test_auto_policies(self):
        self.assertEqual(self.approval("all").how, "auto")
        self.assertIsNone(self.approval("approvers"))
        self.gh.issues[5]["user"]["login"] = "sam"
        self.assertEqual(self.approval("approvers").by, "sam")

    def test_requester_marker_only_trusted_on_server_issues(self):
        self.gh.issues[5]["body"] = "x\n<!-- agentorch:requested-by=sam -->"
        self.assertIsNone(self.approval("approvers"))  # bob forged it
        self.gh.issues[5]["user"]["login"] = "server[bot]"
        self.assertEqual(self.approval("approvers").by, "sam")

    def test_server_tasks_never_auto_approve_themselves(self):
        self.gh.issues[5]["user"]["login"] = "server[bot]"
        tasks.find_approval(self.gh, self.gh.issue(5), ["server[bot]"], "approvers", "server[bot]")
        self.assertIsNone(tasks.find_approval(self.gh, self.gh.issue(5), ["server[bot]"],
                                              "approvers", "server[bot]"))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub(login="server[bot]")

    def test_create_and_approve(self):
        issue = tasks.create_task(self.gh, "  Add   login ", "details", requester="bob",
                                  source="https://x/1", approve=True)
        self.assertEqual(issue["title"], "Add login")
        self.assertIn("agent:queued", self.gh.label_names(issue["number"]))
        self.assertEqual(tasks.requested_by(self.gh.issue(issue["number"]), "server[bot]"), "bob")
        self.assertTrue(tasks.is_approval_text(self.gh.comments_by_issue[issue["number"]][-1]["body"]))

    def test_cancel_and_retry(self):
        self.gh.add_issue(3, labels=["agent:queued"])
        tasks.cancel_task(self.gh, 3)
        self.assertEqual(self.gh.issues[3]["state"], "closed")
        self.assertEqual(self.gh.label_names(3), ["agent:cancelled"])
        tasks.retry_task(self.gh, 3)
        self.assertEqual(self.gh.issues[3]["state"], "open")
        self.assertEqual(self.gh.label_names(3), ["agent:queued"])

    def test_title_required(self):
        with self.assertRaises(ValueError):
            tasks.create_task(self.gh, "   ")

    def test_fingerprint_tracks_title_and_body(self):
        issue = self.gh.add_issue(9)
        before = tasks.fingerprint(issue)
        self.assertEqual(before, tasks.fingerprint(dict(issue)))
        self.assertNotEqual(before, tasks.fingerprint(dict(issue, body="changed")))


class BoardTaskTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(tasks.parse_board_task("[Sam's Codex] /task Add a health check"),
                         {"title": "Add a health check", "body": ""})
        self.assertEqual(tasks.parse_board_task("[A's B] heads up\n/task Fix login\nIt 500s on empty email."),
                         {"title": "Fix login", "body": "It 500s on empty email."})
        self.assertEqual(tasks.parse_board_task("[A's B] /task\nTitle on next line\nbody"),
                         {"title": "Title on next line", "body": "body"})
        self.assertIsNone(tasks.parse_board_task("[A's B] mention /task in passing"))
        self.assertIsNone(tasks.parse_board_task("[A's B] /tasks list"))
        self.assertEqual(tasks.parse_board_task("[A's B] !run do it", "!run")["title"], "do it")


if __name__ == "__main__":
    unittest.main()
