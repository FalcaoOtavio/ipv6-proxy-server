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

    def apply(self, records):
        self.records = list(records)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("injected failure after partial apply")

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
    config = render_config(settings, client.app.state.manager.records)
    assert f"proxy -6 -n -a -p{first['port']} -i127.0.0.1 -e{rotated['ipv6']}" in config
    assert "socks -6 -a" in config
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
    assert client.post("/proxies", json={"username": "a", "password": "two"}).status_code == 409
    assert client.post("/proxies/39999/rotate").status_code == 404


def test_rollback(client, settings):
    first = client.post("/proxies", json={}).json()
    manager = client.app.state.manager
    manager.backend.failures = 1
    assert client.post(f"/proxies/{first['port']}/rotate").status_code == 503
    assert client.get("/proxies").json() == [first]
    assert str(manager.backend.records[0].ipv6) == first["ipv6"]
    assert json.loads((settings.data_dir / "proxies.json").read_text())[0]["ipv6"] == first["ipv6"]


def test_rollback_failure_blocks_writes(client):
    client.post("/proxies", json={})
    manager = client.app.state.manager
    manager.backend.failures = 2
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
            records = list(executor.map(lambda _: manager.create(ProxyCreate()), range(20)))
        assert len({r.port for r in records}) == 20
        assert len({r.ipv6 for r in records}) == 20
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
