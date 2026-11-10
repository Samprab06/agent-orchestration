# Hackathon Agent Orchestrator

See [Oracle worker operations](deploy/README.md) for the persistent Stage 2 service.

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

## Run the live GitHub demo

Install a GitHub App only on this repository, with Contents, Issues, and
Pull requests read/write. On Oracle, place its private key at
`~/.config/agent-orchestrator/github-app.pem` with mode `0600`, owned by the
worker. Keep the key outside the checkout. Python 3.9+ and OpenSSL are
required. Pass the numeric App ID as `--app-id`. The worker discovers the
installation, mints a short-lived token restricted to this repository, and
refreshes the token in memory before it expires. The app can create and
update the demo issue. Only `Samprab06` can approve its execution.


From the staging directory containing the source files and `bin/banyancode`,
create or find the disposable queued issue:

```sh
python3 stage1_demo.py --app-id "$APP_ID" --logs logs --create-issue
```

Open the printed issue URL and post the exact approval comment as
`Samprab06`. Then run the worker from Oracle with a clean clone in `checkout`:

```sh
/usr/bin/time -f 'wall=%e user_cpu=%U system_cpu=%S avg_cpu=%P peak_rss_kib=%M' \
  python3 stage1_demo.py --app-id "$APP_ID" --checkout checkout --banyancode bin/banyancode --logs logs
```

The one-shot worker discovers the queued issue and checks approval before
launching BanyanCode and again before pushing. It only accepts the fixed
smoke task and commits only `smoke.txt`. It opens a draft PR and posts its
URL to the issue. Check the PR for the Verify workflow. The runner log is
`logs/issue-<number>.jsonl`.

If the worker reports that the branch exists, check for a previous draft
PR before retrying. A failed PR API call can leave a pushed branch. Open a
draft PR manually from that branch after reviewing the commit. The worker
only runs once per invocation. The Stage 2 service handles queued work separately.
