"""A periodic "team lead" pass over Linear, the board and open PRs.

``agentorch supervise`` (run every 15 minutes by a systemd timer) collects a
snapshot: every open ticket with its agent, state, age and blockers, the recent
board posts, the open pull requests, and the known agents. It asks the planner
model (Codex, or any configured endpoint) for a short list of actions, then
applies only these, within fixed limits:

* ``comment``  note on a ticket (unclear acceptance criteria, a question)
* ``assign``   move a ticket to another known agent
* ``ready``    move a Backlog ticket to Todo (e.g. a failed one worth retrying)
* ``backlog``  park a ticket that is not ready, with a reason
* ``create``   a new ticket for a known agent (gaps, follow-ups, splits)
* ``board``    a post on the Agent Board (coordination, mentions)

It never deletes, closes, merges or touches finished tickets. Every applied
action is logged and summarised to the operator's phone; ``--dry-run`` shows
the actions without applying them.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from agentorch.config import Config
from agentorch.linear import AGENT_PREFIX, Linear, LinearBoard, agent_labels, blockers
from agentorch.planner import agent_catalog, chat

MAX_ACTIONS = 10
MAX_CREATES = 3
ACTION_TYPES = ("comment", "assign", "ready", "backlog", "create", "board")

SYSTEM = """You are the team lead for a hackathon team of humans and coding agents.
Every 15 minutes you review the Linear tickets, the agent message board and the open pull
requests, and you keep work flowing. Look for:
- tickets that are vague or too big for one agent session: comment with concrete acceptance
  criteria, or create smaller follow-up tickets
- tickets assigned to the wrong agent, or a busy agent while another is idle: reassign
- failed tickets in Backlog that look worth retrying: mark ready, with a comment on what to
  do differently
- tickets stuck in progress for a long time: comment or post on the board
- questions or blockers on the board addressed to the team or to you: answer or route them
- open PRs that need another agent's attention: post on the board mentioning that agent
Do nothing when things are fine. Never invent agents. Prefer few, high-value actions.
Respond with ONLY a JSON object:
{"summary": "one or two sentences on the state of the project",
 "actions": [
  {"type": "comment", "ticket": "HAC-12", "body": "..."},
  {"type": "assign", "ticket": "HAC-12", "agent": "oracle-frontend", "reason": "..."},
  {"type": "ready", "ticket": "HAC-12", "reason": "..."},
  {"type": "backlog", "ticket": "HAC-12", "reason": "..."},
  {"type": "create", "title": "...", "description": "...", "agent": "...", "blocked_by": ["HAC-12"]},
  {"type": "board", "body": "... mention agents with @name ..."}]}"""


def age_minutes(timestamp: str) -> int:
    try:
        then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0
    return int((datetime.now(timezone.utc) - then).total_seconds() // 60)


def snapshot(config: Config, linear: Linear, board: Optional[LinearBoard],
             pulls: Callable[[], List[Dict[str, Any]]]) -> Dict[str, Any]:
    tickets = []
    for issue in linear.issues(state_types=("backlog", "unstarted", "started"), first=50):
        if any(l["name"].lower() == "board" for l in issue["labels"]["nodes"]):
            continue
        tickets.append({
            "id": issue["identifier"], "title": issue["title"], "state": issue["state"]["name"],
            "agent": (agent_labels(issue) or [None])[0], "priority": issue.get("priority"),
            "minutes_since_update": age_minutes(issue.get("updatedAt", "")),
            "blocked_by": blockers(issue),
            "description": (issue.get("description") or "")[:600],
        })
    posts = []
    if board is not None:
        try:
            posts = [{"from": p.login, "at": p.created_at, "text": p.body[:500]} for p in board.last(15)]
        except Exception:  # the board is context, not essential
            posts = []
    try:
        prs = pulls()
    except Exception:
        prs = []
    load: Dict[str, int] = {}
    for t in tickets:
        if t["agent"] and t["state"] not in ("Backlog",):
            load[t["agent"]] = load.get(t["agent"], 0) + 1
    return {"agents": agent_catalog(config), "open_tickets_per_agent": load, "tickets": tickets,
            "board": posts, "open_pull_requests": prs}


def validate(raw: Dict[str, Any], known_agents: List[str], ticket_ids: List[str]) -> List[Dict[str, Any]]:
    actions, creates = [], 0
    for item in (raw.get("actions") or [])[:MAX_ACTIONS * 2]:
        if not isinstance(item, dict) or item.get("type") not in ACTION_TYPES:
            continue
        kind = item["type"]
        ticket = str(item.get("ticket", "")).upper()
        agent = str(item.get("agent", "")).lower().replace(AGENT_PREFIX, "")
        if kind in ("comment", "assign", "ready", "backlog") and ticket not in ticket_ids:
            continue
        if kind in ("assign", "create") and agent not in known_agents:
            continue
        if kind in ("comment", "board") and not str(item.get("body", "")).strip():
            continue
        if kind == "create":
            if creates >= MAX_CREATES or not str(item.get("title", "")).strip():
                continue
            creates += 1
        actions.append(dict(item, ticket=ticket, agent=agent))
        if len(actions) >= MAX_ACTIONS:
            break
    return actions


def apply(actions: List[Dict[str, Any]], linear: Linear, board: Optional[LinearBoard]) -> List[str]:
    done = []
    note = "_(team lead review)_"
    for action in actions:
        kind = action["type"]
        try:
            if kind == "board":
                if board is not None:
                    board.post(action["body"])
                    done.append(f"board: {action['body'][:80]}")
                continue
            if kind == "create":
                issue = linear.create_issue(action["title"][:120],
                                            (action.get("description") or "") + f"\n\n{note}",
                                            labels=[AGENT_PREFIX + action["agent"]])
                for ref in action.get("blocked_by") or []:
                    try:
                        linear.block(linear.issue(str(ref))["id"], issue["id"])
                    except Exception:
                        pass
                done.append(f"created {issue['identifier']} for {action['agent']}: {action['title'][:60]}")
                continue
            issue = linear.issue(action["ticket"])
            if kind == "comment":
                linear.comment(issue["id"], f"{action['body']}\n\n{note}")
            elif kind == "assign":
                old = [l["name"] for l in issue["labels"]["nodes"] if l["name"].lower().startswith(AGENT_PREFIX)]
                linear.set_labels(issue, add=[AGENT_PREFIX + action["agent"]], remove=old)
                linear.comment(issue["id"], f"Reassigned to `{action['agent']}`: {action.get('reason', '')}\n\n{note}")
            elif kind == "ready":
                linear.move(issue, "unstarted")
                linear.comment(issue["id"], f"Back to Todo: {action.get('reason', '')}\n\n{note}")
            elif kind == "backlog":
                linear.move(issue, "backlog")
                linear.comment(issue["id"], f"Parked in Backlog: {action.get('reason', '')}\n\n{note}")
            done.append(f"{kind} {action['ticket']}" + (f" -> {action['agent']}" if kind == "assign" else ""))
        except Exception as error:  # one bad action must not stop the rest
            done.append(f"FAILED {kind} {action.get('ticket', '')}: {error}")
    return done


def supervise(config: Config, linear: Linear, board: Optional[LinearBoard],
              pulls: Callable[[], List[Dict[str, Any]]], dry_run: bool = False, ask=chat) -> Dict[str, Any]:
    snap = snapshot(config, linear, board, pulls)
    if not snap["tickets"] and not snap["board"]:
        return {"summary": "Nothing to review yet.", "actions": [], "applied": []}
    reply = ask(config.planner, SYSTEM, json.dumps(snap, indent=1))
    raw = extract_json_any(reply)
    actions = validate(raw, list(snap["agents"]), [t["id"] for t in snap["tickets"]])
    applied = [] if dry_run else apply(actions, linear, board)
    return {"summary": str(raw.get("summary", ""))[:500], "actions": actions, "applied": applied,
            "at": time.strftime("%H:%M")}


def extract_json_any(text: str) -> Dict[str, Any]:
    """Like planner.extract_json but the object needs ``actions``, not ``tickets``."""
    try:
        data = json.loads(_slice(text))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"summary": "The model reply was not valid JSON; no actions taken.", "actions": []}
    return data


def _slice(text: str) -> str:
    import re
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    return fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
