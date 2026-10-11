import json
import os
import socket
import subprocess
import time

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

def render_config(settings, record):
    # One 3proxy process per port: only this record's user is loaded, and
    # restarting it never touches another port's listener or sessions.
    lines = ["nserver 1.1.1.1", "nscache 65536", "maxconn 200",
             "timeouts 1 5 30 60 180 1800 15 60"]
    if record.username:
        lines += [f"users {record.username}:CL:{record.password}",
                  "auth strong", f"allow {record.username}"]
    else:
        lines += ["auth none", "allow *"]
    # socks also serves UDP ASSOCIATE; the relay egresses from -e like TCP.
    command = "proxy -6 -n -a" if record.protocol == "http" else "socks -6 -a"
    lines += [f"{command} -p{record.port} -i{settings.bind_ipv4} -e{record.ipv6}"]
    return "\n".join(lines) + "\n"

class LinuxBackend:
    def __init__(self, settings):
        self.settings = settings
        self.processes = {}  # port -> (record, Popen)
        self.addresses = set()
        self.journal = settings.data_dir / "owned-addresses.json"
        if self.journal.exists():
            from ipaddress import IPv6Address
            self.addresses = {str(IPv6Address(ip)) for ip in json.loads(self.journal.read_text())}
            if any(IPv6Address(ip) not in settings.subnet for ip in self.addresses):
                raise RuntimeError("Owned address journal does not match configured subnet")

    def _ip(self, *args):
        return subprocess.run(["ip", *args], check=True, capture_output=True, text=True, timeout=15)

    def existing_addresses(self):
        data = json.loads(self._ip("-j", "-6", "addr", "show", "dev", self.settings.interface).stdout)
        return {entry["local"] for interface in data for entry in interface.get("addr_info", [])}

    def _config(self, port):
        return self.settings.data_dir / f"3proxy-{port}.cfg"

    def _stop(self, port):
        _, process = self.processes.pop(port)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def stop(self):
        for port in list(self.processes):
            self._stop(port)

    def apply(self, records):
        """Converge to records touching only ports whose record changed."""
        desired = {record.port: record for record in records}
        wanted = {str(record.ipv6) for record in records}
        existing = self.existing_addresses()
        foreign = wanted & (existing - self.addresses)
        if foreign:
            raise RuntimeError("Generated IPv6 address already belongs to another service")
        # Persist ownership before touching the interface, so restart can clean partial work.
        self.addresses |= wanted
        atomic_json(self.journal, sorted(self.addresses))
        for port, (record, process) in list(self.processes.items()):
            if desired.get(port) != record or process.poll() is not None:
                self._stop(port)
        for address in wanted - existing:
            self._ip("-6", "addr", "add", f"{address}/128", "dev", self.settings.interface, "nodad")
        for address in (self.addresses - wanted) & existing:
            self._ip("-6", "addr", "del", f"{address}/128", "dev", self.settings.interface)
        self.addresses = wanted
        atomic_json(self.journal, sorted(wanted))
        for stale in self.settings.data_dir.glob("3proxy-*.cfg"):
            if stale.stem.removeprefix("3proxy-") not in {str(port) for port in desired}:
                stale.unlink()
        pending = [record for record in records if record.port not in self.processes]
        for record in pending:
            # Refuse occupied ports before launching; readiness must belong to this process.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((str(self.settings.bind_ipv4), record.port))
        for record in pending:
            config = self._config(record.port)
            atomic_write(config, render_config(self.settings, record))
            with (self.settings.data_dir / "3proxy.log").open("ab") as log:
                process = subprocess.Popen([str(self.settings.binary), str(config)],
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            self.processes[record.port] = (record, process)
        deadline = time.monotonic() + 5
        waiting = {record.port for record in pending}
        while waiting and time.monotonic() < deadline:
            for port in list(waiting):
                if self.processes[port][1].poll() is not None:
                    raise RuntimeError("3proxy exited during startup; inspect 3proxy.log")
                try:
                    with socket.create_connection((str(self.settings.bind_ipv4), port), timeout=0.1):
                        waiting.remove(port)
                except OSError:
                    pass
            if waiting:
                time.sleep(0.05)
        if waiting:
            raise RuntimeError("3proxy did not open all requested ports")

    def healthy(self, records):
        return all(record.port in self.processes and self.processes[record.port][0] == record
                   and self.processes[record.port][1].poll() is None for record in records)

    def close(self):
        self.stop()
