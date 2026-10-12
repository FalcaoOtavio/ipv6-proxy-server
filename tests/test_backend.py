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
    def __init__(self, addresses=(), active=(), enabled=None):
        self.addresses = set(addresses)
        self.active = set(active)
        self.enabled = set(self.active if enabled is None else enabled)
        self.commands = []

    def systemctl(self, argv, check=True):
        self.commands.append(argv)
        verb, unit = argv[1], argv[-1]
        port = int(unit.split("@")[1].split(".")[0]) if "@" in unit and "*" not in unit else None
        if verb in ("start", "restart"):
            self.active.add(port)
        elif verb == "stop":
            self.active.discard(port)
        elif verb == "enable":
            self.enabled.add(port)
        elif verb == "disable":
            self.enabled.discard(port)
        elif verb == "is-enabled":
            return SimpleNamespace(returncode=0 if port in self.enabled else 1)
        elif verb == "is-active":
            return SimpleNamespace(returncode=0 if port in self.active else 3,
                                   stdout="active\n" if port in self.active else "inactive\n")
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
    backend._wait_ready = lambda port, since=None: None
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
    assert mutating(host.commands) == [["systemctl", "stop", "ipv6-proxy@30000.service"],
                                       ["systemctl", "disable", "--no-reload", "ipv6-proxy@30000.service"],
                                       ["ip", "-6", "addr", "del", "2001:db8::aa/128", "dev", "eth0"]]
    assert not (tmp_path / "proxies" / "30000.cfg").exists()
    assert (tmp_path / "proxies" / "30001.cfg").stat().st_mode & 0o777 == 0o600
    assert backend.healthy([b]) and host.active == {30001}

def test_provision_commands_are_per_instance(tmp_path):
    host = Host()
    backend = backend_for(tmp_path, host)
    backend.provision(record(30005, "2001:db8::5"))
    assert_targets_only(host.commands, 30005, {"2001:db8::5"})
    assert mutating(host.commands)[-2:] == [["systemctl", "enable", "--no-reload", "ipv6-proxy@30005.service"],
                                            ["systemctl", "start", "ipv6-proxy@30005.service"]]

def test_start_enables_only_when_needed_and_never_reloads_pid1(tmp_path):
    host = Host(enabled={30005})
    supervisor = SystemdSupervisor(run=host.systemctl, config_dir=tmp_path)
    supervisor.start(30005, tmp_path / "30005.cfg")
    assert mutating(host.commands) == [["systemctl", "start", "ipv6-proxy@30005.service"]]
    assert not any(c[1] == "daemon-reload" or "--now" in c for c in host.commands)
    host.commands.clear()
    supervisor.stop(30006)  # never enabled: stop only, no disable
    assert mutating(host.commands) == [["systemctl", "stop", "ipv6-proxy@30006.service"]]

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
    atomic_json(tmp_path / "pending-ports.json", [30009])  # interrupted delete of 30009
    assert backend_for(tmp_path, host).intents == {30009}
    backend = backend_for(tmp_path, host)
    backend.ready = lambda port: port in host.active
    assert backend.reconcile([healthy, divergent, missing]) == set()
    assert mutating(host.commands) == [
        ["systemctl", "stop", "ipv6-proxy@30009.service"],
        ["systemctl", "disable", "--no-reload", "ipv6-proxy@30009.service"],
        ["systemctl", "restart", "ipv6-proxy@30001.service"],
        ["ip", "-6", "addr", "add", "2001:db8::3/128", "dev", "eth0", "nodad"],
        ["systemctl", "enable", "--no-reload", "ipv6-proxy@30002.service"],
        ["systemctl", "start", "ipv6-proxy@30002.service"]]
    assert not (tmp_path / "proxies" / "30009.cfg").exists()
    assert json.loads((tmp_path / "pending-ports.json").read_text()) == []
    assert json.loads((tmp_path / "orphans.json").read_text()) == []
    assert (tmp_path / "proxies" / "notes.cfg").exists()
    assert backend.healthy([healthy, divergent, missing])

def test_stale_or_empty_state_never_stops_unknown_proxies(tmp_path):
    """M7: state.json restored old/empty at API start: instances it lacks keep serving."""
    settings = settings_for(tmp_path)
    live = [record(30000 + i, f"2001:db8::{i + 1}") for i in range(5)]
    (tmp_path / "proxies").mkdir()
    for r in live:
        (tmp_path / "proxies" / f"{r.port}.cfg").write_text(render_config(settings, r))
        atomic_json(tmp_path / "proxies" / f"{r.port}.addr", {"ipv6": str(r.ipv6), "interface": "eth0"})
    atomic_json(tmp_path / "owned-addresses.json", sorted(str(r.ipv6) for r in live))
    host = Host(addresses={str(r.ipv6) for r in live}, active={r.port for r in live})
    backend = backend_for(tmp_path, host)
    backend.ready = lambda port: port in host.active
    assert backend.reconcile([live[0]]) == set()  # state.json only knows the first
    assert mutating(host.commands) == []
    assert host.active == {r.port for r in live} and host.addresses == {str(r.ipv6) for r in live}
    assert all((tmp_path / "proxies" / f"{r.port}.cfg").exists() for r in live)
    assert backend.orphans == {r.port for r in live[1:]}
    assert json.loads((tmp_path / "orphans.json").read_text()) == [r.port for r in live[1:]]
    assert backend.reconcile([]) == set() and mutating(host.commands) == []  # empty state.json too

def test_intent_is_settled_only_after_commit(tmp_path):
    host = Host()
    backend = backend_for(tmp_path, host)
    a = record(30000, "2001:db8::a")
    backend.provision(a)
    assert json.loads((tmp_path / "pending-ports.json").read_text()) == [30000]
    backend.settle({30000})
    assert backend.intents == set()
    # Crash after start but before state.json committed: next start stops only it.
    backend.provision(record(30001, "2001:db8::b"))
    host.commands.clear()
    restarted = backend_for(tmp_path, host)
    restarted.ready = lambda port: port in host.active
    assert restarted.reconcile([a]) == set()
    assert mutating(host.commands) == [["systemctl", "stop", "ipv6-proxy@30001.service"],
                                       ["systemctl", "disable", "--no-reload", "ipv6-proxy@30001.service"],
                                       ["ip", "-6", "addr", "del", "2001:db8::b/128", "dev", "eth0"]]

def test_reconcile_failure_is_reported_per_port(tmp_path):
    host = Host()
    backend = backend_for(tmp_path, host)
    good, bad = record(30000, "2001:db8::1"), record(30001, "2001:db8::2")

    def wait(port, since=None):
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
    assert mutating(host.commands) == [["systemctl", "enable", "--no-reload", "ipv6-proxy@30001.service"],
                                       ["systemctl", "start", "ipv6-proxy@30001.service"],
                                       ["systemctl", "stop", "ipv6-proxy@30000.service"],
                                       ["systemctl", "disable", "--no-reload", "ipv6-proxy@30000.service"]]
    assert "2001:db8::a" in host.addresses and json.loads(backend.journal.read_text()) == ["2001:db8::a"]

def test_render_config_denies_private_destinations_before_allow(tmp_path):
    settings = settings_for(tmp_path)
    for extra, auth, allow in (({}, "auth iponly", "allow *"),
                               ({"username": "u", "password": "p"}, "auth strong", "allow u")):
        lines = render_config(settings, record(30000, "2001:db8::1", **extra)).splitlines()
        deny = "deny * * ::/128,::1/128,::ffff:0:0/96,fe80::/10,fc00::/7,ff00::/8,2001:db8::/64"
        assert "auth none" not in lines  # it skips ACLs: the deny would be dead
        assert lines.index(auth) < lines.index("deny * * * * UDPASSOC") < lines.index(deny) < lines.index(allow)
    open_settings = settings.model_copy(update={"block_private_destinations": False})
    assert not any(line.startswith("deny") for line in render_config(open_settings, record(30000, "2001:db8::1")).splitlines())

def test_units_start_limit_outlasts_boot_retries():
    import configparser
    from pathlib import Path
    for name in ("ipv6-proxy@.service", "ipv6-proxy-api.service"):
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.optionxform = str
        unit.read(Path(__file__).parent.parent / "deploy" / name)
        interval = int(unit["Unit"]["StartLimitIntervalSec"])
        burst = int(unit["Unit"]["StartLimitBurst"])
        restart = int(unit["Service"]["RestartSec"])
        # The limit must allow retrying for at least a minute at RestartSec pace.
        assert burst * restart >= 20 and interval >= burst * restart, name
        assert "StartLimit" not in "".join(unit["Service"]), name  # [Service] placement is ignored

def test_ready_timeout_env_default_and_limits(tmp_path, monkeypatch):
    for name, value in {"PROXY_IPV6_SUBNET": "2001:db8::/64", "PROXY_BIND_IPV4": "127.0.0.1",
                        "PROXY_INTERFACE": "eth0", "PROXY_API_TOKEN": "x" * 32}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PROXY_READY_TIMEOUT", raising=False)
    assert Settings.from_env().ready_timeout == 30
    for value in ("1", "300", "90"):
        monkeypatch.setenv("PROXY_READY_TIMEOUT", value)
        assert Settings.from_env().ready_timeout == int(value)
    for value in ("0", "301", "-5", "x"):
        monkeypatch.setenv("PROXY_READY_TIMEOUT", value)
        with pytest.raises(ValueError):
            Settings.from_env()

class Clock:
    def __init__(self):
        self.now = 1000.0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds

def slow_backend(tmp_path, monkeypatch, timeout, ready_at):
    """3proxy stays active but only accepts connections at clock time ready_at."""
    clock = Clock()
    monkeypatch.setattr("proxy_api.backend.time", clock)
    host = Host(active={30000, 30001})
    backend = LinuxBackend(settings_for(tmp_path).model_copy(update={"ready_timeout": timeout}),
                           SystemdSupervisor(run=host.systemctl, config_dir=tmp_path / "proxies"))
    backend.ready = lambda port: clock.now >= ready_at
    return backend, clock

def test_wait_ready_honours_configured_timeout(tmp_path, monkeypatch):
    # T30: a 3proxy that binds after ~21 s (slow VM) passes with the default 30 s...
    backend, clock = slow_backend(tmp_path, monkeypatch, 30, 1021.0)
    backend._wait_ready(30000)
    # ...and fails with a 10 s override, naming the env to raise.
    backend, clock = slow_backend(tmp_path, monkeypatch, 10, 1021.0)
    with pytest.raises(RuntimeError, match=r"in 10s \(PROXY_READY_TIMEOUT"):
        backend._wait_ready(30000)
    assert 1010 <= clock.now < 1011

def test_reconcile_deadline_counts_from_each_launch(tmp_path, monkeypatch):
    # Waiting in series must neither stack timeouts nor shorten a later proxy's budget.
    backend, clock = slow_backend(tmp_path, monkeypatch, 30, 1021.0)
    backend._wait_ready(30000, since=clock.now)
    backend._wait_ready(30001, since=1000.0)  # launched at t=1000: already ready, no extra wait
    backend._wait_ready(30001, since=900.0)  # deadline long past but up: probed once, accepted
    assert clock.now < 1022
    backend.ready = lambda port: False
    with pytest.raises(RuntimeError):
        backend._wait_ready(30001, since=980.0)  # its own 30 s already elapsed: no extra wait
    assert clock.now < 1022

def test_systemctl_timeout_derives_from_ready_timeout():
    from proxy_api.supervisor import command_timeout
    assert command_timeout(1) == 30 and command_timeout(30) == 45 and command_timeout(300) == 315

def test_start_timeout_with_unit_up_later_is_adopted_not_fenced(tmp_path, monkeypatch):
    """E2E #2: systemctl start outlives its timeout but the unit comes up: adopt it."""
    import subprocess
    clock = Clock()
    monkeypatch.setattr("proxy_api.backend.time", clock)
    host = Host()
    real = host.systemctl

    def slow(argv, check=True):
        result = real(argv, check)
        if argv[1] == "start":
            raise subprocess.TimeoutExpired(argv, 45)  # job queued; unit becomes active anyway
        return result

    backend = LinuxBackend(settings_for(tmp_path), SystemdSupervisor(run=slow, config_dir=tmp_path / "proxies"))
    backend._ip = host.ip
    backend._refuse_foreign_port = lambda port: None
    backend.ready = lambda port: port in host.active and clock.now >= 1005
    a = record(30002, "2001:db8::2")
    assert backend.reconcile([a]) == set()
    assert backend.healthy([a]) and backend.statuses([a]) == {30002: "running"}

def test_activating_unit_is_waited_for(tmp_path):
    supervisor = SystemdSupervisor(run=lambda argv, check=True: SimpleNamespace(returncode=3, stdout="activating\n"))
    assert supervisor.is_active(30000)

def test_recover_adopts_or_starts_only_the_fenced_port(tmp_path):
    host = Host()
    backend = backend_for(tmp_path, host)
    a, b = record(30000, "2001:db8::1"), record(30001, "2001:db8::2")
    backend.ready = lambda port: port in host.active
    backend.provision(a)
    backend.provision(b)
    host.commands.clear()
    backend.serving.pop(a.port)  # as if fenced: adopted again, nothing restarted
    backend.recover(a.port, a)
    assert mutating(host.commands) == [] and backend.healthy([a, b])
    host.active.discard(a.port)  # really down: only its own unit is started
    backend.recover(a.port, a)
    assert_targets_only(host.commands, a.port, {"2001:db8::1"})
    assert backend.healthy([a, b])
    host.commands.clear()
    backend.recover(a.port, None)  # fenced mid-create: stopped and its address released
    assert_targets_only(host.commands, a.port, {"2001:db8::1"})
    assert a.port not in host.active and b.port in host.active and "2001:db8::1" not in host.addresses
