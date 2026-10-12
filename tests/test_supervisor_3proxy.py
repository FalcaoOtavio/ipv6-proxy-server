"""Real 3proxy under LocalProcessSupervisor: each proxy lives and dies alone,
and a new API object adopts running proxies without restarting them."""
import os
import signal
import socket
import time

import pytest

from proxy_api.manager import Manager
from proxy_api.models import ProxyCreate
from test_isolation_3proxy import BINARY, ADMIN, LoopbackBackend, free_ports, stop_all

pytestmark = pytest.mark.skipif(not BINARY, reason="Set TEST_3PROXY_BINARY for real integration")

@pytest.fixture
def setup(tmp_path):
    from proxy_api.config import Settings
    ports = free_ports(4)
    # loopback-only test harness: targets/egress are loopback, so the SSRF deny is off
    settings = Settings(subnet="::ffff:127.0.0.0/120", bind_ipv4="127.0.0.1", interface="lo",
                        block_private_destinations=False,
                        api_token=ADMIN, data_dir=tmp_path, binary=BINARY,
                        port_start=min(ports), port_end=max(ports), supervisor="local")
    manager = Manager(settings, LoopbackBackend(settings))
    try:
        yield settings, manager, sorted(ports)
    finally:
        manager.close()
        stop_all(settings)

def create(manager, port):
    manager.mutate("", None, None, lambda: manager.plan_create(ProxyCreate(port=port)))

def pids(manager, ports):
    return {port: manager.backend.supervisor.main_pid(port) for port in ports}

def serves(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False

def test_each_proxy_is_independent_and_adopted_on_api_restart(setup):
    settings, manager, ports = setup
    a, b, c = ports[:3]
    for port in (a, b, c):
        create(manager, port)
    before = pids(manager, (a, b, c))
    assert None not in before.values() and len(set(before.values())) == 3
    # Proxies are not in the API's process group/session.
    assert all(os.getsid(pid) == pid for pid in before.values())
    manager.mutate("", None, None, lambda: manager.plan_rotate(a))  # restart of A only
    assert pids(manager, (b, c)) == {b: before[b], c: before[c]}
    assert pids(manager, (a,))[a] not in (None, before[a])
    os.kill(pids(manager, (a,))[a], signal.SIGKILL)
    time.sleep(0.2)
    assert not serves(a) and serves(b) and serves(c)
    assert pids(manager, (b, c)) == {b: before[b], c: before[c]}
    assert not manager.backend.healthy(manager.records)
    # API restart: B/C adopted with the same PIDs, only the dead A is started again.
    manager.close()
    manager = Manager(settings, LoopbackBackend(settings))
    try:
        assert pids(manager, (b, c)) == {b: before[b], c: before[c]}
        assert serves(a) and manager.backend.healthy(manager.records)
        manager.mutate("", None, None, lambda: manager.plan_delete(a))  # stop of A only
        assert not serves(a) and pids(manager, (b, c)) == {b: before[b], c: before[c]}
    finally:
        manager.close()

def test_fenced_port_recovers_alone_without_api_restart(setup):
    settings, manager, ports = setup
    a, b, c = ports[:3]
    for port in (a, b, c):
        create(manager, port)
    before = pids(manager, (a, b, c))
    # Fence A as a failed operation+compensation would: its process gone, port fenced.
    os.kill(before[a], signal.SIGKILL)
    time.sleep(0.2)
    manager.broken_ports.add(a)
    record = next(r for r in manager.records if r.port == a)
    status, body = manager.recover(a, record.id)
    assert status == 200 and body["status"] == "running" and a not in manager.broken_ports
    assert serves(a) and pids(manager, (a,))[a] not in (None, before[a])
    assert pids(manager, (b, c)) == {b: before[b], c: before[c]}
    # Fenced while still up with its committed config (the E2E #2 case): adopted, same PID.
    up = pids(manager, (b,))[b]
    manager.broken_ports.add(b)
    manager.backend.serving.pop(b)
    assert manager.recover(b)[1]["status"] == "running"
    assert pids(manager, (b, c)) == {b: up, c: before[c]} and manager.backend.healthy(manager.records)

def test_readiness_rejects_a_port_owned_by_another_process(setup):
    settings, manager, ports = setup
    squatter = socket.socket()
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", ports[0]))
    squatter.listen()
    try:
        backend = manager.backend
        # Even with a live instance pid, a listener it does not own is not "ready".
        create(manager, ports[1])
        backend.supervisor.main_pid = lambda port, real=backend.supervisor.main_pid: real(ports[1])
        assert not backend.ready(ports[0]) and backend.ready(ports[1])
        del backend.supervisor.main_pid
        with pytest.raises(Exception) as error:
            create(manager, ports[0])
        assert getattr(error.value, "status_code", None) == 503
        assert squatter.fileno() != -1 and serves(ports[0])  # the third party is untouched
        assert ports[0] not in {r.port for r in manager.records}
    finally:
        squatter.close()

def test_edit_batch_and_status_against_real_3proxy(tmp_path):
    from fastapi.testclient import TestClient
    from proxy_api.app import create_app
    from proxy_api.config import Settings
    from test_isolation_3proxy import EchoServer, tunnel, Heartbeat
    ports = sorted(free_ports(6))
    # loopback-only test harness: targets/egress are loopback, so the SSRF deny is off
    settings = Settings(subnet="::ffff:127.0.0.0/120", bind_ipv4="127.0.0.1", interface="lo", api_token=ADMIN,
                        block_private_destinations=False,
                        data_dir=tmp_path, binary=BINARY, port_start=ports[0], port_end=ports[-1], supervisor="local")
    echo = EchoServer()
    try:
        with TestClient(create_app(settings, LoopbackBackend)) as client:
            client.headers["Authorization"] = "Bearer " + ADMIN
            b = client.post("/proxies", json={"port": ports[0], "protocol": "socks5",
                                              "username": "bob", "password": "bob-pw"}).json()
            stream, _ = tunnel({**b, "password": "bob-pw"}, echo)
            beat = Heartbeat(stream)
            beat.start()
            supervisor = client.app.state.manager.backend.supervisor
            b_pid = supervisor.main_pid(b["port"])
            try:
                made = client.post("/proxies/batch", json={"count": 2, "start_port": ports[1],
                                                           "credentials": "random", "username": "r"})
                assert made.status_code == 201, made.text
                created = made.json()["created"]
                for item in created:
                    sock, egress = tunnel(item, echo)
                    sock.close()
                    assert egress == item["ipv6"]
                a = created[0]
                # Port move: new instance up, old one gone, same id/ipv6; B untouched.
                moved = client.patch(f"/proxies/{a['port']}", json={"port": ports[4], "protocol": "http"})
                assert moved.status_code == 200, moved.text
                assert (moved.json()["id"], moved.json()["ipv6"]) == (a["id"], a["ipv6"])
                assert not serves(a["port"])
                sock, egress = tunnel({**moved.json(), "password": a["password"]}, echo)
                sock.close()
                assert egress == a["ipv6"]
                assert client.get(f"/proxies/{ports[4]}").json()["status"] == "running"
                os.kill(supervisor.main_pid(ports[4]), signal.SIGKILL)
                time.sleep(0.2)
                assert client.get(f"/proxies/{ports[4]}").json()["status"] == "failed"
                assert client.get(f"/proxies/{b['port']}").json()["status"] == "running"
                assert beat.alive_for(0.3) and supervisor.main_pid(b["port"]) == b_pid
            finally:
                beat.halt.set()
                stream.close()
    finally:
        echo.close()
        stop_all(settings)

def bindable_ports(count, start=20000):
    """Below the ephemeral range: free_ports() ports may be taken by any client during a long run."""
    found = []
    for port in range(start, 32768):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        found.append(port)
        if len(found) == count:
            return found
    raise RuntimeError("not enough free ports below 32768")

def test_reconcile_of_200_proxies_is_measured_and_adopts_on_restart(tmp_path):
    """A5: N=200 real instances. Cold start brings all up; an API restart
    adopts them all (same PIDs, no restart). Timings are printed for the
    installer's deadline (pytest -s)."""
    from ipaddress import IPv6Address
    from proxy_api.config import Settings
    from proxy_api.models import ProxyRecord
    ports = bindable_ports(200)
    # loopback-only test harness: targets/egress are loopback, so the SSRF deny is off
    settings = Settings(subnet="::ffff:127.0.0.0/120", bind_ipv4="127.0.0.1", interface="lo",
                        block_private_destinations=False, api_token=ADMIN, data_dir=tmp_path,
                        binary=BINARY, supervisor="local")
    records = [ProxyRecord(port=port, ipv6=IPv6Address(f"::ffff:127.0.0.{i + 1}")) for i, port in enumerate(ports)]
    try:
        backend = LoopbackBackend(settings)
        began = time.monotonic()
        assert backend.reconcile(records) == set()
        cold = time.monotonic() - began
        assert backend.healthy(records)
        before = {port: backend.supervisor.main_pid(port) for port in ports}
        backend.close()
        backend = LoopbackBackend(settings)
        began = time.monotonic()
        assert backend.reconcile(records) == set()
        warm = time.monotonic() - began
        assert {port: backend.supervisor.main_pid(port) for port in ports} == before
        print(f"\nreconcile N=200: cold {cold:.2f}s ({cold / 200 * 1000:.0f} ms/proxy), warm {warm:.2f}s")
        # Generous ceilings: catch O(N^2) or per-proxy sleeps, not machine noise.
        assert cold < 60 and warm < 20, (cold, warm)
    finally:
        stop_all(settings)
