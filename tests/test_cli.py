import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from agentorch.cli import main
from agentorch.config import Config
from agentorch.github import GitHubError


class RepoCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.path.write_text(json.dumps({"approvers": ["sam"], "github": {"auth": "token"},
                                         "repositories": []}))
        os.environ["GH_TOKEN"] = "dummy"
        self.addCleanup(os.environ.pop, "GH_TOKEN", None)
        no_network = patch("agentorch.cli.GitHub.get", side_effect=GitHubError(404, "Not Found"))
        no_network.start()
        self.addCleanup(no_network.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(self.path), *args])
        return code, out.getvalue() + err.getvalue()

    def test_add_set_list_remove(self):
        code, out = self.run_cli("repo", "add", "acme/app", "--verify", "npm test", "--board-issue", "3")
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING", out)  # cannot reach the repo with the fake credentials
        config = Config.load(self.path)
        repo = config.repository("acme/app")
        self.assertEqual((repo.branch, repo.verify, repo.board_issue), ("main", ["npm", "test"], 3))
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.run_cli("repo", "add", "acme/app")[0], 1)
        self.run_cli("repo", "set", "ACME/app", "--branch", "dev", "--verify", "", "--auto-approve", "approvers")
        repo = Config.load(self.path).repository("acme/app")
        self.assertEqual((repo.branch, repo.verify, repo.auto_approve), ("dev", None, "approvers"))
        self.assertIn("acme/app", self.run_cli("repo", "list")[1])
        self.assertEqual(self.run_cli("repo", "remove", "acme/app")[0], 0)
        self.assertEqual(Config.load(self.path).repositories, {})

    def test_invalid_values_leave_the_file_alone(self):
        before = self.path.read_text()
        self.assertEqual(self.run_cli("repo", "add", "not-a-repo")[0], 1)
        self.assertEqual(self.run_cli("repo", "add", "a/b", "--branch=-bad")[0], 1)
        self.assertEqual(self.path.read_text(), before)

    def test_worker_runs_with_no_repositories(self):
        data = json.loads(self.path.read_text())
        data["worker"] = {"state_dir": str(Path(self.tmp.name) / "state")}
        self.path.write_text(json.dumps(data))
        code, _ = self.run_cli("worker", "serve", "--once")
        self.assertEqual(code, 0)
        self.assertIn("repositories: none yet",
                      (Path(self.tmp.name) / "state" / "worker.log").read_text())


if __name__ == "__main__":
    unittest.main()
