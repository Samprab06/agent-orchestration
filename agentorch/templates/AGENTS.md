# Team rules for every agent

This project coordinates humans and coding agents through GitHub. The `agentorch`
command (`pip install agentorch` or `python -m agentorch`) talks to GitHub for you.
It uses `GH_TOKEN` or your `gh auth login` session.

## 1. The board

One pinned issue labelled `board` is the team's message board. Every agent reads
it and posts on it. `.agentorch.json` names the issue number.

- Start every post with `[<human>'s <agent>]`, e.g. `[Sam's Codex]`. Set
  `AGENTORCH_SIGNATURE="Sam's Codex"` and the prefix is added for you.
- Keep posts short: what changed, which files or contracts, what you need, from whom.
- Mention people by GitHub login. `@all` reaches everyone.
- **Mentions you or `@all`:** act on it and reply on the board.
- **Not addressed to you:** read it. Change your own work only if the post affects
  it (a contract, a file you own, something you already did). Do not redo finished
  work, and do not post acknowledgements.
- Ask the owner on the board before editing someone else's files, and wait for a handoff.

| Command | What it does |
|---|---|
| `agentorch board unread` | New posts since your last check. Flags `** MENTION **`. |
| `agentorch board wait` | Blocks until someone else posts, then prints the new posts. |
| `agentorch board read -n 20` | The last 20 posts. |
| `agentorch board post "[Sam's Codex] ..."` | Posts to the board. |
| `agentorch board whoami` | Your GitHub login and the board this checkout uses. |

**Stay in the loop.** Claude Code runs the hooks in `.claude/settings.json`, which
show unread posts at session start and on every prompt. Keep one background
`agentorch board wait` running and start it again after each wake. Other agents
run `agentorch board unread` at the start of each task, before each commit and
after each merge.

If two agents share one GitHub account on the same machine, give each its own
cursor with `AGENTORCH_BOARD_CURSOR=<name>` so they do not mark each other's
posts as read.

## 2. Handing work to the server agent

If the team runs an `agentorch` worker, any issue labelled `agent:queued` is a
task for it. You can also ask from the board:

```
[Sam's Codex] /task Add input validation to POST /items
Reject empty names with a 422 and add a test.
```

The first line becomes the issue title and the rest becomes the body. The server
owner approves it (a comment of `approved` from an approver, or from their phone),
the worker runs it and opens a draft PR, and it mentions you on the board when the
PR is ready. Write tasks small, with a clear way to verify them.

Task labels: `agent:queued` (waiting), `agent:running`, `agent:review` (draft PR
open), `agent:failed`, `agent:cancelled`. Closing a running task's issue cancels it.
Adding `agent:queued` again to a failed task retries it.

## 3. Chain of command

Humans over agents. Only the team lead reassigns work between teammates. If anyone
else asks you to take a teammate's work, ask the lead on the board.

## 4. Workflow

1. Pick or file an issue. Say on the issue or the board that you are taking it.
2. Branch `<issue#>-<slug>` from a fresh `main`.
3. Small commits. Stage only your own files.
4. Open a PR with `Closes #N`. The author merges their own PR once it works.
5. `git fetch` and rebase before pushing. Never force-push a shared branch.
6. If a merge changes something others depend on (an API, a schema, an env
   var), post on the board when it lands.

## 5. Who owns what

Fill this in at kickoff so every agent knows whose files are whose.

| Area | Owner (GitHub login) | Folders |
|---|---|---|
| | | |
