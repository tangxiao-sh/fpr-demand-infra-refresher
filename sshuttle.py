"""Lifecycle management for one long-lived project sshuttle process."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Sequence
import shutil
import signal
import subprocess

from config import ProjectConfig


LOG = logging.getLogger("accessor.sshuttle")

# These commands are intentionally fixed in code rather than read from a shell
# string. That avoids command injection and makes their privileged scope clear.
NETWORK_PREP_COMMANDS = (
    ("dscacheutil", "-flushcache"),
    ("killall", "-HUP", "mDNSResponder"),
    ("pfctl", "-f", "/etc/pf.conf"),
)


def prepare_network_before_proxy(password_provider: Callable[[], str] | None = None) -> bool:
    """Run required macOS network setup with a terminal-only sudo prompt.

    `sudo -v` inherits this process's stdin/stdout/stderr, so macOS displays a
    normal terminal password prompt and hides typed characters. Subsequent
    `sudo -n` calls explicitly refuse to prompt again or invoke an askpass GUI.
    """
    LOG.info("Network preparation requires sudo")
    # A user may have configured SUDO_ASKPASS globally. Remove it so a missing
    # terminal fails safely instead of falling back to a graphical password UI.
    terminal_env = os.environ.copy()
    terminal_env.pop("SUDO_ASKPASS", None)
    try:
        if password_provider is None:
            LOG.info("Enter the password in this terminal")
            validated = subprocess.run(
                ["sudo", "-p", "Accessor sudo password: ", "-v"],
                env=terminal_env,
                check=False,
            )
        else:
            password = password_provider()
            # `-S` consumes the password from stdin. This lets the curses UI
            # keep its screen active while still using sudo's normal timestamp
            # cache for the following non-interactive commands.
            validated = subprocess.run(
                ["sudo", "-S", "-p", "", "-v"],
                input=f"{password}\n",
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=terminal_env,
                check=False,
            )
        if validated.returncode != 0:
            LOG.error("sudo authentication failed; proxy will not start")
            return False
        for command in NETWORK_PREP_COMMANDS:
            # In the prompt_toolkit UI a password provider is supplied. Never
            # let privileged command output write directly to the terminal: a
            # single sudo/PF diagnostic would corrupt the live screen.
            hidden_output = (
                {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
                if password_provider is not None
                else {}
            )
            result = subprocess.run(
                ["sudo", "-n", *command], env=terminal_env, check=False, **hidden_output
            )
            if result.returncode != 0:
                LOG.error("Network preparation failed: %s", " ".join(command))
                return False
    except OSError as error:
        LOG.error("Unable to execute sudo network preparation: %s", error)
        return False
    return True


class SshuttleProcess:
    """Start, inspect, and stop one project script that owns sshuttle."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[object] | None = None
        self.log_handle: object | None = None
        self.log_path: Path | None = None

    def is_alive(self) -> bool:
        """Do not restart on every credential rotation; only inspect on schedule."""
        return self.process is not None and self.process.poll() is None

    @staticmethod
    def check_health(urls: Sequence[str]) -> tuple[int, int]:
        """Return successful/total private endpoint probes without exposing output.

        The health URLs are routed through sshuttle. ``--noproxy '*'`` is
        important on developer laptops: a corporate HTTP proxy could otherwise
        make a healthy response bypass the tunnel and give a false green result.
        """
        if not urls:
            return 0, 0
        curl = shutil.which("curl")
        if curl is None:
            return 0, len(urls)
        successful = 0
        for url in urls:
            try:
                result = subprocess.run(
                    [
                        curl,
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--location",
                        "--noproxy",
                        "*",
                        "--connect-timeout",
                        "5",
                        "--max-time",
                        "15",
                        url,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            successful += result.returncode == 0
        return successful, len(urls)

    @staticmethod
    def find_external_proxy_pids() -> tuple[int, ...]:
        """Find an sshuttle/proxy launched outside this Accessor instance.

        A developer may start proxy from a project terminal before opening
        Accessor. Such a connection is usable but must never be stopped or
        duplicated by this process, so it is reported separately.
        """
        pgrep = shutil.which("pgrep")
        if pgrep is None:
            return ()
        process_ids: set[int] = set()
        for pattern in ("sshuttle", "establish_proxy_connection.py"):
            try:
                result = subprocess.run(
                    [pgrep, "-f", pattern],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            process_ids.update(
                int(line) for line in result.stdout.splitlines() if line.isdigit()
            )
        return tuple(sorted(process_ids))

    @staticmethod
    def has_pf_sshuttle_anchor() -> bool:
        """Check macOS PF state for sshuttle rules without prompting for sudo.

        sshuttle's forwarding helper commonly runs as root, which macOS may
        hide from a regular user's process list. Its PF anchors remain visible
        through `sudo -n` while the existing sudo timestamp is valid. `-n`
        deliberately returns false instead of interrupting the menu for a
        password prompt.
        """
        try:
            result = subprocess.run(
                ["sudo", "-n", "pfctl", "-s", "Anchors"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and any(
            "sshuttle" in line.lower() for line in result.stdout.splitlines()
        )

    def start(self, project: ProjectConfig, prepare_network: bool = True) -> bool:
        """Run proxy in a detached session after foreground sudo preparation."""
        if self.is_alive():
            return True
        if prepare_network and not prepare_network_before_proxy():
            return False
        LOG.info("Starting sshuttle for %s", project.name)
        try:
            # sudo -v has completed before this point. The child can therefore
            # be detached: it cannot steal menu input or render over the status
            # screen, while its sshuttle output remains available in a log file.
            self.log_path = Path("/tmp") / "accessor-demand-proxy.log"
            self.log_handle = self.log_path.open("a", encoding="utf-8")
            self.process = subprocess.Popen(
                project.proxy_command,
                cwd=project.directory,
                stdin=subprocess.DEVNULL,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            LOG.info("Proxy output is written to %s", self.log_path)
            return True
        except OSError as error:
            LOG.error("Unable to start sshuttle for %s: %s", project.name, error)
            self.process = None
            if self.log_handle is not None:
                self.log_handle.close()
                self.log_handle = None
            return False

    @staticmethod
    def _descendant_pids(root_pid: int) -> list[int]:
        """Return descendants before the parent exits and reparents them."""
        pgrep = shutil.which("pgrep")
        if pgrep is None:
            return []
        descendants, pending = [], [root_pid]
        while pending:
            parent = pending.pop()
            try:
                result = subprocess.run(
                    [pgrep, "-P", str(parent)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            children = [int(line) for line in result.stdout.splitlines() if line.isdigit()]
            descendants.extend(children)
            pending.extend(children)
        return descendants

    @staticmethod
    def _signal(process_ids: Sequence[int], signum: int) -> None:
        for process_id in process_ids:
            try:
                os.kill(process_id, signum)
            except (ProcessLookupError, PermissionError):
                pass

    def stop(self, project: ProjectConfig) -> None:
        """Stop the Python script, its shell, and sshuttle when Accessor exits."""
        process = self.process
        if process is None or process.poll() is not None:
            self.process = None
            return
        process_ids = [*reversed(self._descendant_pids(process.pid)), process.pid]
        LOG.info("Stopping sshuttle process tree rooted at %s", process.pid)
        try:
            self._signal(process_ids, signal.SIGTERM)
            process.wait(timeout=project.shutdown_grace_seconds)
        except subprocess.TimeoutExpired:
            LOG.warning("sshuttle did not stop gracefully; sending SIGKILL")
            self._signal(process_ids, signal.SIGKILL)
            process.wait(timeout=5)
        finally:
            self.process = None
            if self.log_handle is not None:
                self.log_handle.close()
                self.log_handle = None
