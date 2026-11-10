# Persistent Oracle worker

The worker handles one approved task at a time in `Samprab06/agent-orchestration`.
It polls every 45 seconds by default. GitHub remains the source of truth for
approval and review. SQLite records local execution and publication checkpoints.

## Install on Oracle

Use a current checkout of the worker source. Run these commands as the non-root
Oracle service account. On this host the account is `opc`. The source and the
worker's clean checkout of GitHub `main` are separate.

```sh
sudo dnf install bubblewrap
umask 077
root="$HOME/.local/share/agent-orchestrator-worker"
install -d -m 700 "$root/source" "$root/state" \
  "$HOME/.config/agent-orchestrator" "$HOME/.config/systemd/user"
install -m 600 worker.py sandbox_runner.py stage1_demo.py smoke.py orchestrator.py \
  github_app.py "$root/source/"
install -m 644 deploy/agent-orchestrator.service "$HOME/.config/systemd/user/"
git clone https://github.com/Samprab06/agent-orchestration.git "$root/checkout"
python3 install_banyancode.py "$root/bin"
```

Use the existing GitHub App key at
`~/.config/agent-orchestrator/github-app.pem`, owned by the service account
with mode `0600`. The App needs Contents, Issues, and Pull requests read/write
on this repository. The worker discovers the installation and refreshes its
short-lived access token in memory. Store these settings in
`~/.config/agent-orchestrator/worker.env` with mode `0600`.

```dotenv
AGENT_APP_ID=5701891
AGENT_MODEL=opencode/beam-free
AGENT_POLL=45
AGENT_TIMEOUT=240
AGENT_PUBLISH_ATTEMPTS=3
```

The repository needs the labels `agent:queued`, `agent:running`,
`agent:failed`, `agent:cancelled`, and `agent:review`. Create any missing labels
before enabling the service. The App can manage issue labels.

```sh
loginctl show-user "$(id -un)" -p Linger
# If Linger=no, enable boot startup for this service account.
sudo loginctl enable-linger "$(id -un)"
systemd-analyze --user verify "$HOME/.config/systemd/user/agent-orchestrator.service"
systemctl --user daemon-reload
systemctl --user enable --now agent-orchestrator.service
```

For updates, wait for the active task to finish, stop the service, copy the
updated source and unit with the same `install` commands, then run
`systemctl --user daemon-reload` and start it again. Keep the SQLite state and
attempt directories intact across updates.

## Submit a task

1. Create a GitHub issue with a concrete code task and add `agent:queued`.
2. Generate the approval with
   `python3 orchestrator.py Samprab06/agent-orchestration ISSUE_NUMBER`.
3. Have `Samprab06` post the exact command on that issue. Regenerate approval
   if the task title or body changes.

The worker checks the current issue and approval before launching BanyanCode,
before pushing, and before opening a draft PR. Each issue gets a deterministic
`agent/issue-N` branch. A task can propose edits to repository files. GitHub
workflow and repository configuration changes under `.github/` require a
separate human-managed PR. The worker runs the repository's Python unittest
suite and requires a nonempty verified diff.

## Operate and troubleshoot

```sh
root="$HOME/.local/share/agent-orchestrator-worker"
control() { python3 "$root/source/worker.py" --state "$root/state" "$@"; }

systemctl --user status agent-orchestrator.service
systemctl --user start agent-orchestrator.service
systemctl --user stop agent-orchestrator.service
systemctl --user restart agent-orchestrator.service
tail -n 60 "$root/state/worker.log"
journalctl _SYSTEMD_USER_UNIT=agent-orchestrator.service -n 60 --no-pager
control status                     # one JSON row per issue, including active phase
control cancel ISSUE_NUMBER        # terminates the active sandbox when possible
control retry ISSUE_NUMBER         # resumes publication or creates a new attempt
```

An operator cancellation preserves the attempt. A stop or crash during coding
marks the task interrupted, reports the failure on GitHub, and retains the
worktree under `state/issue-N/attempt-M/worktree`. Inspect it with
`git -C PATH status --short` and `git -C PATH diff`. An interrupted attempt
never restarts coding automatically.

To retry, wait for the failure report to appear, add `agent:queued` again,
confirm that the current task has a valid approval, then run `control retry N`.
A new coding attempt uses a fresh worktree. An existing verified commit
resumes publication only if its approval still matches. Use a new issue if
the task changed after a commit was verified. The service can cancel work
until the draft PR is created. Review or close any PR that already exists.

Publishing resumes after a restart. It checks the remote branch and looks for
a matching PR before doing either write. A branch at an unexpected commit
requires human investigation. Publication failures retry with exponential
backoff up to `AGENT_PUBLISH_ATTEMPTS`. GitHub status reporting also recovers
after API outages. Completion removes only clean worktrees. Retained
worktrees are operator-owned evidence and should be removed with
`git worktree remove PATH` after their changes are inspected and backed up.

## Limits and logs

The service shares at most one CPU core, applies a 896 MiB memory throttle and
a 1,200 MiB hard limit, and limits the unit to 96 processes and threads.
BanyanCode has a configurable 240-second deadline and verification has a
90-second deadline. The coding sandbox mounts only the worktree, a disposable
home, the runner binary, and runtime dependencies. Repository tests run in a
separate network-disabled sandbox. The App key and Hermes files are outside
both sandboxes.

Per-attempt logs are capped at 16 MiB by the runner's file-size limit. The
worker keeps the newest 20 task logs by default and rotates its own log at
2 MiB with four backups. The cgroup includes the worker and sandbox children.
Read its resource usage while the service is active:

```sh
unit=agent-orchestrator.service
systemctl --user show "$unit" -p CPUUsageNSec -p MemoryCurrent -p MemoryMax
group=$(systemctl --user show "$unit" -p ControlGroup --value)
cat "/sys/fs/cgroup$group/memory.peak"
```
