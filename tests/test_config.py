import unittest

from agentorch.config import Config, ConfigError, parse_remote


class RemoteTests(unittest.TestCase):
    def test_parse_remote_forms(self):
        self.assertEqual(parse_remote("git@github.com:acme/app.git"), "acme/app")
        self.assertEqual(parse_remote("https://github.com/acme/app"), "acme/app")
        self.assertEqual(parse_remote("ssh://git@github.com/acme/app.git/"), "acme/app")
        self.assertIsNone(parse_remote("not a url"))


class ConfigTests(unittest.TestCase):
    def base(self, **extra):
        data = {
            "approvers": ["Sam"],
            "repositories": [{"repository": "acme/app", "branch": "main", "board_issue": 1}],
        }
        data.update(extra)
        return data

    def test_defaults(self):
        config = Config(self.base())
        repo = config.repository("ACME/app")
        self.assertEqual(repo.branch, "main")
        self.assertEqual(repo.auto_approve, "none")
        self.assertEqual(config.approvers, {"sam"})
        self.assertEqual(config.notify_mentions, {"sam"})
        self.assertEqual(config.agent["type"], "banyancode")

    def test_repo_overrides(self):
        data = self.base(auto_approve="approvers", agent={"type": "codex", "timeout": 600})
        data["repositories"][0].update(auto_approve="all", verify=["npm", "test"],
                                       agent={"model": "x"})
        repo = Config(data).repository(None)
        self.assertEqual(repo.auto_approve, "all")
        self.assertEqual(repo.verify, ["npm", "test"])
        self.assertEqual(repo.agent, {"type": "codex", "timeout": 600, "model": "x"})

    def test_rejections(self):
        bad = [
            self.base(github={"auth": "app"}),
            self.base(approvers=["not a login"]),
            self.base(auto_approve="sometimes"),
            self.base(agent={"type": "mystery"}),
            self.base(notify=[{"type": "pager"}]),
            self.base(repositories=[{"repository": "noslash"}]),
            self.base(repositories=[{"repository": "a/b", "branch": "-x"}]),
            self.base(repositories=[{"repository": "a/b", "verify": "make test"}]),
            self.base(repositories=[{"repository": "a/b", "surprise": 1}]),
            self.base(repositories=[{"repository": "a/b"}, {"repository": "A/B"}]),
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ConfigError):
                Config(data)

    def test_repository_selection(self):
        data = self.base()
        data["repositories"].append({"repository": "acme/other"})
        config = Config(data)
        with self.assertRaises(ConfigError):
            config.repository(None)
        with self.assertRaises(ConfigError):
            config.repository("acme/missing")


if __name__ == "__main__":
    unittest.main()
