# Hackathon Agent Orchestrator

This checkout contains the GitHub approval preflight and PR verification for
Stage 1. The preflight only reads GitHub and exits. It does not dispatch tasks.

## Verify an approval

Requires Python 3.9 or newer. Use a GitHub token with issue read access in
`GH_TOKEN` for private repositories. For a public repository, anonymous reads
work subject to GitHub's rate limit. Set `AGENT_APPROVERS` to a comma-separated
list of approved GitHub logins.

1. Create a task issue and apply `agent:queued`.
2. Generate the command with `python3 orchestrator.py OWNER/REPO ISSUE`.
3. Have an allowlisted collaborator post that exact command on the issue.
4. Run `python3 orchestrator.py OWNER/REPO ISSUE --check`.

The check exits 0 for an eligible issue, 1 for an ineligible issue, and 2 for
configuration or GitHub errors. If a task changes, regenerate the approval
command. If GitHub rejects a request, check repository access and rate limits.

Run the regression tests with `python3 -m unittest -v`.

## Oracle worktree smoke test

On ARM64 Linux with Python 3.9+, Git, and GNU timeout, install the pinned
BanyanCode binaries into a new directory and run the fixture:

```sh
python3 install_banyancode.py bin
python3 smoke.py --banyancode bin/banyancode --log smoke.jsonl
```

The fixture creates a disposable Git repository and task worktree. BanyanCode
uses a fresh home and the `opencode/beam-free` model. The test verifies the
expected file contents, checks that no other worktree files changed, commits
the edit on the task branch, and checks that the base checkout stayed clean.
The fixture is removed on exit. The log remains at the supplied path.

If the provider rejects the request or times out, inspect the JSON event log
and retry manually or pass a verified model with `--model`. The smoke runner
accepts a fixed local prompt. Git worktrees provide branch separation. Deploy
the eventual issue-driven runner under its own restricted OS identity.

## GitHub demo prerequisites

Use a dedicated repository-scoped GitHub credential for the worker. The
eventual draft PR path needs Issues, Contents, and Pull requests read/write.
Keep it outside the checkout and separate from the coding-agent process.
Select a human GitHub approver and a repository for the first task.

The live dispatch, draft PR, board updates, systemd service, and MCP server
are pending.
