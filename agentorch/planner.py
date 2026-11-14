"""Split a project's mega doc into Linear tickets with a small LLM.

``agentorch plan`` reads the doc (from the code repository's base branch, or a
local file), asks any OpenAI-compatible chat endpoint for tickets as JSON,
validates them, saves the plan and sends a summary to the operator.
``agentorch plan apply`` creates the tickets in Linear, labelled for their
agent, with "blocks" relations for dependencies. Applying twice never creates
duplicates.

Config (``planner``): ``base_url`` (e.g. https://openrouter.ai/api/v1),
``model``, ``api_key_file`` (default ~/.config/agentorch/planner-key) or
``api_key_env``, ``megadoc`` (path in the repository), ``repository``,
``max_tickets`` per part (default 40), ``chunk_chars`` (default 60000: larger
docs are planned section by section, split at # and ## headings),
``temperature`` and ``reasoning_effort`` (only sent if set), ``timeout``, ``extra_agents`` (labels for teammates' agents
the planner may also assign).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agentorch.config import Config, expand
from agentorch.linear import AGENT_PREFIX, Linear

MAX_DOC = 400000  # characters sent in one request; larger docs are planned section by section

SYSTEM = """You are a technical project planner. Split the project document into small,
independently verifiable engineering tickets that coding agents can complete in under
an hour each. Respond with ONLY a JSON object, no prose, in this shape:
{"tickets": [{"key": "T1", "title": "imperative title under 80 chars",
  "description": "what to build, which files or modules, and context the agent needs",
  "acceptance": ["concrete check", "..."], "agent": "<one of the allowed agents>",
  "depends_on": ["T0"], "priority": 2}]}
priority: 1 urgent, 2 high, 3 medium, 4 low. depends_on lists keys of tickets that must
be finished first; keep chains short so work runs in parallel. Assign each ticket to the
agent whose description fits best."""


def planner_key(settings: Dict[str, Any]) -> str:
    env = settings.get("api_key_env", "PLANNER_API_KEY")
    if os.environ.get(env):
        return os.environ[env]
    path = expand(settings.get("api_key_file", "~/.config/agentorch/planner-key"))
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""  # local endpoints such as Ollama need no key


def run_command(settings: Dict[str, Any], system: str, user: str, timeout: int = 900) -> str:
    """Use a coding CLI as the planner, e.g. Codex on a ChatGPT login.

    ``command`` is an argv list. The prompt goes to stdin. If an element is
    ``{output}``, it is replaced by a temporary file whose contents are the reply
    (Codex's ``--output-last-message``); otherwise stdout is the reply.
    """
    import subprocess
    import tempfile
    argv = settings["command"]
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("planner.command must be a list of arguments")
    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "reply.txt")
        argv = [output if a == "{output}" else os.path.expanduser(a) for a in argv]
        result = subprocess.run(argv, input=system + "\n\n" + user, text=True, capture_output=True,
                                timeout=int(settings.get("timeout", timeout)), cwd=tmp)
        if result.returncode:
            tail = (result.stderr or result.stdout or "").strip()[-400:]
            raise RuntimeError(f"planner command exited {result.returncode}: {tail}")
        if output in argv:
            with open(output, encoding="utf-8") as handle:
                return handle.read()
        return result.stdout


def chat(settings: Dict[str, Any], system: str, user: str, timeout: int = 180) -> str:
    if settings.get("command"):
        return run_command(settings, system, user)
    base = settings.get("base_url")
    model = settings.get("model")
    if not base or not model:
        raise ValueError("Set planner.command (e.g. Codex) or planner.base_url and planner.model")
    headers = {"Content-Type": "application/json"}
    key = planner_key(settings)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"model": model,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    # Reasoning models (GPT-5/6 family, o-series) reject a custom temperature.
    if settings.get("temperature") is not None:
        payload["temperature"] = float(settings["temperature"])
    if settings.get("reasoning_effort"):
        payload["reasoning_effort"] = settings["reasoning_effort"]
    timeout = int(settings.get("timeout", timeout))
    request = Request(base.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
                      method="POST", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except HTTPError as error:
        raise RuntimeError(f"planner model: HTTP {error.code} {error.read()[:300]!r}") from None
    except URLError as error:
        raise RuntimeError(f"planner model: {error.reason}") from None
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"planner model: unexpected reply {str(data)[:300]}") from None


def extract_json(text: str) -> Dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    try:
        data = json.loads(candidate)
    except ValueError as error:
        raise ValueError(f"The model did not return valid JSON: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("tickets"), list):
        raise ValueError("The model's JSON has no tickets list")
    return data


def validate(data: Dict[str, Any], allowed: List[str], max_tickets: int = 25) -> List[Dict[str, Any]]:
    tickets: List[Dict[str, Any]] = []
    keys = set()
    fallback = allowed[0]
    for index, raw in enumerate(data["tickets"][:max_tickets], 1):
        if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
            continue
        key = str(raw.get("key") or f"T{index}").strip()
        if key in keys:
            key = f"{key}-{index}"
        keys.add(key)
        agent = str(raw.get("agent", "")).strip().lower().replace(AGENT_PREFIX, "")
        acceptance = raw.get("acceptance") or []
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        try:
            priority = min(4, max(1, int(raw.get("priority", 3))))
        except (TypeError, ValueError):
            priority = 3
        tickets.append({
            "key": key, "title": " ".join(str(raw["title"]).split())[:120],
            "description": str(raw.get("description", "")).strip(),
            "acceptance": [str(a).strip() for a in acceptance if str(a).strip()],
            "agent": agent if agent in allowed else fallback,
            "depends_on": [str(d) for d in (raw.get("depends_on") or []) if isinstance(d, (str, int))],
            "priority": priority,
        })
    for ticket in tickets:
        ticket["depends_on"] = [d for d in ticket["depends_on"] if d in keys and d != ticket["key"]]
    # Drop dependency cycles: keep an edge only if it points to an earlier ticket.
    order = {t["key"]: i for i, t in enumerate(tickets)}
    for ticket in tickets:
        ticket["depends_on"] = [d for d in ticket["depends_on"] if order[d] < order[ticket["key"]]]
    if not tickets:
        raise ValueError("The plan has no usable tickets")
    return tickets


def agent_catalog(config: Config) -> Dict[str, str]:
    catalog = {name: spec.get("description", f"{spec.get('type')} coding agent")
               for name, spec in config.subagents.items()}
    for name in config.planner.get("extra_agents", []) or []:
        catalog.setdefault(str(name).lower(), "a teammate's local coding agent")
    return catalog


def plans_dir(config: Config) -> Path:
    path = config.state_dir / "plans"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def sections(document: str, level: int = 2) -> List[Dict[str, str]]:
    """Split markdown at headings of ``level`` or higher (# and ## by default)."""
    pattern = re.compile(r"^(#{1,%d})\s+(.+?)\s*#*\s*$" % level, re.M)
    marks = list(pattern.finditer(document))
    if not marks:
        return [{"title": "Document", "text": document}]
    parts = []
    if document[:marks[0].start()].strip():
        parts.append({"title": "Introduction", "text": document[:marks[0].start()]})
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(document)
        parts.append({"title": mark.group(2).strip(), "text": document[mark.start():end]})
    return parts


def chunk(document: str, limit: int) -> List[Dict[str, str]]:
    """Group consecutive sections into chunks of at most ``limit`` characters."""
    chunks: List[Dict[str, str]] = []
    for part in sections(document):
        text = part["text"]
        while len(text) > limit:  # one huge section: hard split
            chunks.append({"title": part["title"], "text": text[:limit]})
            text = text[limit:]
        if chunks and len(chunks[-1]["text"]) + len(text) <= limit and not chunks[-1].get("full"):
            chunks[-1]["text"] += text
            chunks[-1]["title"] += " / " + part["title"]
        else:
            chunks.append({"title": part["title"], "text": text})
    return chunks


def make_plan(config: Config, document: str, source: str, ask=chat, section: Optional[str] = None,
              progress=None) -> Dict[str, Any]:
    catalog = agent_catalog(config)
    if not catalog:
        raise ValueError("Define subagents (or planner.extra_agents) so tickets can be assigned")
    agents_text = "\n".join(f"- {name}: {desc}" for name, desc in catalog.items())
    if section:
        matches = [p for p in sections(document, 6) if section.casefold() in p["title"].casefold()]
        if not matches:
            raise ValueError(f"No heading matching {section!r} in {source}")
        document = "".join(p["text"] for p in matches)
        source = f"{source} § {section}"
    limit = int(config.planner.get("chunk_chars", 60000))
    pieces = chunk(document, limit) if len(document) > limit else [{"title": "whole document", "text": document}]
    pieces = [p for p in pieces if p["text"].strip()]
    max_tickets = int(config.planner.get("max_tickets", 40))
    tickets: List[Dict[str, Any]] = []
    for number, piece in enumerate(pieces, 1):
        if progress:
            progress(f"planning part {number}/{len(pieces)}: {piece['title'][:80]}")
        done = "\n".join(f"- {t['key']}: {t['title']}" for t in tickets)
        context = (f"\n\nTickets already planned from earlier parts (do not repeat them; you may list "
                   f"their keys in depends_on):\n{done}" if done else "")
        scope = "" if len(pieces) == 1 else f"\n\nThis is part {number} of {len(pieces)}: {piece['title']}."
        user = (f"Allowed agents:\n{agents_text}{context}{scope}\n\n"
                f"Project document ({source}):\n\n{piece['text'][:MAX_DOC]}")
        reply = ask(config.planner, SYSTEM, user)
        prefix = f"P{number}." if len(pieces) > 1 else ""
        raw = extract_json(reply)
        for item in raw["tickets"]:
            if isinstance(item, dict):
                own = {str(t.get("key")) for t in raw["tickets"] if isinstance(t, dict)}
                item["key"] = prefix + str(item.get("key") or "")
                item["depends_on"] = [prefix + str(d) if str(d) in own else str(d)
                                      for d in (item.get("depends_on") or [])]
        earlier = [dict(t) for t in tickets]
        merged = validate({"tickets": earlier + raw["tickets"]}, list(catalog), len(earlier) + max_tickets)
        tickets = merged
    plan = {"id": time.strftime("%Y%m%d-%H%M%S"), "source": source, "created": time.time(),
            "model": config.planner.get("model"), "parts": len(pieces), "tickets": tickets, "applied": {}}
    save_plan(config, plan)
    return plan


def save_plan(config: Config, plan: Dict[str, Any]) -> Path:
    directory = plans_dir(config)
    path = directory / f"plan-{plan['id']}.json"
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    latest = directory / "latest.json"
    latest.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path


def load_plan(config: Config, plan_id: Optional[str] = None) -> Dict[str, Any]:
    directory = plans_dir(config)
    path = directory / (f"plan-{plan_id}.json" if plan_id else "latest.json")
    if not path.is_file():
        raise ValueError("No plan yet. Run agentorch plan first.")
    return json.loads(path.read_text(encoding="utf-8"))


def summary(plan: Dict[str, Any], limit: int = 40) -> str:
    parts = f" in {plan['parts']} parts" if plan.get("parts", 1) > 1 else ""
    lines = [f"Plan {plan['id']}: {len(plan['tickets'])} tickets from {plan['source']}{parts}"]
    for t in plan["tickets"][:limit]:
        deps = f" after {','.join(t['depends_on'])}" if t["depends_on"] else ""
        lines.append(f"{t['key']} [{t['agent']}] P{t['priority']} {t['title']}{deps}")
    if len(plan["tickets"]) > limit:
        lines.append(f"... and {len(plan['tickets']) - limit} more (agentorch plan show)")
    if plan.get("applied"):
        lines.append(f"Already created: {len(plan['applied'])}")
    return "\n".join(lines)


def apply_plan(config: Config, plan: Dict[str, Any], linear: Linear) -> List[str]:
    created: List[str] = []
    applied = plan.setdefault("applied", {})
    ids: Dict[str, str] = {}
    for ticket in plan["tickets"]:
        if ticket["key"] in applied:
            ids[ticket["key"]] = applied[ticket["key"]]["id"]
            continue
        parts = [ticket["description"]]
        if ticket["acceptance"]:
            parts.append("**Acceptance criteria**\n" + "\n".join(f"- [ ] {a}" for a in ticket["acceptance"]))
        if ticket["depends_on"]:
            refs = [applied[d]["identifier"] for d in ticket["depends_on"] if d in applied]
            if refs:
                parts.append("Depends on: " + ", ".join(refs))
        parts.append(f"_Planned by agentorch from {plan['source']} (plan {plan['id']}, {ticket['key']})._")
        issue = linear.create_issue(ticket["title"], "\n\n".join(p for p in parts if p),
                                    labels=[AGENT_PREFIX + ticket["agent"]], priority=ticket["priority"])
        applied[ticket["key"]] = {"id": issue["id"], "identifier": issue["identifier"], "url": issue["url"]}
        ids[ticket["key"]] = issue["id"]
        save_plan(config, plan)  # record progress so a retry never duplicates
        for dep in ticket["depends_on"]:
            if dep in ids:
                linear.block(ids[dep], issue["id"])
        created.append(f"{issue['identifier']} [{ticket['agent']}] {ticket['title']}")
    return created
