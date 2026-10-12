"""Per-proxy process supervision.

Every proxy port runs as its own supervised 3proxy instance, outside the API's
process group and cgroup: restarting or upgrading the API, or one proxy dying,
never interrupts the others. Every mutating call names exactly one instance by
port; nothing here acts on all instances at once.

Migration from the base version (bdfe9c7/main): there every 3proxy was a child
of the API, inside its cgroup. The first API start after the upgrade stops them
(systemd stops the old API cgroup) and reconciliation brings each proxy back as
its own unit. That is a single, explicit interruption at upgrade time, never
part of a routine mutation.
"""
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

# Fixed by deploy/ipv6-proxy@.service (ExecStart reads %i.cfg from here).
SYSTEMD_CONFIG_DIR = Path("/var/lib/ipv6-proxy-api/proxies")
STOP_TIMEOUT = 2  # seconds before SIGKILL; matches TimeoutStopSec= intent for a stateless proxy
UNIT_PATTERN = re.compile(r"^ipv6-proxy@(\d+)\.service\s+\S+\s+(\S+)\s+(\S+)", re.MULTILINE)

def _port(port):
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid port {port}")
    return port

class Supervisor(Protocol):
    def start(self, port: int, config_path: Path) -> None: ...
    def stop(self, port: int) -> None: ...
    def restart(self, port: int) -> None: ...
    def main_pid(self, port: int) -> Optional[int]: ...
    def is_active(self, port: int) -> bool: ...
    def list_managed(self) -> set: ...
    def list_active(self) -> set: ...
    def list_states(self) -> dict: ...

def run_command(argv, check=True):
    try:
        return subprocess.run(argv, check=check, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"{' '.join(argv)} failed ({error.returncode}): {error.stderr.strip()}") from error

class SystemdSupervisor:
    """Production: one `ipv6-proxy@<port>.service` instance per proxy."""

    def __init__(self, run=run_command, config_dir=SYSTEMD_CONFIG_DIR):
        self.run = run
        self.config_dir = config_dir

    @staticmethod
    def unit(port):
        return f"ipv6-proxy@{_port(port)}.service"

    def _systemctl(self, *args, check=True):
        return self.run(["systemctl", *args], check)

    def start(self, port, config_path):
        if Path(config_path) != self.config_dir / f"{_port(port)}.cfg":
            raise RuntimeError(f"ipv6-proxy@.service reads {self.config_dir}/<port>.cfg, not {config_path}")
        # enable: the instance comes back on boot without waiting for the API.
        self._systemctl("enable", "--now", self.unit(port))

    def stop(self, port):
        self._systemctl("disable", "--now", self.unit(port))

    def restart(self, port):
        self._systemctl("restart", self.unit(port))

    def is_active(self, port):
        return self._systemctl("is-active", "--quiet", self.unit(port), check=False).returncode == 0

    def main_pid(self, port):
        if not self.is_active(port):
            return None
        value = self._systemctl("show", "--property=MainPID", "--value", self.unit(port)).stdout.strip()
        return int(value or 0) or None

    def list_states(self):
        """port -> active | activating | failed | ...; one read for the whole pool (read-only)."""
        out = self._systemctl("list-units", "--all", "--plain", "--no-legend", "--type=service",
                              "ipv6-proxy@*.service").stdout
        # A unit waiting for Restart=on-failure is "activating", sub "auto-restart".
        return {int(port): "activating" if sub == "auto-restart" else active
                for port, active, sub in UNIT_PATTERN.findall(out)}

    def list_managed(self):
        return set(self.list_states())

    def list_active(self):
        return {port for port, state in self.list_states().items() if state == "active"}

def _starttime(pid):
    """Kernel start time of a live (non-zombie) pid; None otherwise. Guards against pid reuse."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None
    return None if fields[0] in "ZXx" else fields[19]

class LocalProcessSupervisor:
    """Dev/tests without systemd: one detached 3proxy per port (own session),
    pidfile and log per port beside its config. No automatic restart: the next
    API start reconciles dead instances."""

    def __init__(self, binary, directory):
        self.binary = Path(binary)
        self.directory = Path(directory)
        self.children = {}  # port -> Popen we spawned; polled so a dead proxy never stays a zombie

    def _pidfile(self, port):
        return self.directory / f"{_port(port)}.pid"

    def log_path(self, port):
        return self.directory / f"{_port(port)}.log"

    def main_pid(self, port):
        try:
            pid, start = self._pidfile(port).read_text().split()
            pid = int(pid)
        except (FileNotFoundError, ValueError):
            return None
        child = self.children.get(port)
        if child is not None and child.pid == pid:
            child.poll()
        else:
            try:
                os.waitpid(pid, os.WNOHANG)  # adopted from an earlier API object of this process
            except ChildProcessError:
                pass
        return pid if _starttime(pid) == start else None

    def is_active(self, port):
        return self.main_pid(port) is not None

    def start(self, port, config_path):
        if self.is_active(port):
            raise RuntimeError(f"proxy instance {port} is already running")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.log_path(port).open("ab") as log:
            child = subprocess.Popen([str(self.binary), str(config_path)], stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
        self.children[_port(port)] = child
        temporary = self._pidfile(port).with_suffix(".tmp")
        temporary.write_text(f"{child.pid} {_starttime(child.pid) or 0}\n")
        os.replace(temporary, self._pidfile(port))

    def stop(self, port):
        pid = self.main_pid(port)
        if pid is not None:
            # Same as KillSignal= of the unit: 3proxy exits at once on SIGINT
            # (default action), while SIGTERM takes ~5 s in its own loop.
            os.kill(pid, signal.SIGINT)
            deadline = time.monotonic() + STOP_TIMEOUT
            while self.main_pid(port) is not None and time.monotonic() < deadline:
                time.sleep(0.02)
            if self.main_pid(port) is not None:
                logger.warning("proxy %s ignored SIGINT; killing pid %s", port, pid)
                os.kill(pid, signal.SIGKILL)
                while self.main_pid(port) is not None:
                    time.sleep(0.02)
        self._pidfile(port).unlink(missing_ok=True)
        self.children.pop(_port(port), None)

    def restart(self, port):
        self.stop(port)
        self.start(port, self.directory / f"{_port(port)}.cfg")

    def list_managed(self):
        return {int(path.stem) for path in self.directory.glob("*.pid") if path.stem.isdigit()}

    def list_active(self):
        return {port for port in self.list_managed() if self.is_active(port)}

    def list_states(self):
        return {port: "active" if self.is_active(port) else "failed" for port in self.list_managed()}
