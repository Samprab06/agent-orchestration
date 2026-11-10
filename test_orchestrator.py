import unittest

from orchestrator import approval_command, is_approved


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.issue = {
            "number": 1,
            "node_id": "I_test",
            "state": "open",
            "title": "Smoke test",
            "body": "Change one file.",
            "labels": [{"name": "agent:queued"}],
        }
        self.comment = {
            "body": approval_command(self.issue),
            "author_association": "COLLABORATOR",
            "user": {"login": "Trusted"},
        }

    def eligible(self, comments=None):
        return is_approved(
            self.issue, [self.comment] if comments is None else comments, {"trusted"}
        )

    def test_accepts_exact_approval_from_allowlisted_collaborator(self):
        self.assertTrue(self.eligible())

    def test_label_alone_does_not_approve(self):
        self.assertFalse(self.eligible([]))

    def test_rejects_other_collaborators(self):
        self.comment["user"]["login"] = "other"
        self.assertFalse(self.eligible())

    def test_rejects_former_collaborators(self):
        self.comment["author_association"] = "NONE"
        self.assertFalse(self.eligible())

    def test_editing_issue_invalidates_approval(self):
        self.issue["body"] += " Then do something else."
        self.assertFalse(self.eligible())

    def test_rejects_inactive_tasks_even_with_queue_label(self):
        for state in ("running", "blocked", "review", "done", "failed", "cancelled"):
            with self.subTest(state=state):
                self.issue["labels"].append({"name": f"agent:{state}"})
                self.assertFalse(self.eligible())
                self.issue["labels"].pop()
        self.issue["state"] = "closed"
        self.assertFalse(self.eligible())


if __name__ == "__main__":
    unittest.main()
