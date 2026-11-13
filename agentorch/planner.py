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
``max_tickets`` (default 25), ``extra_agents`` (labels for teammates' agents
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

MAX_DOC = 60000

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


def chat(settings: Dict[str, Any], system: str, user: str, timeout: int = 180) -> str:
    base = settings.get("base_url")
    model = settings.get("model")
    if not base or not model:
        raise ValueError("planner.base_url and planner.model are required")
    headers = {"Content-Type": "application/json"}
    key = planner_key(settings)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"model": model, "temperature": 0.2,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
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


def make_plan(config: Config, document: str, source: str, ask=chat) -> Dict[str, Any]:
    catalog = agent_catalog(config)
    if not catalog:
        raise ValueError("Define subagents (or planner.extra_agents) so tickets can be assigned")
    agents_text = "\n".join(f"- {name}: {desc}" for name, desc in catalog.items())
    if len(document) > MAX_DOC:
        document = document[:MAX_DOC] + "\n\n[document truncated]"
    user = f"Allowed agents:\n{agents_text}\n\nProject document ({source}):\n\n{document}"
    reply = ask(config.planner, SYSTEM, user)
    tickets = validate(extract_json(reply), list(catalog), int(config.planner.get("max_tickets", 25)))
    plan = {"id": time.strftime("%Y%m%d-%H%M%S"), "source": source, "created": time.time(),
            "model": config.planner.get("model"), "tickets": tickets, "applied": {}}
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


def summary(plan: Dict[str, Any], limit: int = 30) -> str:
    lines = [f"Plan {plan['id']}: {len(plan['tickets'])} tickets from {plan['source']}"]
    for t in plan["tickets"][:limit]:
        deps = f" after {','.join(t['depends_on'])}" if t["depends_on"] else ""
        lines.append(f"{t['key']} [{t['agent']}] P{t['priority']} {t['title']}{deps}")
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
