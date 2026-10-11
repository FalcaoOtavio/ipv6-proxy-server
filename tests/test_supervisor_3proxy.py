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
    settings = Settings(subnet="::ffff:127.0.0.0/120", bind_ipv4="127.0.0.1", interface="lo",
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
