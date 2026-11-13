---
name: agentorch
description: "Control the agentorch coding worker: status, approvals, new tasks, cancel/retry, which repositories it serves, and the team board."
version: 1.0.0
author: agentorch
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [github, coding-agent, tasks, approvals, devops]
---

# agentorch: the coding worker on this server

The operator uses this skill from their phone (Telegram) to check on and approve work for the
coding agent that runs on this server. GitHub issues are the tasks. The worker runs
an approved task, opens a draft pull request, and sends its own Telegram updates.
You never edit code yourself here; you run the `agentorch` command and report back.

Run every command with the terminal tool, using the full path
`~/.local/bin/agentorch`. Reply in one to five short lines suited to a phone:
issue numbers, titles, states and links. No tables and no raw JSON.

## When to use

Use this skill when the operator asks about tasks, the queue, the worker, a pull
request it opened, approving or cancelling work, giving the agent a new job, or what
teammates posted on the board. Typical messages: "status", "what's running",
"approve 12", "ok run it", "cancel 12", "retry 12", "new task: add rate limiting",
"what's on the board", "tell the team the API is merged".

## Commands

| Operator says | Run |
|---|---|
| status, what's going on | `~/.local/bin/agentorch status` |
| list tasks, queue | `~/.local/bin/agentorch task list` |
| details on #N | `~/.local/bin/agentorch task show N` |
| approve N, ok N, go N | `~/.local/bin/agentorch task approve N --note "approved from phone"` |
| new task: TITLE (details) | `~/.local/bin/agentorch task new "TITLE" --body "DETAILS"` |
| new task ... and run it now | add `--approve` to the line above |
| cancel N, stop N | `~/.local/bin/agentorch task cancel N --note "cancelled from phone"` |
| retry N | `~/.local/bin/agentorch task retry N` |
| what's on the board | `~/.local/bin/agentorch board read -n 8` |
| post to the board / tell the team ... | `~/.local/bin/agentorch board post --signature "OPERATOR's phone" "TEXT"` |
| is everything healthy | `~/.local/bin/agentorch doctor` and `systemctl --user is-active agentorch-worker` |
| which repos are you watching | `~/.local/bin/agentorch repo list` |
| work on / watch OWNER/NAME | `~/.local/bin/agentorch repo add OWNER/NAME` (add `--branch B`, `--verify "npm test"` or `--board-issue N` if given) |
| change settings for a repo | `~/.local/bin/agentorch repo set OWNER/NAME --verify "pytest -q"` (or `--branch`, `--board-issue`, `--auto-approve none|approvers|all`, `--agent`) |
| stop watching OWNER/NAME | `~/.local/bin/agentorch repo remove OWNER/NAME` |

The worker reloads its config within a minute after `repo add/set/remove`; no
restart is needed. `repo add` reports whether the worker's GitHub credentials can
reach the repository. If it warns that they cannot, tell the operator that the
GitHub App (or token) needs access to that repository.

If more than one repository is configured, the commands need `--repo OWNER/NAME`.
`status` lists the repositories; ask which one if the message doesn't say and it is
ambiguous. A bare "approve" or "ok" right after a "needs approval" notification
refers to the issue in that notification.

## Rules

1. Approve only the issue number the operator named or clearly meant. If unsure, ask
   "Approve #N (title)?" and wait.
2. Ask for confirmation before `cancel`; a running attempt stops and its work is
   discarded from the queue.
3. Never pass `--approve` to `task new` unless the operator said to run it right away.
4. Write task titles as short imperatives ("Add rate limiting to POST /items"). Put
   details, acceptance criteria and file names in `--body`. Quote shell arguments
   carefully; never put the operator's text unquoted into a command.
5. Do not run any other command that changes the repository, the worker's files or
   its service, and never edit `~/.config/agentorch/` by hand; use `agentorch repo`.
   Restart the worker (`systemctl --user restart agentorch-worker`) only on an
   explicit request.
6. Confirm the exact `OWNER/NAME` before `repo add` or `repo remove`, and only
   use `--auto-approve all` if the operator asked for it by name.
7. If a command fails, send the first line of the error and suggest
   `agentorch doctor`.

## Notifications you will see

The worker sends its own messages through this same Telegram bot, prefixed with the repository name, for: a task
waiting for approval, a task started, a draft PR ready, a failure or cancellation, a
teammate mentioning the operator on the board, and a worker that keeps failing. When
the operator replies to one, use the issue number in it.
