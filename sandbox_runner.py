"""Run BanyanCode and repository tests in a disposable mount/PID namespace."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time


class Stopped(Exception):
    """The operator or service manager stopped an active task."""


class Sandbox:
    def __init__(self, runner, checkout, timeout):
        self.runner = runner
        self.checkout = checkout
        self.timeout = timeout

    def invoke(self, worktree, home, log, stop, args, timeout, network, policy=None):
        if stop():
            raise Stopped("Cancellation or shutdown requested")
        command = [
            "/usr/bin/time", "-f", "wall=%e user_cpu=%U system_cpu=%S peak_rss_kib=%M",
            "/usr/bin/prlimit", "--fsize=16777216", "--",
            "/usr/bin/bwrap", "--unshare-user", "--unshare-pid", "--unshare-ipc",
            "--unshare-uts", "--die-with-parent", "--new-session", "--clearenv",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind", str(worktree), str(worktree),
            "--ro-bind", str(worktree / ".git"), str(worktree / ".git"),
            "--ro-bind", str(self.checkout / ".git"), str(self.checkout / ".git"),
            "--bind", str(home), "/agent-home",
            "--setenv", "HOME", "/agent-home", "--setenv", "PATH", "/usr/bin",
            "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            "--chdir", str(worktree),
        ]
        for path in ("/etc/alternatives", "/etc/pki/ca-trust", "/etc/pki/tls/certs", "/etc/crypto-policies",
                     "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
            command += ["--ro-bind", path, path]
        if network:
            command += ["--ro-bind", str(Path(self.runner.executable).parent), "/runner"]
        else:
            command += ["--unshare-net"]
        if policy is not None:
            command += ["--ro-bind", str(policy), str(worktree / "banyancode.json")]
        with log.open("ab") as output:
            with subprocess.Popen(
                command + ["--", *args], stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            ) as child:
                deadline = time.monotonic() + timeout
                try:
                    while child.poll() is None:
                        if stop():
                            raise Stopped("Cancellation or shutdown requested")
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Sandbox exceeded {timeout} seconds")
                        time.sleep(0.2)
                    if child.returncode:
                        raise RuntimeError(f"Sandbox exited {child.returncode}. See {log.name}")
                finally:
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGTERM)
                        try:
                            child.wait(timeout=4)
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid, signal.SIGKILL)
                            child.wait()

    def edit(self, worktree, home, log, stop, prompt):
        policy = home.parent / "policy.json"
        policy.write_text(json.dumps({
            "permission": {"*": "deny", "read": "allow", "edit": "allow",
                           "external_directory": "deny"},
            "banyancode_max_subagents": 1,
            "banyancode_disable_websearch": True,
            "banyancode_codegraph_watch_enabled": False,
        }), encoding="utf-8")
        config = worktree / "banyancode.json"
        if config.is_symlink():
            raise ValueError("BanyanCode configuration cannot be a symlink")
        placeholder = not config.exists()
        if placeholder:
            config.touch(exist_ok=False)
        try:
            binary = "/runner/" + Path(self.runner.executable).name
            self.invoke(worktree, home, log, stop, self.runner.command(prompt, binary),
                        self.timeout, network=True, policy=policy)
        finally:
            if placeholder and config.is_file() and not config.is_symlink() and not config.stat().st_size:
                config.unlink()

    def verify(self, worktree, home, log, stop):
        self.invoke(worktree, home, log, stop, ["/usr/bin/python3", "-m", "unittest", "-v"],
                    90, network=False)
