"""Run agents and verification commands confined to a worktree.

``bwrap`` (bubblewrap, Linux) mounts only the system directories, the agent's
installation, the worktree and a disposable home, with a fresh PID namespace;
verification also loses network access. ``none`` runs the process directly
with a scrubbed environment and a disposable home, for laptops and macOS.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

ETC = ("/etc/alternatives", "/etc/pki", "/etc/ssl", "/etc/ca-certificates", "/etc/crypto-policies",
       "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/localtime", "/etc/passwd",
       "/etc/group")
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
MAX_LOG_BYTES = 16 * 1024 * 1024


class Stopped(Exception):
    """The operator cancelled the task or the service is stopping."""


class Runner:
    def __init__(self, kind: str = "bwrap", binds: Sequence[str] = ()):
        if kind not in ("bwrap", "none"):
            raise ValueError("sandbox type must be bwrap or none")
        if kind == "bwrap" and not shutil.which("bwrap"):
            raise ValueError("bubblewrap (bwrap) is not installed; install it or set sandbox.type to none")
        self.kind = kind
        self.binds = [os.path.realpath(os.path.expanduser(b)) for b in binds]

    @property
    def sandboxed(self) -> bool:
        return self.kind == "bwrap"

    # ------------------------------------------------------------------------
    def _bwrap(self, argv: List[str], worktree: Path, home: Path, git_dirs: Sequence[Path],
               env: Dict[str, str], network: bool, overrides: Dict[Path, Path]) -> List[str]:
        command = [
            "bwrap", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--die-with-parent", "--new-session", "--clearenv",
            "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        ]
        for link in ("bin", "sbin", "lib", "lib64"):
            target = Path("/") / link
            if target.is_symlink():
                command += ["--symlink", os.readlink(target), str(target)]
            elif target.is_dir():
                command += ["--ro-bind", str(target), str(target)]
        for path in ETC:
            if os.path.exists(path):
                command += ["--ro-bind", path, path]
        mounted = set()
        executable_dirs = {os.path.dirname(argv[0]), os.path.dirname(os.path.realpath(argv[0]))}
        for path in sorted(executable_dirs) + self.binds:
            if path.startswith("/usr/") or path in mounted or not os.path.exists(path):
                continue
            mounted.add(path)
            command += ["--ro-bind", path, path]
        command += ["--bind", str(worktree), str(worktree)]
        for git_dir in git_dirs:
            command += ["--ro-bind", str(git_dir), str(git_dir)]
        command += ["--bind", str(home), str(home)]
        for target, source in overrides.items():
            command += ["--ro-bind", str(source), str(target)]
        if not network:
            command.append("--unshare-net")
        for key, value in env.items():
            command += ["--setenv", key, value]
        command += ["--chdir", str(worktree), "--"]
        return command + argv

    def run(self, argv: List[str], worktree: Path, home: Path, log: Path,
            stop: Callable[[], bool], timeout: int, network: bool,
            env: Optional[Dict[str, str]] = None, git_dirs: Sequence[Path] = (),
            config_files: Optional[Dict[str, str]] = None) -> None:
        if stop():
            raise Stopped("Cancellation or shutdown requested")
        found = shutil.which(argv[0])
        if not found:
            raise ValueError(f"Command not found: {argv[0]}")
        found = os.path.abspath(found)
        if os.path.realpath(os.path.dirname(found)) != os.path.dirname(found):
            # A symlinked directory cannot be recreated inside the sandbox.
            found = os.path.realpath(found)
        argv = [found] + list(argv[1:])
        base_env = {
            "HOME": str(home),
            "PATH": ":".join([SYSTEM_PATH, os.path.dirname(argv[0])]
                             + [b for b in self.binds if b.endswith("/bin")]),
            "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "CI": "1", "NO_COLOR": "1",
            "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_CACHE_HOME": str(home / ".cache"), "TMPDIR": "/tmp" if self.sandboxed else str(home / "tmp"),
        }
        base_env.update(env or {})
        (home / "tmp").mkdir(parents=True, exist_ok=True)
        placed: List[Path] = []
        restored: Dict[Path, bytes] = {}
        overrides: Dict[Path, Path] = {}
        try:
            for relative, content in (config_files or {}).items():
                target = worktree / relative
                if target.is_symlink():
                    raise ValueError(f"{relative} in the repository cannot be a symlink")
                if self.sandboxed:
                    source = home.parent / ("override-" + relative.replace("/", "_"))
                    source.write_text(content, encoding="utf-8")
                    if not target.exists():
                        target.touch()
                        placed.append(target)
                    overrides[target] = source
                else:
                    if target.exists():
                        restored[target] = target.read_bytes()
                    else:
                        placed.append(target)
                    target.write_text(content, encoding="utf-8")
            if self.sandboxed:
                command = self._bwrap(argv, worktree, home, git_dirs, base_env, network, overrides)
                popen_env = {"PATH": os.environ.get("PATH", SYSTEM_PATH)}
                cwd = None
            else:
                command = argv
                popen_env = base_env
                cwd = str(worktree)
            self._supervise(command, cwd, popen_env, log, stop, timeout)
        finally:
            for target in placed:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            for target, content in restored.items():
                target.write_bytes(content)

    @staticmethod
    def _supervise(command: List[str], cwd: Optional[str], env: Dict[str, str], log: Path,
                   stop: Callable[[], bool], timeout: int) -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as output:
            output.write(("\n$ " + " ".join(command[-6:])[:500] + "\n").encode())
            output.flush()
            child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + timeout
            try:
                while child.poll() is None:
                    if stop():
                        raise Stopped("Cancellation or shutdown requested")
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Exceeded {timeout} seconds")
                    if log.stat().st_size > MAX_LOG_BYTES:
                        raise RuntimeError("Output exceeded 16 MiB")
                    time.sleep(0.25)
                if child.returncode:
                    raise RuntimeError(f"Exited with status {child.returncode}; see {log}")
            finally:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                    except ProcessLookupError:
                        pass
