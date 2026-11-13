import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from agentorch.config import Config
from agentorch.github import provider_for
from agentorch.notify import (CommandNotifier, Dispatcher, SignalCliNotifier, build, read_env_file,
                              signal_settings)


class FakeSignal(BaseHTTPRequestHandler):
    received = []
    fail = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeSignal.received.append((self.path, body))
        reply = {"jsonrpc": "2.0", "id": body["id"]}
        reply.update({"error": {"message": "unregistered"}} if FakeSignal.fail else {"result": {}})
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / ".env"
        self.env.write_text(
            "# hermes\nOPENAI_API_KEY=secret-not-read\nSIGNAL_HTTP_URL=http://127.0.0.1:8080\n"
            "export SIGNAL_ACCOUNT='+15550001111'\nSIGNAL_ALLOWED_USERS=+15552223333, +15554445555\n"
        )
        for key in ("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT", "SIGNAL_HOME_CHANNEL", "SIGNAL_ALLOWED_USERS"):
            os.environ.pop(key, None)

    def test_env_file_reads_only_signal_keys(self):
        values = read_env_file(str(self.env))
        self.assertNotIn("OPENAI_API_KEY", values)
        self.assertEqual(values["SIGNAL_ACCOUNT"], "+15550001111")

    def test_recipient_fallbacks(self):
        settings = signal_settings({"env_file": str(self.env)})
        self.assertEqual(settings["recipients"], ["+15552223333"])
        with open(self.env, "a") as f:
            f.write("SIGNAL_HOME_CHANNEL=group.abc==\n")
        settings = signal_settings({"env_file": str(self.env)})
        self.assertEqual((settings["recipients"], settings["group_id"]), ([], "abc=="))
        settings = signal_settings({"env_file": str(self.env), "recipients": ["+1999"]})
        self.assertEqual(settings["recipients"], ["+1999"])

    def test_note_to_self_when_linked_to_own_number(self):
        notifier = SignalCliNotifier("http://x", "+1555", ["+1555"])
        params = notifier.payload("hi")["params"]
        self.assertTrue(params["noteToSelf"])
        self.assertNotIn("recipient", params)

    def test_sends_json_rpc(self):
        server = HTTPServer(("127.0.0.1", 0), FakeSignal)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        FakeSignal.received.clear()
        url = f"http://127.0.0.1:{server.server_port}"
        [notifier] = build([{"type": "signal-cli", "url": url, "account": "+1", "recipients": ["+2"]}])
        notifier.send("task_review", "PR ready")
        path, body = FakeSignal.received[0]
        self.assertEqual(path, "/api/v1/rpc")
        self.assertEqual(body["method"], "send")
        self.assertEqual(body["params"], {"message": "PR ready", "account": "+1", "recipient": ["+2"]})
        FakeSignal.fail = True
        self.addCleanup(setattr, FakeSignal, "fail", False)
        with self.assertRaises(RuntimeError):
            notifier.send("x", "y")


class DispatcherTests(unittest.TestCase):
    def test_filters_events_and_swallows_errors(self):
        seen = []

        class Good:
            def send(self, event, message):
                seen.append((event, message))

        class Bad:
            def send(self, event, message):
                raise OSError("down")

        dispatch = Dispatcher([Bad(), Good()], {"task_review"})
        self.assertEqual(dispatch("task_started", "x"), [])
        errors = dispatch("task_review", "done")
        self.assertEqual(seen, [("task_review", "done")])
        self.assertIn("down", errors[0])

    def test_command_notifier_gets_stdin_and_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            script = f"import os,sys; open({str(out)!r},'w').write(os.environ['AGENTORCH_EVENT']+':'+sys.stdin.read())"
            CommandNotifier([sys.executable, "-c", script]).send("task_failed", "boom")
            self.assertEqual(out.read_text(), "task_failed:boom")


class TokenFileTests(unittest.TestCase):
    def test_token_file_permissions_and_env_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text("file-token\n")
            config = Config({"github": {"auth": "token", "token_file": str(path), "token_env": "AO_TEST_TOKEN"}})
            os.environ.pop("AO_TEST_TOKEN", None)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                provider_for(config, "a/b")
            path.chmod(0o600)
            self.assertEqual(provider_for(config, "a/b").token(), "file-token")
            with patch.dict(os.environ, {"AO_TEST_TOKEN": "env-token"}):
                self.assertEqual(provider_for(config, "a/b").token(), "env-token")


if __name__ == "__main__":
    unittest.main()
