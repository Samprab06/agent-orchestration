"""Push notifications from the worker to the operator's phone.

Notifiers are configured as a list. Failures are logged and never stop the
worker.

* ``signal-cli``: send through a signal-cli daemon's JSON-RPC HTTP endpoint,
  the same daemon Hermes uses for its Signal gateway. With
  ``"env_file": "~/.hermes/.env"`` it reuses Hermes' Signal settings and only
  reads the ``SIGNAL_*`` keys from that file. Messages go to the explicit
  ``recipients``/``group_id``, else ``SIGNAL_HOME_CHANNEL``, else the first
  of ``SIGNAL_ALLOWED_USERS``, else the account itself (Note to Self).
* ``command``: run any program with the message on stdin (``AGENTORCH_EVENT``
  holds the event name), e.g. a script that calls another messenger.
* ``stdout``: print, for testing.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.request import Request, urlopen

LOG = logging.getLogger("agentorch.notify")
_ids = itertools.count(1)


class Notifier:
    def send(self, event: str, message: str) -> None:
        raise NotImplementedError


class StdoutNotifier(Notifier):
    def __init__(self, printer: Callable[[str], None] = print):
        self.printer = printer

    def send(self, event: str, message: str) -> None:
        self.printer(f"[{event}] {message}")


class CommandNotifier(Notifier):
    def __init__(self, argv: List[str], timeout: int = 30):
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("command notifier needs argv: a list of strings")
        self.argv = argv
        self.timeout = timeout

    def send(self, event: str, message: str) -> None:
        env = dict(os.environ, AGENTORCH_EVENT=event)
        subprocess.run(self.argv, input=message, text=True, env=env, timeout=self.timeout,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


class SignalCliNotifier(Notifier):
    """signal-cli ``daemon --http`` JSON-RPC: ``send`` to numbers, usernames or a group."""

    def __init__(self, url: str, account: Optional[str] = None, recipients: Iterable[str] = (),
                 group_id: Optional[str] = None, timeout: int = 20):
        self.url = url.rstrip("/")
        if not self.url.endswith("/api/v1/rpc"):
            self.url += "/api/v1/rpc"
        self.account = account
        self.recipients = [r for r in recipients if r]
        self.group_id = group_id
        if not self.recipients and not group_id:
            raise ValueError("signal-cli notifier needs recipients or group_id")
        self.timeout = timeout

    def payload(self, message: str) -> Dict[str, Any]:
        params: Dict[str, Any] = {"message": message}
        if self.account:
            params["account"] = self.account
        if self.group_id:
            params["groupId"] = self.group_id
        elif self.account and self.recipients == [self.account]:
            params["noteToSelf"] = True  # signal-cli linked to the operator's own number
        else:
            params["recipient"] = self.recipients
        return {"jsonrpc": "2.0", "method": "send", "params": params, "id": next(_ids)}

    def send(self, event: str, message: str) -> None:
        request = Request(self.url, data=json.dumps(self.payload(message)).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read() or b"{}")
        if body.get("error"):
            raise RuntimeError(f"signal-cli: {body['error'].get('message', body['error'])}")


SIGNAL_KEYS = ("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT", "SIGNAL_HOME_CHANNEL", "SIGNAL_ALLOWED_USERS")


def read_env_file(path: str, keys: Iterable[str] = SIGNAL_KEYS) -> Dict[str, str]:
    """Read only the named keys from a dotenv file such as ``~/.hermes/.env``."""
    wanted, values = set(keys), {}
    try:
        text = open(os.path.expanduser(path), encoding="utf-8").read()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in wanted:
            value = value.split(" #", 1)[0].strip().strip("'\"")
            values[key] = value
    return values


def signal_settings(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Explicit settings win, then the env file (Hermes' .env), then the environment."""
    env = {k: os.environ[k] for k in SIGNAL_KEYS if os.environ.get(k)}
    if spec.get("env_file"):
        env.update(read_env_file(spec["env_file"]))
    recipients = spec.get("recipients") or ([spec["recipient"]] if spec.get("recipient") else [])
    group_id = spec.get("group_id")
    if not recipients and not group_id:
        home = env.get("SIGNAL_HOME_CHANNEL", "")
        allowed = [u.strip() for u in env.get("SIGNAL_ALLOWED_USERS", "").split(",") if u.strip()]
        target = home or (allowed[0] if allowed else env.get("SIGNAL_ACCOUNT", ""))
        if target.startswith("group."):
            group_id = target[len("group."):]
        elif target:
            recipients = [target]
    return {
        "url": spec.get("url") or env.get("SIGNAL_HTTP_URL") or "http://127.0.0.1:8080",
        "account": spec.get("account") or env.get("SIGNAL_ACCOUNT"),
        "recipients": recipients, "group_id": group_id,
    }


def build(specs: List[Dict[str, Any]]) -> List[Notifier]:
    result: List[Notifier] = []
    for spec in specs:
        kind = spec["type"]
        if kind == "stdout":
            result.append(StdoutNotifier())
        elif kind == "command":
            result.append(CommandNotifier(spec.get("argv") or [], int(spec.get("timeout", 30))))
        elif kind == "signal-cli":
            settings = signal_settings(spec)
            result.append(SignalCliNotifier(settings["url"], settings["account"],
                                            settings["recipients"], settings["group_id"]))
    return result


class Dispatcher:
    def __init__(self, notifiers: List[Notifier], events: Iterable[str]):
        self.notifiers = notifiers
        self.events = set(events)

    def __call__(self, event: str, message: str) -> List[str]:
        errors: List[str] = []
        if event not in self.events:
            return errors
        for notifier in self.notifiers:
            try:
                notifier.send(event, message)
            except Exception as error:  # a notifier must never take the worker down
                LOG.warning("notifier %s failed: %s", type(notifier).__name__, error)
                errors.append(f"{type(notifier).__name__}: {error}")
        return errors
