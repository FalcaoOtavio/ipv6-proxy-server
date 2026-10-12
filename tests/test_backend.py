import json
from ipaddress import IPv6Address
from types import SimpleNamespace

import pytest

from proxy_api.backend import LinuxBackend, atomic_json, render_config
from proxy_api.config import Settings
from proxy_api.models import ProxyRecord
from proxy_api.supervisor import SystemdSupervisor

def settings_for(tmp_path):
    return Settings(subnet="2001:db8::/64", bind_ipv4="127.0.0.1",
                    interface="eth0", api_token="x" * 32, data_dir=tmp_path)

class Host:
    """Mocked systemctl + ip. Records every command the API would run."""
    def __init__(self, addresses=(), active=()):
        self.addresses = set(addresses)
        self.active = set(active)
        self.commands = []

    def systemctl(self, argv, check=True):
        self.commands.append(argv)
        verb, unit = argv[1], argv[-1]
        port = int(unit.split("@")[1].split(".")[0]) if "@" in unit and "*" not in unit else None
        if verb == "enable" or verb == "restart":
            self.active.add(port)
        elif verb == "disable":
            self.active.discard(port)
        elif verb == "is-active":
            return SimpleNamespace(returncode=0 if port in self.active else 3)
        elif verb == "show":
            return SimpleNamespace(stdout=f"{1000 + port}\n")
        elif verb == "list-units":
            return SimpleNamespace(stdout="".join(f"ipv6-proxy@{p}.service loaded active running x\n"
                                                  for p in sorted(self.active)))
        return SimpleNamespace(returncode=0, stdout="")

    def ip(self, *args):
        if args[0] == "-j":
            return SimpleNamespace(stdout=json.dumps([{"addr_info": [{"local": a} for a in self.addresses]}]))
        self.commands.append(["ip", *args])
        (self.addresses.add if args[2] == "add" else self.addresses.discard)(args[3].split("/")[0])

def backend_for(tmp_path, host):
    supervisor = SystemdSupervisor(run=host.systemctl, config_dir=tmp_path / "proxies")
    backend = LinuxBackend(settings_for(tmp_path), supervisor)
    backend._ip = host.ip
    backend._refuse_foreign_port = lambda port: None
    backend._wait_ready = lambda port: None
    return backend

def record(port, ip, **extra):
    return ProxyRecord(port=port, ipv6=IPv6Address(ip), **extra)

def mutating(commands):
    return [c for c in commands if c[0] == "ip" or c[1] in ("enable", "disable", "start", "stop", "restart")]

def assert_targets_only(commands, port, addresses):
    for command in mutating(commands):
        assert "*" not in " ".join(command), command
        if command[0] == "systemctl":
            assert command[-1] == f"ipv6-proxy@{port}.service", command
        else:
            assert command[4].split("/")[0] in addresses, command

def test_recovery_removes_only_owned_addresses(tmp_path):
    atomic_json(tmp_path / "owned-addresses.json", ["2001:db8::2", "2001:db8::3"])
    host = Host(addresses={"2001:db8::1", "2001:db8::2"})
    backend = backend_for(tmp_path, host)
    assert backend.reconcile([]) == set()
    assert mutating(host.commands) == [["ip", "-6", "addr", "del", "2001:db8::2/128", "dev", "eth0"]]
    assert json.loads(backend.journal.read_text()) == []

def test_failed_cleanup_keeps_ownership_for_restart(tmp_path):
    atomic_json(tmp_path / "owned-addresses.json", ["2001:db8::2"])
    backend = backend_for(tmp_path, Host())
    backend.existing_addresses = lambda: {"2001:db8::2"}

    def fail(*args):
        raise RuntimeError("ip failed")

    backend._ip = fail
    with pytest.raises(RuntimeError, match="ip failed"):
        backend.reconcile([])
    assert json.loads(backend.journal.read_text()) == ["2001:db8::2"]

def test_every_mutation_names_only_its_target_instance(tmp_path):
    host = Host()
    backend = backend_for(tmp_path, host)
    a, b = record(30000, "2001:db8::a"), record(30001, "2001:db8::b")
    backend.provision(a)
    backend.provision(b)
    host.commands.clear()
    rotated = a.model_copy(update={"ipv6": IPv6Address("2001:db8::aa")})
    backend.replace(a, rotated)
    # Safe order: new address, restart of the target only, then old address removed.
    assert mutating(host.commands) == [
        ["ip", "-6", "addr", "add", "2001:db8::aa/128", "dev", "eth0", "nodad"],
        ["systemctl", "restart", "ipv6-proxy@30000.service"],
        ["ip", "-6", "addr", "del", "2001:db8::a/128", "dev", "eth0"]]
    host.commands.clear()
    backend.deprovision(rotated)
    # The address goes only after the instance is stopped.
    assert mutating(host.commands) == [["systemctl", "disable", "--now", "ipv6-proxy@30000.service"],
                                       ["ip", "-6", "addr", "del", "2001:db8::aa/128", "dev", "eth0"]]
    assert not (tmp_path / "proxies" / "30000.cfg").exists()
    assert (tmp_path / "proxies" / "30001.cfg").stat().st_mode & 0o777 == 0o600
    assert backend.healthy([b]) and host.active == {30001}

def test_provision_commands_are_per_instance(tmp_path):
    host = Host()
    backend = backend_for(tmp_path, host)
    backend.provision(record(30005, "2001:db8::5"))
    assert_targets_only(host.commands, 30005, {"2001:db8::5"})
    assert ["systemctl", "enable", "--now", "ipv6-proxy@30005.service"] in host.commands

def test_foreign_address_is_refused_untouched(tmp_path):
    host = Host(addresses={"2001:db8::9"})
    backend = backend_for(tmp_path, host)
    with pytest.raises(RuntimeError, match="another service"):
        backend.provision(record(30000, "2001:db8::9"))
    assert mutating(host.commands) == []

def test_reconcile_adopts_healthy_and_touches_only_divergent(tmp_path):
    settings = settings_for(tmp_path)
    healthy, divergent, missing = (record(30000, "2001:db8::1"), record(30001, "2001:db8::2"),
                                   record(30002, "2001:db8::3"))
    (tmp_path / "proxies").mkdir()
    (tmp_path / "proxies" / "30000.cfg").write_text(render_config(settings, healthy))
    (tmp_path / "proxies" / "30001.cfg").write_text("stale\n")
    (tmp_path / "proxies" / "30009.cfg").write_text("orphan\n")
    (tmp_path / "proxies" / "notes.cfg").write_text("not ours\n")
    atomic_json(tmp_path / "owned-addresses.json", ["2001:db8::1", "2001:db8::2"])
    host = Host(addresses={"2001:db8::1", "2001:db8::2"}, active={30000, 30001, 30009})
    backend = backend_for(tmp_path, host)
    backend.ready = lambda port: port in host.active
    assert backend.reconcile([healthy, divergent, missing]) == set()
    assert mutating(host.commands) == [
        ["systemctl", "disable", "--now", "ipv6-proxy@30009.service"],
        ["systemctl", "restart", "ipv6-proxy@30001.service"],
        ["ip", "-6", "addr", "add", "2001:db8::3/128", "dev", "eth0", "nodad"],
        ["systemctl", "enable", "--now", "ipv6-proxy@30002.service"]]
    assert not (tmp_path / "proxies" / "30009.cfg").exists()
    assert (tmp_path / "proxies" / "notes.cfg").exists()
    assert backend.healthy([healthy, divergent, missing])

def test_reconcile_failure_is_reported_per_port(tmp_path):
    host = Host()
    backend = backend_for(tmp_path, host)
    good, bad = record(30000, "2001:db8::1"), record(30001, "2001:db8::2")

    def wait(port):
        if port == bad.port:
            raise RuntimeError("not ready")

    backend._wait_ready = wait
    assert backend.reconcile([good, bad]) == {bad.port}
    assert backend.healthy([good]) and not backend.healthy([bad])

def test_systemd_config_path_is_fixed(tmp_path):
    supervisor = SystemdSupervisor(run=Host().systemctl, config_dir=tmp_path)
    with pytest.raises(RuntimeError, match="reads"):
        supervisor.start(30000, tmp_path / "elsewhere.cfg")
    with pytest.raises(ValueError):
        supervisor.restart("*")

def test_systemd_states_and_per_proxy_status(tmp_path):
    listing = ("ipv6-proxy@30000.service loaded active running IPv6 proxy instance on port 30000\n"
               "ipv6-proxy@30001.service loaded activating auto-restart IPv6 proxy instance on port 30001\n"
               "ipv6-proxy@30002.service loaded failed failed IPv6 proxy instance on port 30002\n")
    supervisor = SystemdSupervisor(run=lambda argv, check=True: SimpleNamespace(stdout=listing))
    assert supervisor.list_states() == {30000: "active", 30001: "activating", 30002: "failed"}
    backend = LinuxBackend(settings_for(tmp_path), supervisor)
    records = [record(30000 + i, f"2001:db8::{i + 1}") for i in range(4)]
    backend.serving = {r.port: r for r in records}
    assert backend.statuses(records) == {30000: "running", 30001: "starting", 30002: "failed", 30003: "failed"}

def test_instance_address_file_follows_the_proxy(tmp_path):
    host = Host()
    backend = backend_for(tmp_path, host)
    a = record(30000, "2001:db8::a")
    backend.provision(a)
    spec = json.loads((tmp_path / "proxies" / "30000.addr").read_text())
    assert spec == {"ipv6": "2001:db8::a", "interface": "eth0"}
    backend.replace(a, a.model_copy(update={"ipv6": IPv6Address("2001:db8::b")}))
    assert json.loads((tmp_path / "proxies" / "30000.addr").read_text())["ipv6"] == "2001:db8::b"
    backend.deprovision(a)
    assert not (tmp_path / "proxies" / "30000.addr").exists()

def test_port_move_keeps_the_shared_address(tmp_path):
    host = Host()
    backend = backend_for(tmp_path, host)
    a = record(30000, "2001:db8::a")
    backend.provision(a)
    host.commands.clear()
    backend.replace(a, a.model_copy(update={"port": 30001}))
    assert mutating(host.commands) == [["systemctl", "enable", "--now", "ipv6-proxy@30001.service"],
                                       ["systemctl", "disable", "--now", "ipv6-proxy@30000.service"]]
    assert "2001:db8::a" in host.addresses and json.loads(backend.journal.read_text()) == ["2001:db8::a"]
