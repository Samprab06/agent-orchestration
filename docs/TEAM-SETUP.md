# Team setup: Linear + your coding agent

This guide is for each teammate. It connects **your** Claude Code or Codex to the
team's Linear workspace, so your agent can take its own tickets, open PRs, and talk to
the other agents. It takes about 15 minutes.

Part 7 is a separate section for the **dedicated PC** that runs Claude Code unattended.

---

## 0. How the team works (read this first)

- **Linear** holds all the work. The project's mega doc gets split into tickets, and
  each ticket is labelled with the agent that should do it, e.g. `agent:alex-claude`.
- **The Oracle server** runs our server agents (`oracle-backend`, `oracle-frontend`)
  all the time. They pick up their own tickets automatically. You don't need to do
  anything for those.
- **Your agent** (Claude Code or Codex on your laptop) picks up tickets labelled with
  *your* agent name, does them, opens a PR on GitHub, and marks the ticket done.
- **The Agent Board** is Linear ticket **HAC-6**. Its comments are the team chat for
  agents. Agents post updates there and mention each other by agent name
  (`@oracle-backend`, `@alex-claude`, `@all`).
- **The `agentorch` command** is the glue. Your agent runs it to get tickets, mark them
  done, and read or post on the board.

| Thing | Value |
|---|---|
| Linear workspace | hackprinceton |
| Team key (ticket prefix) | `HAC` |
| Agent Board | `HAC-6` |
| Code repository | `<REPO>`, to be announced. Clone it once it exists. |
| Server agents | `oracle-backend`, `oracle-frontend` |

---

## 1. Pick your agent name and tell Sam

Use the form `firstname-tool`, lowercase letters and dashes only:

- `alex-claude` for Alex using Claude Code
- `priya-codex` for Priya using Codex

**Send your agent name to Sam**, so the planner can assign tickets to you. Tickets for you
will carry the label `agent:<your-name>`.

---

## 2. Install the tools

You need **Python 3.9+**, **git**, the **GitHub CLI** (`gh`), and your coding agent.

```sh
python3 --version          # 3.9 or newer (Windows: py --version)
git --version
gh --version               # install from https://cli.github.com if missing
```

Log in to GitHub once. Your agent uses this login to push branches and open PRs.

```sh
gh auth login              # pick GitHub.com, HTTPS, and log in with the browser
```

Install agentorch:

```sh
pip install "git+https://github.com/Samprab06/agent-orchestration@agentorch/5-linear"
agentorch --version        # should print agentorch 0.1.0
```

If `agentorch` is "not found" after installing:
- **macOS/Linux:** `python3 -m pip install --user ...`, then add `~/.local/bin` to your PATH,
  or use `python3 -m agentorch` wherever this guide says `agentorch`.
- **Windows:** use `py -m agentorch`, or add the Python `Scripts` folder to PATH.

---

## 3. Create your own Linear API key

Everyone uses **their own** key, so Linear shows who did what.

1. Open Linear and click your **avatar** (top-left), then **Settings**.
2. In the left sidebar under your personal settings, click **Security & access**.
3. Scroll to **Personal API keys** and click **New API key**.
4. Name it `agentorch`, choose **Full access** and **All teams**, then **Create**.
5. **Copy the key now** (it starts with `lin_api_`). Linear shows it only once.

Treat the key like a password. Never paste it into a chat, a commit or a ticket.

---

## 4. Set your environment

These variables tell agentorch who you are. Set them **permanently**, so every new
terminal (and your agent) has them.

### macOS / Linux

Add these lines to `~/.zshrc` (macOS) or `~/.bashrc` (Linux), then open a new terminal:

```sh
export LINEAR_API_KEY="lin_api_...your key..."
export AGENTORCH_LINEAR_TEAM="HAC"
export AGENTORCH_BOARD_ISSUE="HAC-6"
export AGENTORCH_AGENT="alex-claude"            # your agent name from step 1
export AGENTORCH_SIGNATURE="Alex's Claude"      # how your board posts are signed
```

### Windows (PowerShell)

Run once, then **close and reopen** PowerShell:

```powershell
setx LINEAR_API_KEY "lin_api_...your key..."
setx AGENTORCH_LINEAR_TEAM "HAC"
setx AGENTORCH_BOARD_ISSUE "HAC-6"
setx AGENTORCH_AGENT "alex-claude"
setx AGENTORCH_SIGNATURE "Alex's Claude"
```

### Check it

```sh
agentorch doctor
agentorch board read -n 3
agentorch ticket list
```

`doctor` should show `[ok] Linear: <your name> on team HAC` and `[ok] board: HAC-6`.
The `operator config: none (laptop mode)` line is expected on a laptop.

Say hello so the team can see it worked:

```sh
agentorch board post "online and ready"
```

---

## 5. Connect your agent

The **required** part is that your agent can run `agentorch`. Steps 4 already did that.
Connecting Linear's MCP server is **optional**: it lets your agent browse and edit tickets
directly in conversation, which is handy for questions like "what's blocking HAC-12?".

### 5a. Claude Code

**Linear MCP (optional):**

```sh
claude mcp add --transport http linear https://mcp.linear.app/mcp
```

Then start `claude` and run `/mcp`. Choose **linear** and log in through the browser.

**Team rules and hooks:** once the code repo exists, it will contain `AGENTS.md`,
`CLAUDE.md` and `.claude/settings.json`. Claude Code reads these automatically when you
start it **inside the repo folder**. The hooks show new board posts at the start of every
session and on every prompt. Nothing else to do.

Until the repo exists, start Claude in any folder and paste the prompt from section 6.

### 5b. Codex

**Linear MCP (optional).** Add this to `~/.codex/config.toml` (Windows:
`%USERPROFILE%\.codex\config.toml`):

```toml
[mcp_servers.linear]
url = "https://mcp.linear.app/mcp"
bearer_token_env_var = "LINEAR_API_KEY"
```

This reuses the API key from step 4, so there's no extra login. If your Codex version
doesn't support remote MCP servers, skip this. `agentorch` covers everything you need.

**Team rules:** Codex reads `AGENTS.md` automatically when you start it inside the repo
folder. Until the repo exists, paste the prompt from section 6.

---

## 6. The work loop

### Starting prompt for your agent

Paste this at the start of a session (replace the name):

> You are **alex-claude** on a team of humans and coding agents. Work is tracked in
> Linear (team HAC) and coordinated with the `agentorch` command, which is on PATH.
> 1. Run `agentorch board unread` and read anything addressed to you or `@all`.
> 2. Run `agentorch ticket next` to claim your next ticket. It prints the ticket and a
>    branch name. If there are none, say so and stop.
> 3. In the repo: `git checkout main && git pull`, then create the printed branch.
> 4. Implement the ticket, meet its acceptance criteria, and run the tests.
> 5. Commit, push, and open a PR with `gh pr create`. Title it `[HAC-12] <title>` and put
>    `Fixes HAC-12` in the body.
> 6. Run `agentorch ticket done HAC-12 --pr <PR URL>`.
> 7. If you changed anything others depend on (an API, a schema, an env var), post it with
>    `agentorch board post "..."`, mentioning who it affects.
> 8. Go back to step 1.
>
> Only work on tickets labelled for you. If something blocks you, comment on the ticket
> with `agentorch ticket comment HAC-12 "..."` and ask on the board.

### Command cheat sheet

| Command | What it does |
|---|---|
| `agentorch ticket next` | Claim your next ready ticket (Todo, not blocked). Prints it and its branch name. |
| `agentorch ticket next --peek` | Show the next ticket without claiming it |
| `agentorch ticket list` | Your ready and in-progress tickets (`--all` adds backlog) |
| `agentorch ticket show HAC-12` | One ticket in full |
| `agentorch ticket done HAC-12 --pr <url>` | Move it to In Review with the PR link |
| `agentorch ticket comment HAC-12 "text"` | Add a note or question on the ticket |
| `agentorch ticket new "Title" --agent name --body "..."` | Create a ticket for an agent |
| `agentorch board unread` | New board posts since you last looked (`** MENTION **` = for you) |
| `agentorch board wait` | Wait until someone posts, then print the posts |
| `agentorch board read -n 20` | Last 20 board posts |
| `agentorch board post "text"` | Post to the board |

### Rules everyone follows

- **Branch names come from `ticket next`.** They contain the ticket ID (e.g.
  `hac-12-add-login`), so Linear links your PR automatically.
- **One ticket at a time.** Finish it, or comment why you're blocked, before taking another.
- **Never take tickets labelled for another agent.** Ask on the board instead.
- **Don't edit files another agent is working on** without asking on the board first.
- **Post when you change a contract**: an API route, a data shape, a database schema, an
  env var, a shared component.
- **Don't post acknowledgements** ("ok", "got it"). Post only when there's information.
- **Humans decide.** Only Sam or the team lead reassigns tickets.

### Troubleshooting

| Problem | Fix |
|---|---|
| `No Linear settings` | `AGENTORCH_LINEAR_TEAM` isn't set in this terminal. Open a new terminal after step 4. |
| `No Linear API key` | `LINEAR_API_KEY` isn't set in this terminal. |
| `Linear API 400` / `401` | The key is wrong or was revoked. Create a new one (step 3). |
| `No ready tickets for <name>` | Nothing is assigned to you yet, or your tickets are blocked by unfinished ones. Check `agentorch ticket list --all`. |
| Your board posts aren't signed | Set `AGENTORCH_SIGNATURE`. |
| `gh pr create` fails | Run `gh auth login` again, and make sure you've been added to the repo. |

---

## 7. The dedicated Claude Code PC

This machine runs Claude Code **unattended** for a few hours. It takes tickets labelled
for it one after another, with no one at the keyboard.

### 7.1 Get the machine ready

1. **Power:** plug it in. Set sleep to **Never** and turn off automatic updates and
   restarts for the day.
   - Windows: Settings, then System, then Power, Screen and sleep: Never.
   - macOS: System Settings, then Battery or Energy: prevent sleeping. Or run
     `caffeinate -dims` in a terminal and leave it open.
2. **Accounts:** log in to Claude Code (run `claude` once and sign in), then run
   `gh auth login`. Use the account that should appear as the PR author.
3. Do sections **2 to 4** of this guide on this machine with its **own agent name**, e.g.
   `pc-claude`, and `AGENTORCH_SIGNATURE="Team's PC Claude"`. **Tell Sam the name.**
4. **Clone the repo** into a folder used **only** by this machine's agent:
   ```sh
   git clone https://github.com/<REPO>.git ~/pc-agent-work
   ```
   Don't work in that folder yourself while the loop is running.
5. **Test once by hand:** in `~/pc-agent-work`, run `agentorch doctor` and
   `agentorch ticket next --peek`.

### 7.2 Permissions

Unattended Claude can't answer permission prompts, so give it a fixed allow-list. Save
this as `.claude/settings.local.json` inside `~/pc-agent-work`. Git ignores this file,
so it stays on this machine only.

```json
{
  "permissions": {
    "allow": [
      "Read", "Edit", "Write", "Glob", "Grep",
      "Bash(git:*)", "Bash(gh pr:*)", "Bash(agentorch:*)",
      "Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)",
      "Bash(python3:*)", "Bash(pytest:*)", "Bash(pip:*)"
    ],
    "deny": ["Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(rm -rf:*)"]
  }
}
```

Add your project's other build and test tools (e.g. `"Bash(cargo:*)"`) as needed. Avoid
`--dangerously-skip-permissions` on a machine that holds anyone's personal accounts.

### 7.3 The loop script

**macOS / Linux.** Save as `~/pc-agent-loop.sh`, then `chmod +x ~/pc-agent-loop.sh`:

```sh
#!/usr/bin/env bash
# Takes this agent's Linear tickets one at a time and runs Claude Code on each.
cd ~/pc-agent-work || exit 1
while true; do
  ticket=$(agentorch ticket next 2>&1)
  if [ $? -ne 0 ] || echo "$ticket" | grep -q "^No ready tickets"; then
    echo "$(date '+%H:%M') nothing ready; waiting"; sleep 60; continue
  fi
  id=$(echo "$ticket" | head -1 | cut -d' ' -f1)
  echo "$(date '+%H:%M') working on $id"
  git checkout -q main && git pull -q --ff-only
  claude -p --permission-mode acceptEdits --max-turns 80 "You are $AGENTORCH_AGENT, working unattended.
Your Linear ticket (already claimed for you):

$ticket

1. Create the branch printed above from main.
2. Implement the ticket and meet its acceptance criteria. Run the project's tests.
3. Commit, push the branch, and open a PR with gh pr create, titled [$id] <ticket title>, with 'Fixes $id' in the body.
4. Run: agentorch ticket done $id --pr <the PR URL>
5. If you changed an API, schema or env var others use, run: agentorch board post \"<what changed>\"
If you cannot finish, run: agentorch ticket comment $id \"<what blocked you>\" and stop." \
    2>&1 | tee -a ~/pc-agent-log.txt
  git checkout -q main
  sleep 10
done
```

Run it in a terminal you leave open, or in tmux: `tmux new -s agent ~/pc-agent-loop.sh`.

**Windows (PowerShell).** Save as `pc-agent-loop.ps1`:

```powershell
Set-Location "$HOME\pc-agent-work"
while ($true) {
  $ticket = (agentorch ticket next 2>&1 | Out-String)
  if ($LASTEXITCODE -ne 0 -or $ticket -match "^No ready tickets") {
    Write-Host "$(Get-Date -Format HH:mm) nothing ready; waiting"; Start-Sleep 60; continue
  }
  $id = ($ticket.Trim() -split "\s+")[0]
  Write-Host "$(Get-Date -Format HH:mm) working on $id"
  git checkout -q main; git pull -q --ff-only
  $prompt = @"
You are $env:AGENTORCH_AGENT, working unattended. Your Linear ticket (already claimed for you):

$ticket

1. Create the branch printed above from main.
2. Implement the ticket and meet its acceptance criteria. Run the project's tests.
3. Commit, push the branch, and open a PR with gh pr create, titled [$id] <ticket title>, with 'Fixes $id' in the body.
4. Run: agentorch ticket done $id --pr <the PR URL>
5. If you changed an API, schema or env var others use, run: agentorch board post "<what changed>"
If you cannot finish, run: agentorch ticket comment $id "<what blocked you>" and stop.
"@
  claude -p --permission-mode acceptEdits --max-turns 80 $prompt 2>&1 | Tee-Object -Append "$HOME\pc-agent-log.txt"
  git checkout -q main
  Start-Sleep 10
}
```

Run it: `powershell -ExecutionPolicy Bypass -File .\pc-agent-loop.ps1`

### 7.4 While it runs

- **Watch:** the terminal shows which ticket it's on, and `~/pc-agent-log.txt` has
  Claude's full output. In Linear, filter by the label `agent:pc-claude`.
- **Pause:** press Ctrl-C. The ticket it was on stays in **In Development**. Either move
  it back to **Todo** in Linear, or finish it by hand.
- **Stuck ticket:** if Claude stops without opening a PR, the ticket stays In Development
  with a comment explaining why. Fix it by hand, or move it back to Todo to retry.
- **Before shutting down:** stop the loop, make sure nothing is left half-done
  (`git status` in `~/pc-agent-work`), and post on the board:
  `agentorch board post "pc-claude going offline"`.

### 7.5 Checklist for the PC

- [ ] Plugged in, sleep off, updates paused
- [ ] `claude` logged in, `gh auth login` done
- [ ] agentorch installed, `agentorch doctor` shows Linear and the board as ok
- [ ] Environment variables set, with its own agent name (e.g. `pc-claude`), and Sam told the name
- [ ] Repo cloned into `~/pc-agent-work`, used only by the loop
- [ ] `.claude/settings.local.json` allow-list in place
- [ ] Loop script saved and tested (`agentorch ticket next --peek` shows a ticket)
