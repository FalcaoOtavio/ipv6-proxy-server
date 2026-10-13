import json
import logging
import os
import socket
import subprocess
import time
from functools import partial
from ipaddress import IPv6Address
from pathlib import Path

from .instance_address import address_path
from .supervisor import LocalProcessSupervisor, SystemdSupervisor, command_timeout, run_command

logger = logging.getLogger(__name__)

def atomic_json(path, value):
    atomic_write(path, json.dumps(value, indent=2) + "\n")

def atomic_write(path, content):
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

# Destinations a proxy client must never reach through the host: unspecified,
# loopback, IPv4-mapped, link-local, ULA (e.g. IMDS fd00:ec2::254), multicast.
# The configured subnet is appended so one proxy cannot reach another.
PRIVATE_DESTINATIONS = "::/128,::1/128,::ffff:0:0/96,fe80::/10,fc00::/7,ff00::/8"

def render_config(settings, record):
    # One 3proxy process per port: only this record's user is loaded, and
    # restarting it never touches another port's listener or sessions.
    lines = ["nserver 1.1.1.1", "nscache 65536", "maxconn 200",
             "timeouts 1 5 30 60 180 1800 15 60"]
    if record.username:
        lines += [f"users {record.username}:CL:{record.password}", "auth strong"]
    else:
        lines += ["auth iponly"]  # not "auth none": that one skips every ACL, deny included
    if settings.block_private_destinations:
        # 3proxy checks UDP ASSOCIATE only against the client-declared address and
        # relays datagrams unchecked (SSRF to [::1], ADR 0007): refuse it outright.
        lines += ["deny * * * * UDPASSOC"]
        # Checked after name resolution, so "localhost" is refused too. Must precede allow.
        lines += [f"deny * * {PRIVATE_DESTINATIONS},{settings.subnet}"]
    lines += [f"allow {record.username}" if record.username else "allow *"]
    # socks also serves UDP ASSOCIATE (only with the block off); the relay egresses from -e like TCP.
    command = "proxy -6 -n -a" if record.protocol == "http" else "socks -6 -a"
    lines += [f"{command} -p{record.port} -i{settings.bind_ipv4} -e{record.ipv6}"]
    return "\n".join(lines) + "\n"

def listening_inodes(port):
    """Socket inodes in LISTEN on this TCP port, from this network namespace."""
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except FileNotFoundError:
            continue
        for row in rows:
            fields = row.split()
            if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(fields[9])
    return inodes

def owns_socket(pid, inodes):
    try:
        fds = list(Path(f"/proc/{pid}/fd").iterdir())
    except (FileNotFoundError, PermissionError):
        return False
    for fd in fds:
        try:
            if os.readlink(fd).removeprefix("socket:[").removesuffix("]") in inodes:
                return True
        except OSError:
            continue
    return False

class LinuxBackend:
    """Per-target operations on independently supervised 3proxy instances.

    provision/replace/deprovision each touch exactly one proxy: its config
    file, its supervised instance and its own IPv6 address. Nothing here
    restarts, reloads or stops a proxy that is not the target.
    """
    def __init__(self, settings, supervisor=None):
        self.settings = settings
        self.config_dir = settings.data_dir / "proxies"
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if supervisor is None:
            supervisor = (LocalProcessSupervisor(settings.binary, self.config_dir)
                          if settings.supervisor == "local"
                          else SystemdSupervisor(run=partial(run_command, timeout=command_timeout(settings.ready_timeout))))
        self.supervisor = supervisor
        self.serving = {}  # port -> record this backend last brought up or adopted
        self.addresses = set()
        self.journal = settings.data_dir / "owned-addresses.json"
        if self.journal.exists():
            self.addresses = {str(IPv6Address(ip)) for ip in json.loads(self.journal.read_text())}
            if any(IPv6Address(ip) not in settings.subnet for ip in self.addresses):
                raise RuntimeError("Owned address journal does not match configured subnet")
        # Ports this API is about to bring up or take down and whose result is
        # not yet in state.json. Only these may be stopped as orphans at start:
        # a stale or empty state.json must never stop proxies collectively.
        self.intents_path = settings.data_dir / "pending-ports.json"
        self.intents = set(json.loads(self.intents_path.read_text())) if self.intents_path.exists() else set()
        self.orphans_path = settings.data_dir / "orphans.json"
        self.orphans = set()

    def _ip(self, *args):
        return subprocess.run(["ip", *args], check=True, capture_output=True, text=True, timeout=15)

    def existing_addresses(self):
        data = json.loads(self._ip("-j", "-6", "addr", "show", "dev", self.settings.interface).stdout)
        return {entry["local"] for interface in data for entry in interface.get("addr_info", [])}

    def _intend(self, port):
        if port not in self.intents:
            self.intents.add(port)
            atomic_json(self.intents_path, sorted(self.intents))

    def settle(self, ports=None):
        """Called once state.json holds the outcome for these ports (all by default)."""
        remaining = set() if ports is None else self.intents - set(ports)
        if remaining != self.intents:
            self.intents = remaining
            atomic_json(self.intents_path, sorted(remaining))

    def config_path(self, port):
        return self.config_dir / f"{port}.cfg"

    # Addresses: only ones recorded in our journal are ever removed.
    def _claim(self, address, existing=None):
        address = str(address)
        existing = self.existing_addresses() if existing is None else existing
        if address in existing and address not in self.addresses:
            raise RuntimeError(f"IPv6 {address} already belongs to another service")
        if address not in self.addresses:
            # Persist ownership before touching the interface, so a restart can clean partial work.
            self.addresses.add(address)
            atomic_json(self.journal, sorted(self.addresses))
        if address not in existing:
            self._ip("-6", "addr", "add", f"{address}/128", "dev", self.settings.interface, "nodad")

    def _release(self, address):
        address = str(address)
        # A port move keeps the IPv6: the new instance may already be using it.
        if address not in self.addresses or address in {str(r.ipv6) for r in self.serving.values()}:
            return
        if address in self.existing_addresses():
            self._ip("-6", "addr", "del", f"{address}/128", "dev", self.settings.interface)
        self.addresses.discard(address)
        atomic_json(self.journal, sorted(self.addresses))

    def _refuse_foreign_port(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((str(self.settings.bind_ipv4), port))
            except OSError as error:
                raise RuntimeError(f"Port {port} is in use by another process; it was not touched") from error

    def ready(self, port):
        """Port accepts connections AND its listening socket belongs to the target instance."""
        pid = self.supervisor.main_pid(port)
        if pid is None:
            return False
        try:
            with socket.create_connection((str(self.settings.bind_ipv4), port), timeout=0.2):
                pass
        except OSError:
            return False
        return owns_socket(pid, listening_inodes(port))

    def _wait_ready(self, port, since=None):
        """Wait up to ready_timeout counted from `since` (this proxy's own launch)."""
        timeout = self.settings.ready_timeout
        deadline = (time.monotonic() if since is None else since) + timeout
        # Probe before checking the deadline: in a long reconcile a proxy's own
        # deadline may already be past when its turn comes, yet it is up.
        while True:
            if self.ready(port):
                return
            if not self.supervisor.is_active(port) or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        raise RuntimeError(f"3proxy for port {port} did not become ready in {timeout}s "
                           f"(PROXY_READY_TIMEOUT; raise it on slow hosts); inspect its log "
                           f"(journalctl -u ipv6-proxy@{port} or {self.config_dir}/{port}.log)")

    def _write_address(self, record):
        # Read by the instance's ExecStartPre (instance_address) to restore its own /128 on boot.
        path = address_path(self.config_dir, record.port)
        content = json.dumps({"ipv6": str(record.ipv6), "interface": self.settings.interface}, indent=2) + "\n"
        # Unchanged file skipped: it was written atomically and fsynced before,
        # and two fsyncs per adopted proxy dominated reconcile time (A5).
        if not path.exists() or path.read_text() != content:
            atomic_write(path, content)

    def _launch(self, record):
        atomic_write(self.config_path(record.port), render_config(self.settings, record))
        self._write_address(record)
        if self.supervisor.is_active(record.port):
            self.supervisor.restart(record.port)
        else:
            self._refuse_foreign_port(record.port)
            self.supervisor.start(record.port, self.config_path(record.port))

    def _run(self, record):
        self._launch(record)
        self._wait_ready(record.port)
        self.serving[record.port] = record

    def provision(self, record):
        self._intend(record.port)
        self.serving.pop(record.port, None)
        self._claim(record.ipv6)
        self._run(record)

    def replace(self, old, new):
        """Swap one proxy's config in place: add new address, restart only it, then drop the old address."""
        if old.port != new.port:
            self.provision(new)
            self.deprovision(old)
            return
        self.serving.pop(new.port, None)
        self._claim(new.ipv6)
        self._run(new)
        if old.ipv6 != new.ipv6:
            self._release(old.ipv6)

    def deprovision(self, record):
        """Stop only this instance; its address goes only after its process is gone."""
        self._intend(record.port)
        self.serving.pop(record.port, None)
        self.supervisor.stop(record.port)
        self.config_path(record.port).unlink(missing_ok=True)
        address_path(self.config_dir, record.port).unlink(missing_ok=True)
        self._release(record.ipv6)

    def reconcile(self, records):
        """Converge at API start without touching healthy instances.

        Adopts live instances whose config matches; restarts only divergent
        ones; starts missing ones. An instance absent from records is stopped
        only if pending-ports.json says this API was mid-way creating or
        deleting it; any other one is an orphan: kept serving with its config
        and address, logged and listed in orphans.json for manual cleanup.
        Returns the ports that could not be brought up, so the caller fences
        only them.
        """
        failed = set()
        wanted = {record.port: record for record in records}
        unknown = self.supervisor.list_managed() - wanted.keys()
        for port in sorted(unknown & self.intents):
            logger.info("Stopping proxy %s: interrupted create/delete never committed", port)
            self.supervisor.stop(port)
        self.orphans = unknown - self.intents
        for port in sorted(self.orphans):
            logger.warning("Proxy instance %s runs but is not in state.json; left serving. If it is "
                           "really unwanted: systemctl disable --now ipv6-proxy@%s.service, then "
                           "remove %s/%s.cfg and %s.addr", port, port, self.config_dir, port, port)
        atomic_json(self.orphans_path, sorted(self.orphans))
        keep = wanted.keys() | self.orphans
        for stale in [*self.config_dir.glob("*.cfg"), *self.config_dir.glob("*.addr"),
                      *self.settings.data_dir.glob("3proxy-*.cfg")]:
            name = stale.stem.removeprefix("3proxy-")
            if name.isdigit() and (stale.parent != self.config_dir or int(name) not in keep):
                stale.unlink()
        # A5: cost must stay linear in N. One interface read and one journal
        # write for all new addresses; launch every instance, then wait for
        # them together instead of paying each 3proxy's startup in series.
        existing = self.existing_addresses()
        fresh = {str(r.ipv6) for r in records} - existing - self.addresses
        if fresh:
            self.addresses |= fresh
            atomic_json(self.journal, sorted(self.addresses))
        launched, adopted = [], 0
        for record in records:
            try:
                since = self._bring_up(record, existing)
                if since is None:
                    adopted += 1
                else:
                    launched.append((record, since))
            except Exception:
                logger.exception("Could not bring up proxy on port %s; fencing it", record.port)
                failed.add(record.port)
        # Each deadline counts from that proxy's own launch: waiting in series
        # never stacks timeouts, and a slow one gets its full PROXY_READY_TIMEOUT.
        for record, since in launched:
            try:
                self._wait_ready(record.port, since)
                self.serving[record.port] = record
            except Exception:
                logger.exception("Proxy on port %s did not become ready; fencing it", record.port)
                failed.add(record.port)
        # Each divergent/missing instance got its own systemctl start/restart, in series.
        logger.info("Reconciled %d proxies: %d adopted unchanged, %d (re)started one at a time%s",
                    len(records), adopted, len(launched), f" (ports {', '.join(str(r.port) for r, _ in launched)})" if launched else "")
        in_use = {str(record.ipv6) for record in records} | self._orphan_addresses()
        for address in sorted(self.addresses - in_use):
            self._release(address)
        self.settle()
        return failed

    def _bring_up(self, record, existing):
        """Adopt this instance if it already serves its config, else (re)launch it.

        Returns None when adopted, else the launch time its readiness counts from.
        """
        self._claim(record.ipv6, existing)
        existing.add(str(record.ipv6))
        self._write_address(record)
        path = self.config_path(record.port)
        current = path.read_text() if path.exists() else None
        if current == render_config(self.settings, record) and self.ready(record.port):
            self.serving[record.port] = record  # adopted: same PID, sessions intact
            return None
        self._launch(record)
        return time.monotonic()  # after the call: a slow systemctl does not eat the readiness budget

    def recover(self, port, record=None):
        """Re-evaluate one fenced port; touches no other instance.

        With its record: adopt it if already up with its config, else bring it
        up. Without (fenced mid-create): make sure nothing serves on it.
        """
        self.serving.pop(port, None)
        if record is None:
            self.supervisor.stop(port)
            try:
                address = json.loads(address_path(self.config_dir, port).read_text())["ipv6"]
            except (OSError, ValueError, KeyError, TypeError):
                address = None
            self.config_path(port).unlink(missing_ok=True)
            address_path(self.config_dir, port).unlink(missing_ok=True)
            if address is not None:
                self._release(address)  # only if it is in our journal and unused
        else:
            since = self._bring_up(record, self.existing_addresses())
            if since is not None:
                self._wait_ready(port, since)
                self.serving[port] = record
        self.settle([port])

    def _orphan_addresses(self):
        found = set()
        for port in self.orphans:
            try:
                found.add(str(IPv6Address(json.loads(address_path(self.config_dir, port).read_text())["ipv6"])))
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("Orphan proxy %s has no readable address file", port)
        return found

    def statuses(self, records):
        """port -> running | starting | failed, from one supervisor read."""
        states = self.supervisor.list_states() if records else {}
        result = {}
        for record in records:
            state = states.get(record.port)
            if self.serving.get(record.port) == record and state == "active":
                result[record.port] = "running"
            elif state in ("activating", "reloading"):
                result[record.port] = "starting"
            else:
                result[record.port] = "failed"
        return result

    def healthy(self, records):
        active = self.supervisor.list_active() if records else set()
        return all(self.serving.get(record.port) == record and record.port in active for record in records)

    def close(self):
        # Proxies are not children of the API: closing the API leaves them serving.
        self.serving.clear()
