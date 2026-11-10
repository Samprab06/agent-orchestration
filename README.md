# Hackathon Quick Start

Use [the shared GitHub board](https://github.com/Samprab06/agent-orchestration/issues/9) for handoffs, [Issues](https://github.com/Samprab06/agent-orchestration/issues) for tasks, and draft PRs for review. Each teammate signs in with their own GitHub account. Ask the repository owner for collaborator access before trying to assign issues or update labels. Keep GitHub and model credentials in your personal client configuration.

## Create, assign, and approve a task

1. Open an issue in the project repository. Give it a small scope and a clear verification step. Assign the teammate responsible for the work.
2. Apply `agent:queued` if the task is proposed for Oracle. From the orchestration checkout, generate the approval command:

   ```sh
   python3 orchestrator.py Samprab06/agent-orchestration ISSUE_NUMBER
   ```

   Use the actual repository name for a project task. For a private repository, set `GH_TOKEN` locally to your own credential with issue read access for this command.
3. **Samprab06** posts the exact command on the task issue. Generate a new command after editing its title or body. The Oracle worker checks the author and current task content before running or publishing.
4. Follow the issue for the worker's draft PR. Review and merge through GitHub when ready.

Use the issue assignee as the task owner. Post handoffs on the board with the repository, issue or PR link, your handle and agent name, and a direct mention of the teammate who needs to act. Example: `@teammate please confirm the response shape for GET /items before I finish #12. â€” @my-handle / Codex`. Use `agent:blocked` for a dependency that prevents work, and remove it when the dependency clears. The worker reports `agent:running`, `agent:review`, `agent:failed`, and `agent:cancelled` on the task issue. Check the linked PR for review and Actions status.

## Connect your coding agent

Use [GitHub's official MCP server](https://github.com/github/github-mcp-server) for the board, issues, assignees, comments, PRs, and Actions. These examples use a personal GitHub token in `GITHUB_PAT_TOKEN`. Set it in the environment of the terminal launching your client. Follow GitHub's [credential guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) for the permissions your account and repository support. GitHub limits fine-grained tokens for outside or repository collaborators. If your token cannot write, check your collaborator access and use an approved GitHub authentication method for your account. Never add a token to a shared config or put it in an agent prompt.

### Codex CLI

```sh
codex mcp add github --url https://api.githubcopilot.com/mcp/ --bearer-token-env-var GITHUB_PAT_TOKEN
codex mcp get github
codex
```

Use `/mcp` in the session to check the connection. See [GitHub's Codex guide](https://github.com/github/github-mcp-server/blob/main/docs/installation-guides/install-codex.md).

### Claude Code

Run this in your own terminal from the project checkout. Claude stores the expanded header in your local MCP configuration.

```sh
claude mcp add --scope local --transport http github https://api.githubcopilot.com/mcp/ -H "Authorization: Bearer $GITHUB_PAT_TOKEN"
claude mcp list
claude
```

In PowerShell, substitute `$env:GITHUB_PAT_TOKEN` in the header. See [GitHub's Claude guide](https://github.com/github/github-mcp-server/blob/main/docs/installation-guides/install-claude.md).

### BanyanCode

Register the server in your personal BanyanCode config with the CLI. This
stores the expanded header in your local configuration.

```sh
banyancode mcp add --global --url https://api.githubcopilot.com/mcp/ --header "Authorization=Bearer $GITHUB_PAT_TOKEN" github
banyancode mcp list
```

In PowerShell, use `$env:GITHUB_PAT_TOKEN`. Start BanyanCode from the project
checkout after registration.

### Google Antigravity

Open the MCP manager with `/mcp` in the CLI or use **Manage MCP Servers** in the IDE. In your personal `~/.gemini/config/mcp_config.json`, merge this entry and replace the placeholder locally. Reload the MCP server.

```json
{
  "mcpServers": {
    "github": {
      "serverUrl": "https://api.githubcopilot.com/mcp/",
      "headers": { "Authorization": "Bearer YOUR_PERSONAL_GITHUB_TOKEN" }
    }
  }
}
```

See [Google's Antigravity MCP guide](https://antigravity.google/docs/mcp).

### Try the team workflow

Ask your agent to use GitHub MCP to read the board at `Samprab06/agent-orchestration#9`, find a task and its assignee, and read its linked draft PR and check status. For a write check, ask it to post a harmless comment on [the onboarding test issue](https://github.com/Samprab06/agent-orchestration/issues/5). Leave that issue unapproved. Use the issue or board to post dependency notes and tag the teammate responsible. A live teammate sign-in and write test is deferred until hackathon onboarding.

### Optional local delegation to BanyanCode

BanyanCode 26.11.2 also exposes its own task tools over stdio. Start it on your machine in a disposable project first. This server uses your local BanyanCode model setup. Team coordination still uses GitHub MCP.

```sh
codex mcp add banyan -- banyancode mcp serve --cwd /absolute/project/path --permission reject --max-tasks 1
claude mcp add --transport stdio banyan -- banyancode mcp serve --cwd /absolute/project/path --permission reject --max-tasks 1
```

For Antigravity, the equivalent personal MCP entry uses `"command": "banyancode"` and `"args": ["mcp", "serve", "--cwd", "/absolute/project/path", "--permission", "reject", "--max-tasks", "1"]`. Ask for a harmless no-edit response first. The native server was tested with `tools/list` and a no-edit delegation on 26.11.2.

## Monitor, cancel, retry, or recover

On Oracle, use the operator account:

```sh
root="$HOME/.local/share/agent-orchestrator-worker"
control() {
  python3 "$root/source/worker.py" --config "$HOME/.config/agent-orchestrator/repositories.json" --state "$root/state" "$@"
}
systemctl --user status agent-orchestrator.service
tail -n 60 "$root/state/worker.log"
control status
control --repository Samprab06/agent-orchestration cancel ISSUE_NUMBER
control --repository Samprab06/agent-orchestration retry ISSUE_NUMBER
```

For a retry, wait for the failure report, reapply `agent:queued`, and check that the issue still has current approval. If the service stops, inspect the log, then run `systemctl --user restart agent-orchestrator.service`. Verified commits resume publishing. Interrupted coding worktrees stay on disk for inspection and require an explicit retry.

## Add the hackathon repository

After creation, the owner confirms the exact `Samprab06/NAME` and adds only that repository to the existing App installation. Create the worker labels in that repository and add the repository name and base branch to the Oracle allowlist. Restart the service after any active task finishes. The worker creates a separate checkout and state directory and requests a token scoped to that repository. The first check should use a small approved task with a verification step.

See the [Oracle setup and recovery guide](deploy/README.md) for installation from a Git commit, the allowlist example, updating the service, and reviewing or removing the retained recovery worktree.
