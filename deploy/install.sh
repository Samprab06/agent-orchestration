#!/usr/bin/env bash
# Install or update agentorch for the current user on a Linux server.
#
#   deploy/install.sh            install code, CLI, config skeleton, service, Hermes skill
#   deploy/install.sh --start    ...and (re)start the worker service
#
# Options: --no-service, --no-hermes, --start, --python PATH
# Safe to re-run: it never overwrites your config or token, and keeps
# previous code versions under ~/.local/share/agentorch/app/.
set -euo pipefail

src="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
python="${AGENTORCH_PYTHON:-python3}"
service=1 hermes=1 start=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-service) service=0 ;;
    --no-hermes) hermes=0 ;;
    --start) start=1 ;;
    --python) python="$2"; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }

echo "agentorch install from $src"
command -v "$python" >/dev/null || { echo "python3 not found" >&2; exit 1; }
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "Python 3.9+ required" >&2; exit 1; }
python="$(command -v "$python")"
command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v bwrap >/dev/null || warn "bubblewrap not found: install it (dnf/apt install bubblewrap) or set sandbox.type to none"

umask 077
app="$HOME/.local/share/agentorch/app"
rev="$(git -C "$src" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
dest="$app/$rev"
mkdir -p "$app" "$HOME/.local/bin" "$HOME/.config/agentorch"
chmod 700 "$HOME/.config/agentorch"
rm -rf "$dest.tmp"
mkdir -p "$dest.tmp"
(cd "$src" && tar --exclude='__pycache__' -cf - agentorch) | (cd "$dest.tmp" && tar -xf -)
rm -rf "$dest"
mv "$dest.tmp" "$dest"
ln -sfn "$dest" "$app/current"
say "code      $dest"

cat > "$HOME/.local/bin/agentorch" <<EOF
#!/bin/sh
PYTHONPATH="$app/current\${PYTHONPATH:+:\$PYTHONPATH}" exec "$python" -m agentorch "\$@"
EOF
chmod 755 "$HOME/.local/bin/agentorch"
say "cli       $HOME/.local/bin/agentorch ($("$HOME/.local/bin/agentorch" --version))"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "add ~/.local/bin to PATH" ;; esac

config="$HOME/.config/agentorch/config.json"
if [ -e "$config" ]; then
  say "config    $config (kept)"
else
  install -m 600 "$src/deploy/config.example.json" "$config"
  say "config    $config (new: edit it before starting the worker)"
fi
token="$HOME/.config/agentorch/github-token"
[ -s "$token" ] || warn "no GitHub token at $token (needed when github.auth is token)"

if [ "$service" = 1 ] && command -v systemctl >/dev/null; then
  unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  install -m 644 "$src/deploy/agentorch-worker.service" "$unit_dir/agentorch-worker.service"
  install -m 644 "$src/deploy/agentorch-supervise.service" "$unit_dir/agentorch-supervise.service"
  install -m 644 "$src/deploy/agentorch-supervise.timer" "$unit_dir/agentorch-supervise.timer"
  systemctl --user daemon-reload
  say "service   $unit_dir/agentorch-worker.service"
  if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo no)" != yes ]; then
    warn "linger is off; the worker stops when you log out. Run: sudo loginctl enable-linger $(id -un)"
  fi
  for legacy in agent-orchestrator.service; do
    if systemctl --user is-enabled "$legacy" >/dev/null 2>&1; then
      warn "old $legacy is still enabled; stop it with: systemctl --user disable --now $legacy"
    fi
  done
fi

if [ "$hermes" = 1 ] && [ -d "$HOME/.hermes/skills" ]; then
  skill="$HOME/.hermes/skills/devops/agentorch"
  mkdir -p "$skill"
  install -m 644 "$src/hermes/agentorch/SKILL.md" "$skill/SKILL.md"
  say "hermes    $skill/SKILL.md"
elif [ "$hermes" = 1 ]; then
  say "hermes    not found (~/.hermes/skills); skipped the skill"
fi

if [ "$start" = 1 ]; then
  "$HOME/.local/bin/agentorch" doctor || { echo "doctor failed; fix the config before starting" >&2; exit 1; }
  systemctl --user enable agentorch-worker.service >/dev/null
  systemctl --user restart agentorch-worker.service
  say "started   systemctl --user status agentorch-worker"
else
  cat <<EOF

Next:
  1. Edit $config (approvers, repositories, agent, notify).
  2. Put a GitHub token in $token (chmod 600), or configure GitHub App auth.
  3. agentorch doctor
  4. agentorch notify test "hello from agentorch"
  5. systemctl --user enable --now agentorch-worker
EOF
fi
