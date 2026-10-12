"""Criteria A (independence) and B (failures/concurrency) against real 3proxy.

Scope: egress IPv6 is v4-mapped loopback - loopback/ULA complementar, NAO IPv6 roteado.
Every test writes JSON evidence (no passwords) to tests/acceptance/evidence/.
"""
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv6Address

import pytest
import socket

from proxy_api.backend import render_config

from .conftest import StreamProbe, cli_env_file, open_tunnel, run_cli, write_evidence, ADMIN

STREAM_MIN_SECONDS = 10
MAX_GAP = 1.0

def same_ip(a, b):
    return IPv6Address(a) == IPv6Address(b)

class Survivors:
    """B (HTTP CONNECT) and C (SOCKS5) carrying live streams; snapshot and verify them."""

    def __init__(self, h):
        self.h = h
        self.b = h.create(h.ports[0], "http", "bob", "bob-pw-1")
        self.c = h.create(h.ports[1], "socks5", "carol", "carol-pw-1")
        self.secret = {self.b["port"]: "bob-pw-1", self.c["port"]: "carol-pw-1"}
        self.before = self.snapshot()
        self.probes = []
        for name, proxy in (("B-http-connect", self.b), ("C-socks5", self.c)):
            sock, egress = open_tunnel(proxy, self.secret[proxy["port"]], h.target.port)
            assert same_ip(egress, proxy["ipv6"]), (egress, proxy["ipv6"])
            probe = StreamProbe(name, sock, egress)
            probe.start()
            self.probes.append(probe)
        self.mark = h.supervisor.mark()
        time.sleep(0.3)

    def snapshot(self):
        return {p["port"]: {"pid": self.h.pid(p["port"]), "config": self.h.config(p["port"]).decode()}
                for p in (self.b, self.c)}

    def verify(self, min_seconds, allowed_ports):
        """Stop the streams and assert B/C were untouched; returns evidence."""
        for probe in self.probes:
            remaining = min_seconds - (time.time() - probe.started_at)
            if remaining > 0:
                time.sleep(remaining)
        assert all(probe.alive() for probe in self.probes), [p.error for p in self.probes]
        after = self.snapshot()
        audit = self.h.supervisor.mutating_since(self.mark)
        fresh = {}
        for proxy in (self.b, self.c):
            sock, egress = open_tunnel(proxy, self.secret[proxy["port"]], self.h.target.port)
            sock.close()
            fresh[proxy["port"]] = egress
        for probe in self.probes:
            probe.stop()
        stats = [probe.stats() for probe in self.probes]
        evidence = {"pids_before": {p: v["pid"] for p, v in self.before.items()},
                    "pids_after": {p: v["pid"] for p, v in after.items()},
                    "config_identical": {p: self.before[p]["config"] == after[p]["config"] for p in after},
                    "egress_initial": {p["port"]: p["ipv6"] for p in (self.b, self.c)},
                    "egress_after": fresh, "streams": stats, "supervisor_mutations": audit,
                    "allowed_ports": sorted(allowed_ports)}
        for stat in stats:
            assert stat["error"] is None, stat
            assert stat["duration_s"] >= min_seconds - 0.5, stat
            assert stat["max_gap_s"] <= MAX_GAP, stat
            assert stat["contiguous"], stat
        for port in after:
            assert after[port]["pid"] == self.before[port]["pid"] is not None
            assert after[port]["config"] == self.before[port]["config"]
        for proxy in (self.b, self.c):
            assert same_ip(fresh[proxy["port"]], proxy["ipv6"])
        survivors = {self.b["port"], self.c["port"]}
        touched = {call["port"] for call in audit}
        assert not touched & survivors, audit
        assert touched <= set(allowed_ports), audit
        return evidence

def test_a_independence_during_create_edit_rotate_delete(harness):
    h = harness
    a = h.create(h.ports[2], "http", "alice", "alice-pw-1")
    s = Survivors(h)
    log = []

    def step(name, response, expected):
        log.append({"t": time.time(), "op": name, "status": response.status_code})
        assert response.status_code == expected, (name, response.text)
        return response.json() if response.content else None

    d = step("create D", h.call("POST", "/proxies", json={"port": h.ports[3], "protocol": "socks5"}), 201)
    time.sleep(1)
    h.passwords.add("alice-pw-2")
    edited = step("edit A creds+port", h.call("PATCH", f"/proxies/{a['port']}", headers={"If-Match": a["id"]},
                                              json={"port": h.ports[4], "username": "alice2",
                                                    "password": "alice-pw-2"}), 200)
    assert edited["id"] == a["id"] and edited["port"] == h.ports[4] and edited["username"] == "alice2"
    sock, egress = open_tunnel(edited, "alice-pw-2", h.target.port)
    sock.close()
    assert same_ip(egress, a["ipv6"])
    time.sleep(1)
    rotated = step("rotate A", h.call("POST", f"/proxies/{h.ports[4]}/rotate"), 200)
    sock, egress = open_tunnel(rotated, "alice-pw-2", h.target.port)
    sock.close()
    assert same_ip(egress, rotated["ipv6"]) and not same_ip(rotated["ipv6"], a["ipv6"])
    time.sleep(1)
    step("delete A", h.call("DELETE", f"/proxies/{h.ports[4]}", headers={"If-Match": a["id"]}), 204)
    step("delete D", h.call("DELETE", f"/proxies/{d['port']}"), 204)
    evidence = s.verify(STREAM_MIN_SECONDS, {h.ports[2], h.ports[3], h.ports[4]})
    write_evidence("criterion_a_independence", {"operations": log, **evidence}, h.passwords)

# ---- B: injected failures --------------------------------------------------------------

def occupy(port):
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", port))
    squatter.listen()
    return squatter

def assert_consistent(h, expect_ports):
    listed = h.call("GET", "/proxies")
    assert listed.status_code == 200, listed.text
    api = sorted((p["port"], p["id"], str(IPv6Address(p["ipv6"]))) for p in listed.json())
    disk = sorted((p["port"], p["id"], str(IPv6Address(p["ipv6"]))) for p in h.state()["proxies"])
    assert api == disk
    assert {port for port, _, _ in api} == set(expect_ports)
    return api

def fault_bind_single(h, a):
    squatter = occupy(h.ports[10])
    try:
        r = h.call("PATCH", f"/proxies/{a['port']}", json={"port": h.ports[10]})
        assert r.status_code == 503, r.text
        assert squatter.fileno() != -1  # third party listener untouched
    finally:
        squatter.close()
    return {"status": r.status_code, "expect_ports": [a["port"]]}

def fault_bind_batch(h, a):
    squatter = occupy(h.ports[12])
    try:
        r = h.call("POST", "/proxies/batch", json={"count": 4, "start_port": h.ports[10]})
        assert r.status_code == 503, r.text
    finally:
        squatter.close()
    for port in range(h.ports[10], h.ports[14]):
        assert not h.supervisor.is_active(port)
    return {"status": r.status_code, "expect_ports": [a["port"]]}

def fault_start_single(h, a):
    h.fail_starts(h.ports[10])
    r = h.call("POST", "/proxies", json={"port": h.ports[10], "protocol": "socks5"})
    assert r.status_code == 503, r.text
    assert not h.supervisor.is_active(h.ports[10])
    return {"status": r.status_code, "expect_ports": [a["port"]]}

def fault_start_batch(h, a):
    h.fail_starts(h.ports[12])
    r = h.call("POST", "/proxies/batch", json={"count": 4, "start_port": h.ports[10], "partial": True})
    assert r.status_code == 207, r.text
    assert [f["port"] for f in r.json()["failed"]] == [h.ports[12]]
    created = [p["port"] for p in r.json()["created"]]
    assert created == [h.ports[10], h.ports[11], h.ports[13]]
    return {"status": r.status_code, "expect_ports": [a["port"], *created]}

def fault_persist_single(h, a, monkeypatch):
    fail_writes(h, monkeypatch, 1)
    r = h.call("POST", f"/proxies/{a['port']}/rotate")
    assert r.status_code == 503, r.text
    assert not h.manager.broken
    return {"status": r.status_code, "expect_ports": [a["port"]]}

def fault_persist_batch(h, a, monkeypatch):
    fail_writes(h, monkeypatch, 1)
    r = h.call("POST", "/proxies/batch", json={"count": 3, "start_port": h.ports[10], "protocol": "socks5"})
    assert r.status_code == 503, r.text
    for port in range(h.ports[10], h.ports[13]):
        assert not h.supervisor.is_active(port)
    return {"status": r.status_code, "expect_ports": [a["port"]]}

def fault_rollback_single(h, a):
    # Forward start fails AND the rollback start fails: only A is fenced.
    h.fail_starts(a["port"], times=2)
    r = h.call("PATCH", f"/proxies/{a['port']}", json={"protocol": "socks5"})
    assert r.status_code == 503, r.text
    status = h.call("GET", f"/proxies/{a['port']}").json()["status"]
    assert status == "fenced"
    assert not h.manager.broken
    return {"status": r.status_code, "a_status": status, "expect_ports": None}

def fault_rollback_batch(h, a, monkeypatch):
    # Batch write fails and the state rollback write fails too: writes blocked, processes untouched.
    fail_writes(h, monkeypatch, 2)
    r = h.call("POST", "/proxies/batch", json={"count": 3, "start_port": h.ports[10]})
    assert r.status_code == 503, r.text
    assert h.manager.broken
    for port in range(h.ports[10], h.ports[13]):
        assert not h.supervisor.is_active(port)
    blocked = h.call("POST", "/proxies", json={"port": h.ports[20]})
    assert blocked.status_code == 503
    return {"status": r.status_code, "writes_blocked": True, "expect_ports": None}

def fail_writes(h, monkeypatch, times):
    real = h.manager._write
    left = {"n": times}

    def flaky(records, keys):
        if left["n"] > 0:
            left["n"] -= 1
            raise OSError(28, "No space left on device (injected)")
        return real(records, keys)
    monkeypatch.setattr(h.manager, "_write", flaky)

FAULTS = {"bind-single": fault_bind_single, "bind-batch": fault_bind_batch,
          "start-single": fault_start_single, "start-batch": fault_start_batch,
          "persist-single": fault_persist_single, "persist-batch": fault_persist_batch,
          "rollback-single": fault_rollback_single, "rollback-batch": fault_rollback_batch}

@pytest.mark.parametrize("fault", FAULTS)
def test_b_injected_failure_keeps_b_and_c(harness, fault, monkeypatch):
    h = harness
    a = h.create(h.ports[2], "http", "alice", "alice-pw-1")
    a_pid = h.pid(a["port"])
    s = Survivors(h)
    handler = FAULTS[fault]
    args = (h, a, monkeypatch) if "monkeypatch" in handler.__code__.co_varnames else (h, a)
    result = handler(*args)
    monkeypatch.undo()
    allowed = {a["port"], *h.ports[10:21]}
    evidence = s.verify(3, allowed)
    if result["expect_ports"] is not None:
        state = assert_consistent(h, {s.b["port"], s.c["port"], *result["expect_ports"]})
        if fault in ("bind-batch", "start-single", "persist-batch"):
            assert h.pid(a["port"]) == a_pid  # A itself was not the target
        evidence["state_after"] = state
    evidence["a_pid_before"], evidence["a_pid_after"] = a_pid, h.pid(a["port"])
    write_evidence(f"criterion_b_fault_{fault}", {"fault": fault, "result": result, **evidence}, h.passwords)

# ---- B: concurrency -------------------------------------------------------------------

def test_b_concurrent_api_and_cli(harness, tmp_path):
    h = harness
    s = Survivors(h)
    env = cli_env_file(tmp_path, ADMIN)
    pool = h.ports[4:40]
    rng = random.Random(4)
    outcomes = Counter()
    errors = []
    lock = threading.Lock()

    def record(kind, code):
        with lock:
            outcomes[f"{kind}:{code}"] += 1
            if code >= 500 or code in (401, 422):
                errors.append((kind, code))

    def api_worker(seed):
        local = random.Random(seed)
        for _ in range(6):
            op = local.choice(["create", "batch", "edit", "delete"])
            port = local.choice(pool)
            if op == "create":
                r = h.call("POST", "/proxies", json={"port": port, "protocol": local.choice(["http", "socks5"])})
            elif op == "batch":
                r = h.call("POST", "/proxies/batch", json={"count": 2, "start_port": port, "partial": True})
            elif op == "edit":
                r = h.call("PATCH", f"/proxies/{port}", json={"port": local.choice(pool)})
            else:
                r = h.call("DELETE", f"/proxies/{port}")
            record(f"api-{op}", r.status_code)

    def cli_worker(seed):
        local = random.Random(seed)
        for _ in range(3):
            op = local.choice(["create", "delete", "rotate"])
            port = str(local.choice(pool))
            argv = {"create": ["create", "--port", port, "--yes"], "delete": ["delete", port, "--yes"],
                    "rotate": ["rotate", port, "--yes"]}[op]
            done = run_cli(h.base_url, env, *argv, "--output", "json")
            # exit 4 = 4xx (404/409 races are expected); 3/5/6 are not.
            record(f"cli-{op}", {0: 200, 4: 409}.get(done.returncode, 500 + done.returncode))
            if done.returncode not in (0, 4):
                with lock:
                    errors.append((op, done.returncode, done.stderr[-300:]))

    started = time.time()
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(api_worker, rng.random()) for _ in range(8)]
        futures += [executor.submit(cli_worker, rng.random()) for _ in range(4)]
        for future in futures:
            future.result()
    listed = run_cli(h.base_url, env, "list", "--output", "json")
    assert listed.returncode == 0, listed.stderr
    api = assert_consistent(h, {p["port"] for p in h.state()["proxies"]})
    ports = [p for p, _, _ in api]
    ipv6s = [ip for _, _, ip in api]
    ids = [i for _, i, _ in api]
    assert len(set(ports)) == len(ports) and len(set(ipv6s)) == len(ipv6s) and len(set(ids)) == len(ids)
    for record_ in h.state()["proxies"]:
        port = record_["port"]
        assert h.pid(port) is not None, port
        model = next(r for r in h.manager.records if r.port == port)
        assert h.config(port).decode() == render_config(h.settings, model)
    managed = h.supervisor.list_managed()
    assert managed == set(ports), (managed, ports)
    evidence = s.verify(max(3, time.time() - started), set(pool))
    write_evidence("criterion_b_concurrency", {"outcomes": dict(outcomes), "errors": errors,
                                               "final_ports": ports, **evidence}, h.passwords)
    assert not errors, errors

# ---- B: identity after port reuse --------------------------------------------------------

def test_b_delete_with_stale_if_match_after_port_reuse_is_412(harness):
    h = harness
    port = h.ports[5]
    old = h.create(port, "http", "first", "first-pw-1")
    assert h.call("DELETE", f"/proxies/{port}", headers={"If-Match": old["id"]}).status_code == 204
    new = h.create(port, "socks5", "second", "second-pw-1")
    assert new["id"] != old["id"]
    new_pid = h.pid(port)
    new_config = h.config(port)
    stale = h.call("DELETE", f"/proxies/{port}", headers={"If-Match": f'"{old["id"]}"'})
    assert stale.status_code == 412, stale.text
    rotate = h.call("POST", f"/proxies/{port}/rotate", headers={"If-Match": old["id"]})
    assert rotate.status_code == 412
    current = h.call("GET", f"/proxies/{port}").json()
    assert current["id"] == new["id"] and current["status"] == "running"
    assert h.pid(port) == new_pid and h.config(port) == new_config
    sock, egress = open_tunnel(new, "second-pw-1", h.target.port)
    sock.close()
    assert same_ip(egress, new["ipv6"])
    write_evidence("criterion_b_identity", {"port": port, "old_id": old["id"], "new_id": new["id"],
                                            "stale_delete_status": stale.status_code,
                                            "stale_rotate_status": rotate.status_code,
                                            "pid_before": new_pid, "pid_after": h.pid(port)}, h.passwords)

def test_control_probe_detects_a_dropped_stream(harness):
    """Without this, a probe that never fails would make every pass above meaningless."""
    h = harness
    s = Survivors(h)
    h.supervisor.stop(s.b["port"])
    s.probes[0].join(timeout=6)
    assert s.probes[0].error is not None
    assert s.probes[1].alive()  # C keeps streaming while B is gone
    for probe in s.probes:
        probe.stop()
