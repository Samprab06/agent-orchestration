import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentorch.board import Board, mentioned_logins, mentions, resolve_issue
from tests.fakes import FakeGitHub


class MentionTests(unittest.TestCase):
    def test_mentions_match_whole_logins_only(self):
        self.assertTrue(mentions("hey @sam can you look", ["sam"]))
        self.assertTrue(mentions("@ALL heads up", ["sam", "all"]))
        self.assertFalse(mentions("hey @samuel", ["sam"]))
        self.assertFalse(mentions("email sam@example.com", ["sam"]))
        self.assertTrue(mentions("(@sam)", ["sam"]))

    def test_mentioned_logins_deduplicates(self):
        self.assertEqual(mentioned_logins("@a and @b then @A"), ["a", "b"])


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gh = FakeGitHub(login="me")
        self.gh.add_issue(1, "Message Board", labels=["board"])
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("AGENTORCH_HANDLES", "AGENTORCH_LOGIN", "AGENTORCH_SIGNATURE",
                     "AGENTORCH_BOARD_CURSOR", "AGENTORCH_BOARD_ISSUE", "BOARD_ISSUE"):
            os.environ.pop(name, None)

    def board(self, cursor=None, login="me"):
        return Board(self.gh, 1, login=login, cursor=cursor, state_dir=Path(self.tmp.name))

    def test_first_unread_shows_recent_context_and_skips_own_posts(self):
        for i in range(12):
            self.gh.add_comment(1, "bob", f"[Bob's Codex] post {i}")
        self.gh.add_comment(1, "me", "[Me's Claude] mine")
        posts = self.board().unread()
        self.assertEqual(len(posts), 9)  # last 10 minus my own
        self.assertEqual(posts[-1].body, "[Bob's Codex] post 11")

    def test_unread_is_incremental(self):
        self.gh.add_comment(1, "bob", "[Bob's Codex] one")
        board = self.board()
        self.assertEqual(len(board.unread()), 1)
        self.assertEqual(board.unread(), [])
        self.gh.add_comment(1, "bob", "[Bob's Codex] @me two")
        posts = board.unread()
        self.assertEqual([p.body for p in posts], ["[Bob's Codex] @me two"])
        self.assertTrue(posts[0].mention)

    def test_cursors_are_independent(self):
        self.gh.add_comment(1, "bob", "[Bob's Codex] hi")
        self.assertEqual(len(self.board("a").unread()), 1)
        self.assertEqual(len(self.board("b").unread()), 1)
        self.assertEqual(self.board("a").unread(), [])

    def test_post_requires_signature_or_adds_it(self):
        board = self.board()
        with self.assertRaises(ValueError):
            board.post("no signature")
        post = board.post("done with login", signature="Me's Claude")
        self.assertEqual(post.body, "[Me's Claude] done with login")
        post = board.post("[Me's Claude] already signed")
        self.assertEqual(post.body, "[Me's Claude] already signed")

    def test_wait_returns_new_posts_and_respects_mentions_only(self):
        board = self.board()
        board.unread()
        ticks = []

        def sleep(_):
            ticks.append(1)
            if len(ticks) == 1:
                self.gh.add_comment(1, "bob", "[Bob's Codex] not for you")
            else:
                self.gh.add_comment(1, "bob", "[Bob's Codex] @me please")
        posts = board.wait(interval=0, mentions_only=True, sleep=sleep)
        self.assertEqual(posts[0].body, "[Bob's Codex] @me please")

    def test_wait_times_out(self):
        board = self.board()
        board.unread()
        with self.assertRaises(TimeoutError):
            board.wait(interval=0, timeout=0.01, sleep=lambda _: None)

    def test_bot_login_is_mentioned_without_suffix(self):
        self.gh.add_comment(1, "bob", "[Bob's Codex] @server-agent run this")
        posts = self.board(login="server-agent[bot]").unread()
        self.assertTrue(posts[0].mention)


class ResolveIssueTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        for name in ("AGENTORCH_BOARD_ISSUE", "BOARD_ISSUE"):
            os.environ.pop(name, None)

    @patch("agentorch.board.project_settings", return_value={})
    def test_label_lookup(self, _):
        self.gh.add_issue(4, "Message Board", labels=["board"])
        self.assertEqual(resolve_issue(self.gh), 4)

    @patch("agentorch.board.project_settings", return_value={"board_issue": 7})
    def test_project_file_wins_over_label(self, _):
        self.gh.add_issue(4, "Message Board", labels=["board"])
        self.assertEqual(resolve_issue(self.gh), 7)

    @patch("agentorch.board.project_settings", return_value={})
    def test_env_wins(self, _):
        with patch.dict(os.environ, {"AGENTORCH_BOARD_ISSUE": "12"}):
            self.assertEqual(resolve_issue(self.gh), 12)

    @patch("agentorch.board.project_settings", return_value={})
    def test_missing_board(self, _):
        with self.assertRaises(ValueError):
            resolve_issue(self.gh)


if __name__ == "__main__":
    unittest.main()
