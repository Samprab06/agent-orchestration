# Team rules for every agent

Tickets live in **Linear**, code and pull requests live in **GitHub**, and agents
coordinate on the **Agent Board**, a Linear ticket whose comments are the team chat.
The `agentorch` command does all of it. It needs your own Linear personal API key in
`LINEAR_API_KEY` and your GitHub login (`gh auth login` or `GH_TOKEN`).
`.agentorch.json` names the Linear team and the board ticket.

Set these once per agent:

```sh
export LINEAR_API_KEY=lin_api_...          # your own key
export AGENTORCH_AGENT=priya-codex         # your agent name: tickets labelled agent:priya-codex are yours
export AGENTORCH_SIGNATURE="Priya's Codex" # added to every board post
```

## 1. Tickets

The project's mega doc is split into Linear tickets. Each ticket is labelled with the
agent that should do it (`agent:<name>`). Server agents (`agent:oracle-*`) are picked
up automatically by the always-on server. Yours are labelled with your agent name.

| Command | What it does |
|---|---|
| `agentorch ticket next` | Claims your next ready ticket (Todo, not blocked) and prints it |
| `agentorch ticket list` | Your ready and in-progress tickets (`--agent x` for others) |
| `agentorch ticket show ENG-12` | One ticket in full |
| `agentorch ticket done ENG-12 --pr <url>` | Moves it to In Review with the PR link |
| `agentorch ticket comment ENG-12 "..."` | Notes, questions, blockers on the ticket |
| `agentorch ticket new "Title" --agent x --body "..."` | A new ticket for an agent |

Work loop:
1. `agentorch ticket next`.
2. Branch from a fresh `main` using the branch name it prints (it contains the ticket
   ID, e.g. `eng-12-add-login`). Linear links branches and PRs that mention the ID.
3. Small commits. Open a PR titled `[ENG-12] ...` with `Fixes ENG-12` in the body.
4. `agentorch ticket done ENG-12 --pr <url>`, then post on the board if others depend on it.
5. Never pick up a ticket labelled for another agent. Ask on the board instead.

## 2. The board

| Command | What it does |
|---|---|
| `agentorch board unread` | New posts since your last check; flags `** MENTION **` |
| `agentorch board wait` | Blocks until someone else posts |
| `agentorch board read -n 20` | The last 20 posts |
| `agentorch board post "..."` | Posts (signature added from `AGENTORCH_SIGNATURE`) |

- Mention agents by name (`@oracle-backend`, `@priya-codex`) or `@all`.
- **Mentions you or `@all`:** act on it and reply on the board.
- **Not addressed to you:** change your own work only if it is affected. Don't post
  acknowledgements.
- Post when you change something others depend on: an API, a schema, an env var.

Claude Code runs the hooks in `.claude/settings.json`, which show unread posts at session
start and on every prompt. Keep one background `agentorch board wait` running. Other
agents run `agentorch board unread` at the start of each ticket, before each commit and
after each merge.

## 3. Chain of command

Humans over agents. Only the team lead reassigns tickets between agents.
