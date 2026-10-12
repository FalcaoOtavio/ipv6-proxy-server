import pytest
import proxy_api.backend as backend_module
from test_isolation_3proxy import pytestmark, env, make, call, tunnel, Heartbeat, pid, TENANT_A, TENANT_B

def test_failure_and_rollback_of_a_preserve_b(env, monkeypatch):
    client, ports, echo = env
    b = make(client, TENANT_B, ports[0], "socks5", "bob")
    a = make(client, TENANT_A, ports[1], "http", "alice")
    stream, _ = tunnel(b, echo)
    beat = Heartbeat(stream)
    beat.start()
    backend = client.app.state.manager.backend
    b_pid = pid(client, b["port"])
    try:
        assert beat.alive_for(0.3)
        real = backend_module.render_config
        monkeypatch.setattr(backend_module, "render_config", lambda settings, record:
            "bogus-directive\n" if record.port == a["port"] else real(settings, record))
        result = call(client, "POST", f"/proxies/{a['port']}/rotate", TENANT_A)
        assert result.status_code == 503
        survived = beat.alive_for(0.3)
        assert survived and pid(client, b["port"]) == b_pid, (
            f"unrelated B lost after A update+rollback failure: B_pid={pid(client, b['port'])}/{b_pid}, "
            f"heartbeat={beat.error!r}, manager_broken={client.app.state.manager.broken}")
    finally:
        beat.halt.set()
        stream.close()
        beat.join(timeout=5)
