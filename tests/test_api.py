import json
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv6Address

import pytest
from fastapi.testclient import TestClient

from proxy_api.app import create_app
from proxy_api.backend import render_config
from proxy_api.config import Settings
from proxy_api.manager import Manager
from proxy_api.models import ProxyCreate


class FakeBackend:
    def __init__(self, settings):
        self.records = []
        self.failures = 0

    def _fail(self):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("injected failure after partial apply")

    def reconcile(self, records):
        self.records = list(records)
        return set()

    def provision(self, record):
        self.records = [r for r in self.records if r.port != record.port] + [record]
        self._fail()

    def replace(self, old, new):
        self.records = [new if r.port == old.port else r for r in self.records]
        self._fail()

    def deprovision(self, record):
        self.records = [r for r in self.records if r.port != record.port]
        self._fail()

    def existing_addresses(self):
        return {str(r.ipv6) for r in self.records}

    def healthy(self, records):
        return True

    def close(self):
        pass


@pytest.fixture
def settings(tmp_path):
    return Settings(subnet="2001:db8:1::/64", bind_ipv4="127.0.0.1",
                    interface="eth0", api_token="x" * 32, data_dir=tmp_path)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeBackend)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token
        yield client


def test_lifecycle(client, settings):
    result = client.post("/proxies", json={"username": "alice", "password": "secret"})
    assert result.status_code == 201
    first = result.json()
    assert first["ipv4"] == "127.0.0.1"
    assert "password" not in first
    assert IPv6Address(first["ipv6"]) in settings.subnet
    second = client.post("/proxies", json={"protocol": "socks5"}).json()
    rotated = client.post(f"/proxies/{first['port']}/rotate").json()
    assert rotated["ipv6"] != first["ipv6"]
    assert rotated["port"] == first["port"]
    assert rotated["username"] == "alice"
    assert client.get("/proxies").json()[1] == second
    records = client.app.state.manager.records
    config = render_config(settings, records[0])
    assert f"proxy -6 -n -a -p{first['port']} -i127.0.0.1 -e{rotated['ipv6']}" in config
    assert "users alice:CL:secret" in config
    assert "socks -6 -a" in render_config(settings, records[1])
    assert client.delete(f"/proxies/{first['port']}").status_code == 204
    assert client.get("/proxies").json() == [second]
    assert client.delete(f"/proxies/{first['port']}").status_code == 404


@pytest.mark.parametrize("payload", [
    {"username": "alone"}, {"password": "alone"}, {"username": "a\nallow *", "password": "x"},
    {"username": "a", "password": "$secret"}, {"port": 65536}, {"port": 80},
    {"protocol": "other"}, {"ipv6": "::1"}, {"port": 29999},
])
def test_invalid_inputs(client, payload):
    assert client.post("/proxies", json=payload).status_code == 422


def test_auth(client):
    client.headers.pop("Authorization")
    assert client.get("/proxies").status_code == 401
    assert client.post("/proxies", json={}).status_code == 401
    assert client.post("/proxies/30000/rotate").status_code == 401
    assert client.delete("/proxies/30000").status_code == 401


def test_conflicts(client):
    payload = {"port": 30000, "username": "a", "password": "one"}
    assert client.post("/proxies", json=payload).status_code == 201
    assert client.post("/proxies", json=payload).status_code == 409
    # Each port runs its own 3proxy, so user databases no longer collide.
    assert client.post("/proxies", json={"username": "a", "password": "two"}).status_code == 201
    assert client.post("/proxies/39999/rotate").status_code == 404


def test_rollback(client, settings):
    first = client.post("/proxies", json={}).json()
    manager = client.app.state.manager
    manager.backend.failures = 1
    assert client.post(f"/proxies/{first['port']}/rotate").status_code == 503
    assert client.get("/proxies").json() == [first]
    assert str(manager.backend.records[0].ipv6) == first["ipv6"]
    assert json.loads((settings.data_dir / "state.json").read_text())["proxies"][0]["ipv6"] == first["ipv6"]


def test_rollback_failure_fences_only_the_touched_port(client):
    first = client.post("/proxies", json={}).json()
    other = client.post("/proxies", json={}).json()
    manager = client.app.state.manager
    manager.backend.failures = 2  # the delete and its rollback both fail
    assert client.delete(f"/proxies/{first['port']}").status_code == 503
    assert not manager.broken and manager.broken_ports == {first["port"]}
    # The fenced port is out of the backend; the other keeps serving and mutating.
    assert [r.port for r in manager.backend.records] == [other["port"]]
    assert client.post(f"/proxies/{first['port']}/rotate").status_code == 503
    assert client.delete(f"/proxies/{first['port']}").status_code == 503
    assert client.post(f"/proxies/{other['port']}/rotate").status_code == 200
    assert client.post("/proxies", json={}).status_code == 201

def test_failed_fence_blocks_writes(client):
    client.post("/proxies", json={})
    manager = client.app.state.manager
    manager.backend.failures = 3
    assert client.delete("/proxies/30000").status_code == 503
    assert manager.broken
    assert client.post("/proxies", json={}).status_code == 503
    assert client.get("/proxies").status_code == 503

def test_restart(settings):
    app = create_app(settings, FakeBackend)
    headers = {"Authorization": "Bearer " + settings.api_token}
    with TestClient(app) as client:
        first = client.post("/proxies", json={}, headers=headers).json()
    with TestClient(create_app(settings, FakeBackend)) as client:
        assert client.get("/proxies", headers=headers).json() == [first]
        assert str(client.app.state.manager.backend.records[0].ipv6) == first["ipv6"]


def test_concurrent_creation_and_worker_lock(settings):
    manager = Manager(settings, FakeBackend(settings))
    try:
        with pytest.raises(RuntimeError, match="one worker"):
            Manager(settings, FakeBackend(settings))
        with ThreadPoolExecutor(max_workers=8) as executor:
            create = lambda _: manager.mutate("", None, None, lambda: manager.plan_create(ProxyCreate()))[1]
            records = list(executor.map(create, range(20)))
        assert len({r["port"] for r in records}) == 20
        assert len({r["ipv6"] for r in records}) == 20
    finally:
        manager.close()


def test_exhaustion(settings):
    settings.subnet = type(settings.subnet)("2001:db8::/126")
    with TestClient(create_app(settings, FakeBackend)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token
        for _ in range(3):
            assert client.post("/proxies", json={}).status_code == 201
        assert client.post("/proxies", json={}).status_code == 409
        assert client.post("/proxies/30000/rotate").status_code == 409

def test_if_match_is_compare_and_delete(client):
    first = client.post("/proxies", json={"port": 30005}).json()
    assert len(first["id"]) == 32
    rotated = client.post(f"/proxies/30005/rotate", headers={"If-Match": f'"{first["id"]}"'})
    assert rotated.status_code == 200 and rotated.json()["id"] == first["id"]  # rotate keeps identity
    assert client.delete("/proxies/30005", headers={"If-Match": first["id"]}).status_code == 204
    heir = client.post("/proxies", json={"port": 30005}).json()
    assert heir["id"] != first["id"]  # the port is reused, the identity never is
    for method, path in [("DELETE", "/proxies/30005"), ("POST", "/proxies/30005/rotate")]:
        late = client.request(method, path, headers={"If-Match": first["id"], "Idempotency-Key": "late"})
        assert late.status_code == 412
    assert client.get("/proxies").json() == [heir]
    assert client.delete("/proxies/30005", headers={"If-Match": "nope"}).status_code == 422
    assert client.delete("/proxies/30009", headers={"If-Match": first["id"]}).status_code == 404

def test_keyless_precondition_delete_keys_are_pinned(client, monkeypatch):
    import proxy_api.manager as manager_module
    monkeypatch.setattr(manager_module, "PINNED_MAX", 1)
    client.post("/proxies", json={"port": 30006})
    client.post("/proxies", json={"port": 30007})
    assert client.delete("/proxies/30006", headers={"Idempotency-Key": "d1"}).status_code == 204
    # Pinned capacity exhausted: refused rather than forgetting d1.
    assert client.delete("/proxies/30007", headers={"Idempotency-Key": "d2"}).status_code == 428
    assert [p["port"] for p in client.get("/proxies").json()] == [30007]
