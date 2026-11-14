"""Linear orchestrator, planner and board against an in-memory Linear."""
import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from agentorch import planner
from agentorch.config import Config
from agentorch.dispatch import Orchestrator
from agentorch.linear import LinearBoard, blockers, branch_name
from agentorch.sandbox import Runner
from tests.fakes import FakeGitHub
from tests.test_worker import AGENT, sh

STATES = [
    {"id": "s-backlog", "name": "Backlog", "type": "backlog", "position": 0},
    {"id": "s-todo", "name": "Todo", "type": "unstarted", "position": 1},
    {"id": "s-progress", "name": "In Progress", "type": "started", "position": 2},
    {"id": "s-review", "name": "In Review", "type": "started", "position": 3},
    {"id": "s-done", "name": "Done", "type": "completed", "position": 4},
]


class FakeLinear:
    team_key = "ENG"

    def __init__(self):
        self.issues_by_id = {}
        self.comment_log = []
        self.relations = []
        self._n = itertools.count(1)
        self._clock = itertools.count(1)
        self.lock = threading.Lock()

    def now(self):
        return f"2026-01-01T00:00:{next(self._clock):02d}.000Z"

    def viewer(self):
        return {"id": "u1", "name": "Sam", "displayName": "sam"}

    def state(self, kind, name=None):
        for s in STATES:
            if name and s["name"].lower() == name.lower():
                return s
        return next((s for s in STATES if s["type"] == kind), None)

    def label_id(self, name, create=True, color=""):
        return "l-" + name

    def create_issue(self, title, description="", labels=(), state_kind="unstarted", priority=None):
        n = next(self._n)
        issue = {"id": f"id-{n}", "identifier": f"ENG-{n}", "title": title, "description": description,
                 "url": f"https://linear.app/x/issue/ENG-{n}", "priority": priority or 0,
                 "createdAt": self.now(), "state": dict(self.state(state_kind)),
                 "labels": {"nodes": [{"id": "l-" + l, "name": l} for l in labels]}, "comments": []}
        self.issues_by_id[issue["id"]] = issue
        return issue

    def _find(self, ref):
        for issue in self.issues_by_id.values():
            if ref in (issue["id"], issue["identifier"]):
                return issue
        raise ValueError(ref)

    def _view(self, issue):
        view = dict(issue)
        view["inverseRelations"] = {"nodes": [
            {"type": "blocks", "issue": {"identifier": self._find(a)["identifier"],
                                         "state": {"type": self._find(a)["state"]["type"]}}}
            for a, b in self.relations if b == issue["id"]]}
        return view

    def issue(self, ref):
        return self._view(self._find(ref))

    def issues(self, label=None, state_types=("unstarted",), first=50):
        rows = [self._view(i) for i in self.issues_by_id.values()
                if (not state_types or i["state"]["type"] in state_types)
                and (not label or any(l["name"] == label for l in i["labels"]["nodes"]))]
        return sorted(rows, key=lambda i: (i["priority"] or 5, i["createdAt"]))

    def move(self, issue, kind, name=None):
        state = self.state(kind, name)
        self._find(issue["id"])["state"] = dict(state)
        return state["name"]

    def set_labels(self, issue, add=(), remove=()):
        real = self._find(issue["id"])
        names = [l["name"] for l in real["labels"]["nodes"] if l["name"] not in remove] + list(add)
        real["labels"]["nodes"] = [{"id": "l-" + n, "name": n} for n in dict.fromkeys(names)]

    def comment(self, issue_id, body):
        with self.lock:
            c = {"id": f"c{len(self.comment_log)}", "body": body, "createdAt": self.now(),
                 "url": "u", "user": {"name": "Sam", "displayName": "sam"}}
            self._find(issue_id)["comments"].append(c)
            self.comment_log.append((self._find(issue_id)["identifier"], body))
        return c

    def comments(self, issue_id, since=None, first=100):
        rows = self._find(issue_id)["comments"]
        return [c for c in rows if not since or c["createdAt"] > since]

    def block(self, blocker_id, blocked_id):
        self.relations.append((blocker_id, blocked_id))


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        seed = root / "seed"
        seed.mkdir()
        sh(seed, "git", "init", "-q", "-b", "main")
        (seed / "README.md").write_text("app\n")
        sh(seed, "git", "add", ".")
        sh(seed, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.origin = root / "origin.git"
        sh(root, "git", "clone", "-q", "--bare", str(seed), str(self.origin))
        script = root / "agent.py"
        script.write_text(AGENT)
        agent = {"type": "command", "argv": [sys.executable, str(script), "{prompt}"],
                 "env": ["FAKE_AGENT_MODE"], "timeout": 30}
        self.config = Config({
            "identity": {"human": "Sam", "agent": "Oracle"},
            "linear": {"team": "ENG", "board_issue": "ENG-1"},
            "subagents": {"oracle-backend": agent, "oracle-frontend": agent},
            "sandbox": {"type": "none"},
            "worker": {"state_dir": str(root / "state"), "max_parallel": 2},
            "repositories": [{"repository": "acme/app"}],
        })
        self.linear = FakeLinear()
        self.board = self.linear.create_issue("Agent Board", labels=["board"], state_kind="started")
        self.gh = FakeGitHub("acme/app", login="bot[bot]")
        self.events = []
        os.environ["FAKE_AGENT_MODE"] = "write"
        self.addCleanup(os.environ.pop, "FAKE_AGENT_MODE", None)
        self.orch = Orchestrator(self.config, threading.Event(), lambda e, m: self.events.append((e, m)),
                                 linear=self.linear, github=lambda name: self.gh, runner=Runner("none"))
        self.addCleanup(self.orch.state.close)
        repo = self.config.repository(None)
        checkout = self.orch.checkout(repo)
        checkout.remote = str(self.origin)
        checkout.env = lambda: dict(os.environ, GIT_TERMINAL_PROMPT="0")

    def cycle(self):
        self.orch.tick()
        self.orch.wait_idle(30)
        self.orch.tick()

    def test_two_subagents_run_in_parallel_and_open_prs(self):
        a = self.linear.create_issue("Add API", labels=["agent:oracle-backend"], priority=2)
        b = self.linear.create_issue("Add page", labels=["agent:oracle-frontend"])
        other = self.linear.create_issue("Teammate work", labels=["agent:priya-codex"])
        self.orch.tick()
        self.assertEqual(len(self.orch.threads), 2)
        self.orch.wait_idle(30)
        self.orch.tick()
        for issue in (a, b):
            task = self.orch.state.get(issue["identifier"])
            self.assertEqual(task["phase"], "review", task.get("error"))
            self.assertEqual(self.linear.issue(issue["id"])["state"]["name"], "In Review")
            sh(self.origin, "git", "rev-parse", branch_name(issue))
        self.assertEqual(self.linear.issue(other["id"])["state"]["name"], "Todo")
        titles = sorted(p["head"]["ref"] for p in self.gh.pulls)
        self.assertEqual(titles, sorted([branch_name(a), branch_name(b)]))
        self.assertTrue(all(p["draft"] for p in self.gh.pulls))
        board_posts = [b for i, b in self.linear.comment_log if i == "ENG-1"]
        self.assertTrue(any("@oracle-backend finished ENG-2" in p for p in board_posts))
        self.assertEqual(sorted(e for e, _ in self.events).count("task_review"), 2)

    def test_blocked_ticket_waits_for_blocker(self):
        first = self.linear.create_issue("Schema", labels=["agent:oracle-backend"])
        second = self.linear.create_issue("Use schema", labels=["agent:oracle-frontend"])
        self.linear.block(first["id"], second["id"])
        self.assertEqual(blockers(self.linear.issue(second["id"])), ["ENG-2"])
        self.cycle()
        self.assertIsNone(self.orch.state.get("ENG-3"))
        self.linear.move(first, "completed")
        self.cycle()
        self.assertEqual(self.orch.state.get("ENG-3")["phase"], "review")

    def test_failure_goes_to_backlog_and_retry_works(self):
        os.environ["FAKE_AGENT_MODE"] = "noop"
        issue = self.linear.create_issue("Nothing", labels=["agent:oracle-backend"])
        self.cycle()
        self.assertEqual(self.orch.state.get(issue["identifier"])["phase"], "failed")
        self.assertEqual(self.linear.issue(issue["id"])["state"]["name"], "Backlog")
        self.assertIn("task_failed", [e for e, _ in self.events])
        os.environ["FAKE_AGENT_MODE"] = "write"
        self.linear.move(issue, "unstarted")
        self.cycle()
        task = self.orch.state.get(issue["identifier"])
        self.assertEqual((task["phase"], task["attempt"]), ("review", 2))

    def test_one_ticket_per_subagent_at_a_time(self):
        self.linear.create_issue("One", labels=["agent:oracle-backend"])
        self.linear.create_issue("Two", labels=["agent:oracle-backend"])
        self.orch.tick()
        self.assertEqual(len(self.orch.threads), 1)
        self.orch.wait_idle(30)
        self.cycle()
        self.assertEqual(sorted(t["phase"] for t in self.orch.state.all()), ["review", "review"])

    def test_board_mentions_forwarded(self):
        self.config.notify_mentions = {"sam"}
        self.orch.tick()
        self.linear.comment(self.board["id"], "[Priya's Codex] @sam API is merged")
        self.orch.tick()
        self.assertTrue(any(e == "board_mention" and "API is merged" in m for e, m in self.events))


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config({
            "linear": {"team": "ENG"},
            "subagents": {"oracle-backend": {"type": "banyancode", "description": "APIs and DB"},
                          "oracle-frontend": {"type": "banyancode"}},
            "planner": {"base_url": "http://x", "model": "small", "extra_agents": ["priya-codex"]},
            "worker": {"state_dir": self.tmp.name},
        })

    def reply(self, *_):
        return "Sure!\n```json\n" + json.dumps({"tickets": [
            {"key": "T1", "title": "Create schema", "agent": "oracle-backend", "priority": 1,
             "acceptance": ["migration runs"], "description": "Tables for items"},
            {"key": "T2", "title": "Items page", "agent": "agent:oracle-frontend", "depends_on": ["T1", "T9"]},
            {"key": "T3", "title": "Docs", "agent": "nobody", "depends_on": ["T3"]},
            {"key": "T4", "title": "Teammate task", "agent": "priya-codex", "priority": "high"},
        ]}) + "\n```"

    def test_make_and_apply_plan_idempotently(self):
        plan = planner.make_plan(self.config, "# Mega doc", "doc.md", ask=self.reply)
        tickets = {t["key"]: t for t in plan["tickets"]}
        self.assertEqual(tickets["T2"]["agent"], "oracle-frontend")
        self.assertEqual(tickets["T2"]["depends_on"], ["T1"])
        self.assertEqual(tickets["T3"]["agent"], "oracle-backend")  # unknown agent -> first
        self.assertEqual(tickets["T3"]["depends_on"], [])
        self.assertEqual(tickets["T4"]["priority"], 3)
        self.assertIn("T2 [oracle-frontend]", planner.summary(plan))
        linear = FakeLinear()
        created = planner.apply_plan(self.config, planner.load_plan(self.config), linear)
        self.assertEqual(len(created), 4)
        t2 = linear.issue("ENG-2")
        self.assertEqual(blockers(t2), ["ENG-1"])
        self.assertIn("agent:oracle-frontend", [l["name"] for l in t2["labels"]["nodes"]])
        self.assertIn("- [ ] migration runs", linear.issue("ENG-1")["description"])
        self.assertEqual(planner.apply_plan(self.config, planner.load_plan(self.config), linear), [])

    def test_large_doc_is_planned_in_parts_with_context(self):
        self.config.planner["chunk_chars"] = 60
        doc = "# Mega\nintro\n\n## Backend\n" + "b" * 50 + "\n\n## Frontend\n" + "f" * 50 + "\n"
        prompts = []

        def ask(settings, system, user):
            prompts.append(user)
            n = len(prompts)
            deps = ["P1.T1"] if n > 1 else []
            return json.dumps({"tickets": [{"key": "T1", "title": f"Part {n} work",
                                            "agent": "oracle-backend", "depends_on": deps}]})
        plan = planner.make_plan(self.config, doc, "doc.md", ask=ask)
        self.assertGreater(plan["parts"], 1)
        self.assertEqual(len(plan["tickets"]), plan["parts"])
        self.assertIn("P1.T1: Part 1 work", prompts[1])  # later parts see earlier tickets
        self.assertEqual(plan["tickets"][1]["depends_on"], ["P1.T1"])

    def test_section_filter(self):
        doc = "# A\nalpha\n## Payments\npay stuff\n## Other\nx\n"
        seen = []
        planner.make_plan(self.config, doc, "doc.md", section="payments",
                          ask=lambda s, sy, u: seen.append(u) or self.reply())
        self.assertIn("pay stuff", seen[0])
        self.assertNotIn("alpha", seen[0])
        with self.assertRaises(ValueError):
            planner.make_plan(self.config, doc, "doc.md", section="nope", ask=self.reply)

    def test_no_temperature_unless_configured(self):
        from unittest.mock import patch
        sent = {}

        class Response:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return self.body

        def fake_urlopen(request, timeout=0):
            sent.update(json.loads(request.data))
            return Response(json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode())
        with patch("agentorch.planner.urlopen", fake_urlopen):
            planner.chat({"base_url": "http://x", "model": "gpt-6-luna"}, "s", "u")
        self.assertNotIn("temperature", sent)
        self.assertEqual(sent["model"], "gpt-6-luna")

    def test_bad_model_output(self):
        with self.assertRaises(ValueError):
            planner.make_plan(self.config, "doc", "d", ask=lambda *_: "I cannot do that")


class LinearBoardTests(unittest.TestCase):
    def test_unread_skips_own_signature_and_flags_agent_mentions(self):
        linear = FakeLinear()
        board_issue = linear.create_issue("Agent Board", labels=["board"])
        with tempfile.TemporaryDirectory() as tmp:
            board = LinearBoard(linear, board_issue["identifier"], state_dir=Path(tmp),
                                signature="Sam's oracle-backend")
            linear.comment(board_issue["id"], "[Priya's Codex] hello")
            self.assertEqual(len(board.unread()), 1)
            board.post("my update")
            linear.comment(board_issue["id"], "[Priya's Codex] @oracle-backend please add /items")
            posts = board.unread()
            self.assertEqual(len(posts), 1)
            self.assertTrue(posts[0].mention)
            self.assertEqual(board.unread(), [])


if __name__ == "__main__":
    unittest.main()
