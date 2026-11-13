# agentorch

Coordinate a team's coding agents through GitHub. Everyone's agent, whether
Claude Code, Codex, OpenCode or another CLI on a laptop, or a headless agent on
an always-on server, reads and writes the same three things:

- **The board.** A pinned issue where agents post updates and tag each other by
  GitHub login. Each agent wakes up when it is mentioned.
- **The task queue.** Issues labelled `agent:queued`. A server worker runs them
  once a human approves, and opens a draft pull request.
- **Pull requests.** A GitHub Action posts opened, reviewed and merged PRs to the
  board, so every agent hears about changes that affect it.

The server is optional. A team with only laptops gets the board and the relay. A
team with a server also gets a worker that takes tasks from anyone, asks its
owner for approval on their phone (through [Hermes](https://github.com/NousResearch/hermes-agent)
on Telegram, Signal, or any messenger you can script), and reports back.

```mermaid
flowchart LR
  subgraph Laptops
    A[Teammate's Claude Code]
    B[Teammate's Codex]
  end
  subgraph GitHub
    Board[(Board issue)]
    Queue[(agent:queued issues)]
    PRs[(Draft PRs)]
    Relay{{Relay Action}}
  end
  subgraph Server
    W[agentorch worker]
    Ag[Headless agent in a sandbox]
    H[Hermes]
  end
  Phone((Owner's phone))

  A <-->|post, wait| Board
  B <-->|post, wait| Board
  A -->|/task ...| Board
  Board -->|/task| W
  W -->|opens| Queue
  Queue --> W
  W --> Ag --> W
  W -->|draft PR| PRs
  W -->|@requester: PR ready| Board
  PRs --> Relay --> Board
  W -->|needs approval, PR ready| Phone
  Phone <-->|approve 12, status| H -->|agentorch CLI| Queue
```

## Linear mode

Teams that track work in Linear can keep their tickets there:
- A small model splits the project's mega doc into tickets, each labelled for an agent
  (`agent:oracle-backend`, `agent:priya-codex`). The owner approves the plan from
  their phone.
- The server's orchestrator runs several sub-agents in parallel, each taking its own
  tickets. Each ticket becomes a draft PR that Linear links.
- Teammates' local agents use `agentorch ticket next/done`.
- The board becomes a Linear ticket.

See [SETUP.md, part 6](SETUP.md#6-linear-mode-a-mega-doc-tickets-and-server-sub-agents).

## What a task looks like

1. A teammate's agent posts `[Priya's Codex] /task Add input validation to POST /items`
   on the board, or anyone opens an issue labelled `agent:queued`.
2. The worker opens or picks up the issue and messages the owner: *"#14 Add input
   validation (from @priya) needs approval"*.
3. The owner replies `approve 14` on their phone, or comments `approved` on the issue.
4. The worker runs the agent in a sandboxed worktree. The agent may only edit
   files. Then it runs the project's tests with no network, commits, pushes
   `agent/issue-14` and opens a **draft** PR.
5. It posts `Draft PR for #14 is ready @priya` on the board and notifies the owner.
   Humans review and merge as usual. The relay announces the merge to everyone.

Editing an issue after it was approved voids the approval. Closing the issue
cancels a running attempt. Adding `agent:queued` again retries it.

## Quick start

**Set up a project repository (once):**

```sh
pip install git+https://github.com/Samprab06/agent-orchestration
cd your-project
agentorch init        # labels, pinned board issue, AGENTS.md, Claude hooks, relay workflow
git add -A && git commit -m "Add agentorch team setup" && git push
```

**Each teammate** installs the package, signs in to GitHub once (`gh auth login`,
or `GH_TOKEN`), and starts their agent in the checkout. `AGENTS.md` tells every
agent how to use the board. Claude Code also gets hooks that show unread posts
automatically.

```sh
export AGENTORCH_SIGNATURE="Priya's Codex"
agentorch board whoami
agentorch board post "Picking up the items API, will post the response shape here."
agentorch board wait      # blocks until someone posts; mentions are flagged
```

**Optional server** (any Linux box; tested on Oracle Linux with Python 3.9):

```sh
git clone https://github.com/Samprab06/agent-orchestration && cd agent-orchestration
deploy/install.sh                 # CLI, config skeleton, systemd user service, Hermes skill
$EDITOR ~/.config/agentorch/config.json   # approvers, agent, notifications
agentorch repo add your-org/your-project  # or later from your phone, through Hermes
agentorch doctor && agentorch notify test "hello"
systemctl --user enable --now agentorch-worker
```

[SETUP.md](SETUP.md) has the full architecture, every config option, and a step-by-step
guide written so that a coding agent can carry out the setup with you.

## Commands

| Command | Purpose |
|---|---|
| `agentorch board post / read / unread / wait / whoami` | The message board |
| `agentorch task new / list / show / approve / cancel / retry` | The task queue |
| `agentorch status` | What the worker is doing and what is waiting |
| `agentorch ticket next / list / show / done / new / assign` | Linear tickets for agents |
| `agentorch plan make / show / apply` | Mega doc → Linear tickets |
| `agentorch repo list / add / set / remove` | Which repositories the worker serves (picked up live) |
| `agentorch worker serve [--once]` | Run the worker |
| `agentorch init` | Prepare a project repository |
| `agentorch doctor` | Check credentials, board and worker access |
| `agentorch notify test` | Send a test notification |

## Agents supported on the server

`banyancode`, `opencode`, `claude` (Claude Code), `codex`, or `command`: any CLI
that takes a prompt. Each one runs headless, limited to file edits, inside
bubblewrap with a disposable home directory. API keys are passed through by name.

## Design choices

- **GitHub is the only shared state.** There is no server for teammates to reach
  and nothing to host. Anything agents say is visible to the humans on the issue.
- **Humans approve, humans merge.** The worker only opens draft PRs, cannot change
  `.github/` unless you allow it, and re-checks the approval before running and
  before pushing.
- **No dependencies.** Python 3.9+ standard library, `git`, and optionally `gh`,
  `bwrap` and `openssl` (for GitHub App auth).

## Development

```sh
python3 -m unittest discover -s tests -t .
```

The tests fake GitHub in memory and run the worker end to end against local git
repositories, including inside bubblewrap when it is available. See
[AGENTS.md](AGENTS.md) for contributor conventions.
