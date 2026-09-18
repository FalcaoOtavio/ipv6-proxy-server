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


def render_config(settings, records):
    lines = ["nserver 1.1.1.1", "nscache 65536", "maxconn 200",
             "timeouts 1 5 30 60 180 1800 15 60"]
    users = {r.username: r.password for r in records if r.username}
    for username, password in sorted(users.items()):
        lines.append(f"users {username}:CL:{password}")
    # One foreground process; each listener has its own ACL.
    for record in sorted(records, key=lambda item: item.port):
        lines += ["flush"]
        if record.username:
            lines += ["auth strong",
                      f"allow {record.username}"]
        else:
            lines += ["auth none", "allow *"]
        command = "proxy -6 -n -a" if record.protocol == "http" else "socks -6 -a"
        lines += [f"{command} -p{record.port} -i{settings.bind_ipv4} -e{record.ipv6}"]
    return "\n".join(lines) + "\n"


class LinuxBackend:
    def __init__(self, settings):
        self.settings = settings
        self.process = None
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

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None

    def apply(self, records):
        wanted = {str(record.ipv6) for record in records}
        existing = self.existing_addresses()
        foreign = wanted & (existing - self.addresses)
        if foreign:
            raise RuntimeError("Generated IPv6 address already belongs to another service")
        # Persist ownership before touching the interface, so restart can clean partial work.
        self.addresses |= wanted
        atomic_json(self.journal, sorted(self.addresses))
        self.stop()
        for address in wanted - existing:
            self._ip("-6", "addr", "add", f"{address}/128", "dev", self.settings.interface, "nodad")
        for address in (self.addresses - wanted) & existing:
            self._ip("-6", "addr", "del", f"{address}/128", "dev", self.settings.interface)
        self.addresses = wanted
        atomic_json(self.journal, sorted(wanted))
        config = self.settings.data_dir / "3proxy.cfg"
        atomic_write(config, render_config(self.settings, records))
        if not records:
            return
        # Refuse occupied ports before launching; readiness must belong to this process.
        for record in records:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((str(self.settings.bind_ipv4), record.port))
        with (self.settings.data_dir / "3proxy.log").open("ab") as log:
            self.process = subprocess.Popen([str(self.settings.binary), str(config)],
                                            stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        deadline = time.monotonic() + 5
        pending = {record.port for record in records}
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("3proxy exited during startup; inspect 3proxy.log")
            for port in list(pending):
                try:
                    with socket.create_connection((str(self.settings.bind_ipv4), port), timeout=0.1):
                        pending.remove(port)
                except OSError:
                    pass
            if not pending:
                return
            time.sleep(0.05)
        raise RuntimeError("3proxy did not open all requested ports")

    def healthy(self, records):
        return not records or (self.process is not None and self.process.poll() is None)

    def close(self):
        self.stop()
