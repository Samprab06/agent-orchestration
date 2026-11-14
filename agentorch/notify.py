"""Push notifications from the worker to the operator's phone.

Notifiers are configured as a list. Failures are logged and never stop the
worker.

* ``telegram``: send through the Telegram Bot API. With
  ``"env_file": "~/.hermes/.env"`` it reuses the bot Hermes' Telegram gateway
  runs, reading only ``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_HOME_CHANNEL`` and
  ``TELEGRAM_ALLOWED_USERS``. Sending does not interfere with Hermes receiving
  messages. Messages go to ``chat_ids``, else the home channel, else the
  first allowed user (a user's ID is their private chat with the bot).
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
from urllib.error import HTTPError, URLError
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


class TelegramNotifier(Notifier):
    LIMIT = 4000  # Telegram allows 4096 characters per message

    def __init__(self, token: str, chat_ids: Iterable[str], api: str = "https://api.telegram.org",
                 timeout: int = 20):
        if not token:
            raise ValueError("telegram notifier needs a bot token (TELEGRAM_BOT_TOKEN)")
        self.chat_ids = [str(c) for c in chat_ids if str(c).strip()]
        if not self.chat_ids:
            raise ValueError("telegram notifier needs chat_ids, TELEGRAM_HOME_CHANNEL or TELEGRAM_ALLOWED_USERS")
        self._token = token
        self.api = api.rstrip("/")
        self.timeout = timeout

    def send(self, event: str, message: str) -> None:
        text = message if len(message) <= self.LIMIT else message[:self.LIMIT] + "..."
        for chat in self.chat_ids:
            data = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
            request = Request(f"{self.api}/bot{self._token}/sendMessage", data=data, method="POST",
                              headers={"Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read() or b"{}")
            except HTTPError as error:
                try:
                    detail = json.loads(error.read() or b"{}").get("description", "")
                except ValueError:
                    detail = ""
                # Never include the URL: it contains the bot token.
                raise RuntimeError(f"telegram: HTTP {error.code} {detail}".strip()) from None
            except (URLError, OSError) as error:
                raise RuntimeError(f"telegram: {getattr(error, 'reason', error)}") from None
            if not body.get("ok"):
                raise RuntimeError(f"telegram: {body.get('description', 'send failed')}")


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
TELEGRAM_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL", "TELEGRAM_ALLOWED_USERS")


def read_env_file(path: str, keys: Iterable[str] = SIGNAL_KEYS) -> Dict[str, str]:
    """Read only the named keys from a dotenv file such as ``~/.hermes/.env``."""
    wanted, values = set(keys), {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            text = handle.read()
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


def telegram_settings(spec: Dict[str, Any]) -> Dict[str, Any]:
    env = {k: os.environ[k] for k in TELEGRAM_KEYS if os.environ.get(k)}
    if spec.get("env_file"):
        env.update(read_env_file(spec["env_file"], TELEGRAM_KEYS))
    chats = [str(c) for c in (spec.get("chat_ids") or ([spec["chat_id"]] if spec.get("chat_id") else []))]
    if not chats:
        home = env.get("TELEGRAM_HOME_CHANNEL", "").strip()
        allowed = [u.strip() for u in env.get("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()]
        chats = [home] if home else allowed[:1]
    token = spec.get("bot_token") or env.get("TELEGRAM_BOT_TOKEN", "")
    return {"token": token, "chat_ids": chats}


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
        elif kind == "telegram":
            settings = telegram_settings(spec)
            result.append(TelegramNotifier(settings["token"], settings["chat_ids"],
                                           spec.get("api", "https://api.telegram.org")))
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
