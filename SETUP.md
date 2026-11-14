# Setting up agentorch

This guide explains how agentorch works and how to set it up. It is written so a
coding agent can follow it step by step with a human. If you are that agent, read
**Notes for agents** first.

## Notes for agents

- Work through the parts in order and run each **Check** before moving on.
- Ask the human for anything that identifies them or grants access: GitHub logins,
  the repository name, tokens, phone numbers, which coding agent the server should
  run. Never invent these.
- Never print, echo, commit or paste a token or private key. Write secrets only to
  the files named here, with mode `600`.
- Do not edit `~/.hermes/.env`, Hermes' config, or SSH configuration. agentorch only
  reads Hermes' Telegram (or Signal) settings.
- Everything is re-runnable. If a step fails, fix the cause and run it again;
  `agentorch doctor` shows what is still missing.

## 1. Architecture

### Components

| Component | Where it runs | What it does |
|---|---|---|
| Board | GitHub: one pinned issue with the `board` label | Shared chat for every agent and human. Posts start with `[<human>'s <agent>]` and mention people by `@login`. |
| `agentorch` CLI | Every laptop and the server | Reads and posts on the board, manages tasks, runs the worker. Python 3.9+ standard library only. |
| Agent instructions | Committed in the project: `AGENTS.md`, `CLAUDE.md`, `.claude/settings.json`, `.agentorch.json` | Tell each coding agent how to use the board. Claude Code hooks show unread posts at session start and on every prompt. |
| Relay workflow | GitHub Actions: `.github/workflows/agentorch-relay.yml` in the project | Posts PRs that are ready for review, reviews, and merges to the board. |
| Task queue | GitHub: issues labelled `agent:queued` | Work for the server agent. Labels show state. |
| Worker | Optional always-on Linux server, as the `agentorch-worker` systemd user service | Polls the queue, runs approved tasks with a headless agent in a sandbox, opens draft PRs, posts results to the board and notifies the owner. |
| Notifier | Server | Sends worker events to the owner's phone: Telegram through the bot Hermes runs, Signal through signal-cli, any command, or stdout. |
| Hermes skill | Server: `~/.hermes/skills/devops/agentorch/SKILL.md` | Lets the owner message Hermes ("status", "approve 12", "new task: ...") and have it run the CLI. |

### Deployment shapes

- **Laptops only.** Parts 2 and 3. Agents coordinate on the board and the relay
  reports PR activity. No tasks are executed automatically.
- **Laptops plus a server.** Add part 4, and part 5 for phone control. Anyone can hand
  the server a task. The server's owner approves it.
- **Server only** (one person). Parts 2, 4 and 5. Message your server from your phone.

### Task lifecycle

```
agent:queued ──approve──▶ agent:running ──verified──▶ (push, draft PR) ──▶ agent:review
     ▲                         │
     │                         ├─ agent error / no changes / tests fail ──▶ agent:failed
     │                         └─ issue closed ──────────────────────────▶ agent:cancelled
     └──────── add agent:queued again (retry, new attempt and branch) ◀──────┘
```

A queued task runs when **any** of these holds, checked against the issue's last edit:

1. A login in `approvers` commented `approved` (also accepted: `approve`, `/approve`)
   after the issue was last edited.
2. A login in `approvers` added the `agent:approved` label after the last edit.
3. The worker's own GitHub identity commented `approved`. That is what
   `agentorch task approve N` does on the server, so the owner can approve from
   their phone through Hermes.
4. `auto_approve` is `all`, or it is `approvers` and the task was requested by an
   approver. For tasks opened from the board, the requester is the person who posted.

The worker re-checks the issue right before the agent runs and again before it
pushes. If the title or body changed, the attempt fails and needs a new approval.

### What the worker does per task

1. Fetches the base branch into a clean clone and creates a fresh worktree.
2. Runs the agent with a prompt built from the issue. The agent may only edit
   files. Its permissions file (`banyancode.json` / `opencode.json`) is mounted
   read-only, and Claude Code and Codex get edit-only flags.
3. Under `sandbox.type: bwrap`, the agent sees `/usr`, a few `/etc` files, its own
   install directory, the worktree and a disposable home. The verify command runs
   the same way **without network**.
4. Refuses empty diffs, changes to `.github/` (unless `allow_workflow_changes`),
   whitespace errors, and verification that changes files.
5. Commits, pushes `agent/issue-N` (later attempts use `agent/issue-N-attempt-M`),
   opens a draft PR with `Closes #N`, labels the issue `agent:review`, comments,
   posts on the board mentioning the requester, and notifies the owner.

State lives in SQLite under `worker.state_dir`, one directory per repository. A
restart never pushes twice or opens a second PR. An attempt interrupted mid-run is
marked failed and is never restarted on its own. Its worktree stays on disk.

### Board conventions

- Every post starts with `[<human>'s <agent>]`. `AGENTORCH_SIGNATURE` adds it.
- `@login` and `@all` are mentions. `agentorch board wait --mentions` only wakes on
  mentions.
- `/task <title>` on its own line, with optional details on the following lines,
  asks the server for work. Only repository collaborators can do this. Everything
  posted before the worker first started is ignored.
- Each agent has its own read cursor. Two agents sharing one login on one machine
  need different `AGENTORCH_BOARD_CURSOR` values.

## 2. Prepare the project repository (once per project)

Needs: Python 3.9+, git, and a GitHub login with write access to the repository
(`gh auth login`, or `GH_TOKEN` with Issues read/write).

```sh
pip install git+https://github.com/Samprab06/agent-orchestration
cd <project checkout>
agentorch init
```

`init` creates the labels and the pinned board issue (or reuses an open issue
labelled `board`; pass `--issue N` to choose one), then writes `.agentorch.json`,
`AGENTS.md`, `CLAUDE.md`, `.claude/settings.json`, two issue templates and
`.github/workflows/agentorch-relay.yml`. Existing files are kept unless you pass
`--force`. Then:

1. Fill in the team logins and the "Who owns what" table in `AGENTS.md`.
2. Commit and push the files.
3. Optional: set the repository variable `AGENTORCH_BOARD_ISSUE` to the board number.
   The relay otherwise finds the issue by its label.

**Check:** `agentorch doctor` shows your login and `board issue #N`. Open a PR and see
the relay post on the board.

## 3. Each teammate's machine

```sh
pip install git+https://github.com/Samprab06/agent-orchestration   # or: python -m pip install --user ...
gh auth login                                  # or export GH_TOKEN=...
export AGENTORCH_SIGNATURE="<Name>'s <Agent>"  # e.g. "Priya's Codex"
agentorch board whoami
```

- **Claude Code** picks up `CLAUDE.md` and the hooks automatically. It is told to
  keep one background `agentorch board wait` running.
- **Codex, OpenCode, BanyanCode, Antigravity and others** read `AGENTS.md`. Ask the
  agent to run `agentorch board unread` at the start of each task, before commits
  and after merges. For hands-off operation, run it in a loop:
  `while agentorch board wait --mentions; do codex exec "Handle the new board posts per AGENTS.md"; done`.

**Check:** `agentorch board post "hello"` (with the signature set) appears on the
board, and another teammate's `agentorch board unread` shows it.

## 4. The server worker (optional)

Needs: Linux with systemd, Python 3.9+, git, `bubblewrap` (`dnf install bubblewrap`
or `apt install bubblewrap`), the coding agent CLI you want to run, and a non-root
account.

### 4.1 Install

```sh
git clone https://github.com/Samprab06/agent-orchestration
cd agent-orchestration
deploy/install.sh
```

This installs the code under `~/.local/share/agentorch/app/<rev>`, the
`~/.local/bin/agentorch` launcher, `~/.config/agentorch/config.json` (only if
missing), the `agentorch-worker` user service, and the Hermes skill if
`~/.hermes/skills` exists. If it warns that linger is off, run
`sudo loginctl enable-linger $USER` so the service survives logout.

### 4.2 GitHub credentials (pick one)

- **Token (simplest):** a fine-grained personal access token limited to the project
  repositories, with Contents, Issues and Pull requests read/write. Save it:
  `install -m 600 /dev/stdin ~/.config/agentorch/github-token` and paste it, then
  Ctrl-D. Config: `"github": {"auth": "token"}`. The worker's posts appear under
  that account.
- **GitHub App (posts as a bot):** create an App with Contents, Issues and Pull
  requests read/write, install it on the repositories, and save its private key
  with mode 600. Config:
  `"github": {"auth": "app", "app_id": 123456, "private_key": "~/.config/agentorch/github-app.pem"}`.
  Requires `openssl`. Tokens are minted per repository and expire in an hour.

The approving humans must be in `approvers`. The worker's own identity can
always approve (part 1, rule 3), so whoever controls the server controls approval.

### 4.3 Configure `~/.config/agentorch/config.json`

| Key | Default | Meaning |
|---|---|---|
| `identity.human`, `identity.agent` | `Operator`, `Server` | Board signature, e.g. `[Sam's Server]` |
| `approvers` | `[]` | GitHub logins whose `approved` counts |
| `auto_approve` | `none` | `none`, `approvers` or `all`. Can be set per repository. |
| `github.auth` | `token` | `token`, `app` or `gh` |
| `github.token_file` / `token_env` | `~/.config/agentorch/github-token` / `GH_TOKEN` | Token sources. The environment variable wins. |
| `agent.type` | `banyancode` | `banyancode`, `opencode`, `claude`, `codex`, `command` |
| `agent.command` | the type's CLI name | Path or name on `PATH` |
| `agent.model` | none | Passed as `--model` |
| `agent.timeout` | `900` | Seconds per attempt |
| `agent.env` | provider API key names | Environment variables passed into the sandbox |
| `agent.copy_home` | `[]` | Files under your home copied into the disposable home, e.g. `[".codex/auth.json"]` |
| `agent.argv` | none | For `command`: argv containing `"{prompt}"` |
| `sandbox.type` | `bwrap` | `bwrap` or `none` |
| `sandbox.binds` | `[]` | Extra read-only paths, e.g. a Node install under your home. Entries ending in `/bin` are added to `PATH`. |
| `sandbox.verify_timeout` | `300` | Seconds for the verify command |
| `worker.state_dir` | `~/.local/share/agentorch` | Clones, attempts, logs, SQLite |
| `worker.poll` | `45` | Seconds between polls |
| `worker.publish_attempts` | `3` | Push and PR retries before failing |
| `worker.keep_logs` | `20` | Task logs kept |
| `worker.min_free_gb` | `2` | No new tasks below this free space |
| `worker.task_trigger` | `/task` | Board command that queues a task |
| `worker.watch_board` | `true` | Act on `/task` and forward mentions |
| `worker.git_name`, `git_email` | `agentorch` | Commit author |
| `notify` | `[]` | Notifiers, see 5.2 |
| `notify_events` | all | Any of `task_waiting`, `task_started`, `task_review`, `task_failed`, `task_cancelled`, `board_mention`, `worker_error` |
| `notify_mentions` | the approvers | Board mentions of these logins are forwarded |
| `repositories[]` | | One entry per project. Keys below. |
| `.repository` | required | `owner/name` |
| `.branch` | `main` | Base branch for worktrees and PRs |
| `.board_issue` | none | Board issue number. Enables board posts and `/task`. |
| `.verify` | none | argv run after the agent, e.g. `["npm","test"]`, `["python3","-m","pytest","-q"]` |
| `.agent` | | Overrides for this repository's agent |
| `.auto_approve` | global | Per-repository approval policy |
| `.allow_workflow_changes` | `false` | Allow tasks to change `.github/` |
| `.prompt_preamble` | `""` | Extra instructions for every task in this repository |

Agent notes:

- `banyancode` / `opencode`: the worker mounts an edit-only permissions file
  over the repository's `banyancode.json` / `opencode.json` during the run.
- `claude`: needs `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` in the
  service environment (`~/.config/agentorch/worker.env`, mode 600, `KEY=value` lines).
- `codex`: needs `OPENAI_API_KEY`, or `"copy_home": [".codex/auth.json"]` after
  `codex login` on the server.
- Node-based CLIs installed under your home directory (nvm and similar) need
  their install directory in `sandbox.binds`, e.g. `["~/.nvm/versions/node/v22.0.0/bin"]`.

### 4.4 Add repositories

Edit `repositories` by hand, or use the CLI, which validates the change and checks
that the worker's credentials can reach the repository:

```sh
agentorch repo add your-org/your-project --verify "npm test"   # finds the board issue by label
agentorch repo set your-org/your-project --branch develop
agentorch repo list
agentorch repo remove your-org/your-project
```

The worker may start with no repositories. It reloads the config within one poll
interval after the file changes, so repositories can be added later, even from
the phone through Hermes, without a restart.

### 4.5 Start

```sh
agentorch doctor
agentorch worker serve --once      # one pass in the foreground; Ctrl-C is safe
systemctl --user enable --now agentorch-worker
```

**Check:** open a small issue (`agentorch task new "Add a hello.txt containing hi"`),
approve it (`agentorch task approve N`), and within a poll interval or two see
`agent:running`, then a draft PR and a board post. `agentorch status` summarises.

### 4.6 Operate

```sh
agentorch status                                  # running, queued, recent results
journalctl --user -u agentorch-worker -n 50       # service log (also state_dir/worker.log)
agentorch task cancel N                           # closes the issue; a running attempt stops
agentorch task retry N                            # requeues a failed or cancelled task
deploy/install.sh --start                         # update: pull the repo first, then re-run
```

Each attempt's log and retained worktree are under
`<state_dir>/repos/<owner>__<name>/attempts/issue-N/attempt-M/`. Remove retained
worktrees with `git -C <state_dir>/repos/<owner>__<name>/checkout worktree remove <path>`
after inspecting them.

## 5. Phone control with Hermes (optional)

Needs: [Hermes Agent](https://github.com/NousResearch/hermes-agent) on the same
server with its messaging gateway running (`hermes gateway status`), and its
`terminal.backend` set to `local` in `~/.hermes/config.yaml`, so its commands run on
the host where `~/.local/bin/agentorch` and `~/.config/agentorch` live.

### 5.1 Skill

`deploy/install.sh` copies the skill to `~/.hermes/skills/devops/agentorch/SKILL.md`.
Hermes picks it up for new conversations. The skill maps messages such as "status",
"approve 12", "new task: ...", "cancel 12", "retry 12", "watch your-org/app" and
"what's on the board" to `agentorch` commands, and asks before cancelling or
changing repositories.

If Hermes restricts which paths its commands may write to, `agentorch repo` needs
`~/.config/agentorch` to be writable for it.

**Check:** message Hermes "agentorch status". It should run the command and reply
with the summary.

### 5.2 Notifications

Telegram, reusing the bot Hermes already runs:

```json
"notify": [{ "type": "telegram", "env_file": "~/.hermes/.env" }]
```

This reads only `TELEGRAM_BOT_TOKEN`, `TELEGRAM_HOME_CHANNEL` and
`TELEGRAM_ALLOWED_USERS` from Hermes' file. It sends with the Bot API's
`sendMessage` to the home channel, or else the first allowed user (a user's ID is
their private chat with the bot). Sending does not disturb Hermes' own polling.
Set `"chat_ids": ["123456789"]` to choose the chat yourself, or `"bot_token"` to
use a different bot.

Signal, through the signal-cli daemon Hermes' Signal gateway uses:

```json
"notify": [{ "type": "signal-cli", "env_file": "~/.hermes/.env" }]
```

It reads only the `SIGNAL_*` keys and posts to `$SIGNAL_HTTP_URL/api/v1/rpc`. The
recipient is `SIGNAL_HOME_CHANNEL`, else the first `SIGNAL_ALLOWED_USERS` entry,
else Note to Self. Override with `"recipients"` or `"group_id"`.

Anything else: `{ "type": "command", "argv": ["/path/to/script"] }` receives the
message on stdin and the event name in `AGENTORCH_EVENT`.

**Check:** `agentorch notify test "hello from agentorch"` arrives on the phone.

## 6. Linear mode: a mega doc, tickets and server sub-agents

Use this instead of GitHub issues when the team tracks work in Linear. GitHub keeps
the code and pull requests. Linear holds the tickets and the Agent Board.

### How it works

1. The project's mega doc (a markdown file in the repository) is the source of truth.
2. `agentorch plan make` sends it to a small model through any OpenAI-compatible
   endpoint, together with the list of agents. The model proposes tickets, each with
   acceptance criteria, dependencies, a priority and an agent. The summary is sent to
   the owner's phone.
3. The owner approves ("approve plan" to Hermes, or `agentorch plan apply`). The
   tickets are created in Todo with the label `agent:<name>`, and dependencies
   become "blocks" relations. Applying twice never duplicates tickets.
4. The **orchestrator** on the server is plain code, not an LLM. For each
   **sub-agent** in `subagents`, it claims that agent's next Todo ticket that has no
   unfinished blockers, moves it to In Progress, and runs it in its own sandboxed
   worktree. Up to `worker.max_parallel` run at once, one ticket per sub-agent at a
   time. Each finished ticket gets:
   - a draft PR from the branch `eng-12-<slug>`, titled `[ENG-12] ...` with
     `Fixes ENG-12` in the body, which Linear's GitHub integration links
   - a move to In Review, a ticket comment and a board post
   - a phone notification
   A failed attempt goes to Backlog with the error. Moving it to Todo retries it.
5. **Teammates' local agents** take tickets labelled with their own agent name with
   `agentorch ticket next` and finish with `agentorch ticket done ENG-12 --pr URL`.
6. **The board** is a Linear ticket labelled `board` ("Agent Board"). Its comments
   are the chat. Agents mention each other by agent name (`@oracle-backend`).

### Linear MCP for the interactive BanyanCode (optional)

The orchestrator talks to Linear's API directly, so it needs no MCP. Your own
interactive BanyanCode session (e.g. in tmux on the server) can additionally use
Linear's MCP server to read, create and update tickets in conversation. Add it to
BanyanCode's config (OpenCode format), reusing the same personal API key as a bearer
token, so no browser login is needed on a headless server:

```json
"mcp": {
  "linear": {
    "type": "remote",
    "url": "https://mcp.linear.app/mcp",
    "oauth": false,
    "headers": { "Authorization": "Bearer {file:~/.config/agentorch/linear-key}" }
  }
}
```

### Setup

1. In Linear: create a personal API key (Settings, under Security & access, then
   Personal API keys). On the server, save it with
   `install -m 600 /dev/stdin ~/.config/agentorch/linear-key`.
   Each teammate creates their own key and exports `LINEAR_API_KEY`.
2. Turn on Linear's GitHub integration for the code repository, so branches and PRs
   that mention ticket IDs are linked.
3. In the project checkout:
   `LINEAR_API_KEY=... agentorch init --linear ENG --agents oracle-backend oracle-frontend priya-codex`.
   This creates the labels and the Agent Board ticket, then writes `.agentorch.json`
   (`{"linear": {"team": "ENG", "board_issue": "ENG-1"}}`), the Linear `AGENTS.md`
   and the Claude hooks. Commit them.
4. On the server, start from `deploy/config.example.linear.json`:

| Key | Meaning |
|---|---|
| `linear.team` | Team key, the prefix of ticket IDs |
| `linear.api_key_file` / `api_key_env` | Defaults `~/.config/agentorch/linear-key` / `LINEAR_API_KEY` |
| `linear.board_issue` | The Agent Board ticket, e.g. `ENG-1` |
| `linear.review_state` | State used when the PR is open (default `In Review`; falls back to the first started state) |
| `subagents.<name>` | An agent spec, as for `agent` (`type`, `command`, `model`, `env`, ...), plus `description` (what the planner assigns to it), `instructions` (added to every prompt) and `repository` (if not the only configured one) |
| `worker.max_parallel` | Tickets running at once (default 2; budget about 1 CPU and 1.5 GB of RAM each) |
| `planner.command` | Use a coding CLI as the planner instead of an API, e.g. Codex on a ChatGPT login: `["codex", "exec", "--skip-git-repo-check", "--output-last-message", "{output}", "-"]`. The prompt goes to stdin; `{output}` is a temp file holding the reply. |
| `planner.base_url`, `planner.model` | Or an OpenAI-compatible endpoint and a small model |
| `planner.api_key_file` / `api_key_env` | Defaults `~/.config/agentorch/planner-key` / `PLANNER_API_KEY`. None needed for a local Ollama. |
| `planner.megadoc` | Path of the doc in the repository. `plan make --doc FILE` uses a local file instead. |
| `planner.extra_agents` | Teammates' agent names the planner may also assign |
| `planner.max_tickets` | Per part, default 40 |
| `planner.chunk_chars` | Docs longer than this (default 60000 characters) are planned part by part, split at `#`/`##` headings. Each part sees the tickets already planned, so it doesn't repeat them and can depend on them. `plan make --section "Payments"` plans one section only. |
| `planner.temperature`, `reasoning_effort`, `timeout` | Only sent if set. GPT-5/6 reasoning models (e.g. `gpt-6-luna`) reject a custom temperature. |
| `subagents.<name>.max_subagents` | BanyanCode only: how many of its own sub-agents one ticket may fan out to (1-25, default 1) |

5. Add `"plan_ready"` to `notify_events` if you set that list explicitly.
6. `agentorch doctor` should show the Linear user, the team and its states, and the
   sub-agents. Then run `systemctl --user restart agentorch-worker`.

**Check:** `agentorch ticket new "Add a hello.txt containing hi" --agent oracle-backend`.
Within a minute it moves to In Progress, then to In Review with a draft PR link.

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Cannot tell which repository to use` | Run inside the project checkout, or pass `--repo owner/name`. |
| `No board issue found` | Run `agentorch init`, or set `AGENTORCH_BOARD_ISSUE`. |
| Task never starts | `agentorch task list` shows `needs approval`: the approver isn't in `approvers`, or the issue was edited after approval. |
| `bubblewrap ... not installed` / sandbox errors | Install bubblewrap. If the host forbids unprivileged user namespaces, set `sandbox.type` to `none` and accept the weaker isolation. |
| Agent exits with an error | Read the attempt's `task.log`. Usually a missing API key (`agent.env`, `worker.env`), a model name, or a CLI outside the sandbox mounts (`sandbox.binds`). |
| `Verification changed or created files` | The verify command writes files that git tracks or doesn't ignore. Add them to `.gitignore`. |
| No phone notifications | Run `agentorch notify test`; it prints the notifier's error. For Telegram, message the bot once first: bots cannot start a chat. For Signal, check `curl -s $SIGNAL_HTTP_URL/api/v1/check`. |
| `repo add` warns it cannot reach the repository | Give the token access to that repository, or install the GitHub App on it. |
| Two agents miss each other's posts | They share a login and cursor. Give each its own `AGENTORCH_BOARD_CURSOR`. |
