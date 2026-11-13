"""Push notifications from the worker to the operator's phone.

Notifiers are configured as a list. Failures are logged and never stop the
worker.

* ``signal-cli``: send through a signal-cli daemon's JSON-RPC HTTP endpoint,
  the same daemon Hermes uses for its Signal gateway.
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


def build(specs: List[Dict[str, Any]]) -> List[Notifier]:
    result: List[Notifier] = []
    for spec in specs:
        kind = spec["type"]
        if kind == "stdout":
            result.append(StdoutNotifier())
        elif kind == "command":
            result.append(CommandNotifier(spec.get("argv") or [], int(spec.get("timeout", 30))))
        elif kind == "signal-cli":
            recipients = spec.get("recipients") or ([spec["recipient"]] if spec.get("recipient") else [])
            url = spec.get("url") or os.environ.get("SIGNAL_HTTP_URL", "http://127.0.0.1:8080")
            account = spec.get("account") or os.environ.get("SIGNAL_ACCOUNT")
            result.append(SignalCliNotifier(url, account, recipients, spec.get("group_id")))
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
