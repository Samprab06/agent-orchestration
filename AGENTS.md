# Working on agentorch

Instructions for agents and humans changing this repository. The rules agentorch
installs into *other* projects are in `agentorch/templates/AGENTS.md`.

## Layout

| Path | Contents |
|---|---|
| `agentorch/cli.py` | argparse entry point for every command |
| `agentorch/config.py` | operator config and `.agentorch.json` |
| `agentorch/github.py` | REST/GraphQL client and credentials (token, gh, GitHub App) |
| `agentorch/board.py` | message board on a pinned issue |
| `agentorch/tasks.py` | task creation and the approval rules |
| `agentorch/worker.py` | worker loop, attempt state, publishing, reporting, status |
| `agentorch/agents.py` | headless agent backends |
| `agentorch/sandbox.py` | bubblewrap and unsandboxed runners |
| `agentorch/notify.py` | notifiers (signal-cli, command, stdout) |
| `agentorch/templates/` | files `agentorch init` writes into projects |
| `hermes/agentorch/SKILL.md` | Hermes skill for phone control |
| `deploy/` | install script, systemd unit, example config |
| `tests/` | unittest suite; `tests/fakes.py` fakes GitHub |

## Rules

- Python 3.9 compatible, standard library only. The reference server runs the
  system Python 3.9. Use `from __future__ import annotations` for type hints.
- Keep secrets out of argv, URLs and logs. Git gets tokens through `git_auth_env`.
- Approval and safety checks (`tasks.find_approval`, `RepoWorker.still_approved`,
  the `.github/` guard, the sandbox) need a test for every behaviour change.
- Documentation is part of the change: update `README.md`, `SETUP.md` and the
  config table when you add or change options.

## Test

```sh
python3 -m unittest discover -s tests -t .
```

Worker tests run real git against temporary bare repositories. Bubblewrap tests
run when `bwrap` works on the machine and are skipped otherwise.
