"""T03 contracts: GET /{port} + status, PATCH, batch, per-target compensation."""
import json
from ipaddress import IPv6Address

import pytest
from fastapi.testclient import TestClient

from proxy_api.app import CAPABILITIES, create_app
from proxy_api.config import Settings

ADMIN = "x" * 32

class TargetBackend:
    """Fake per-target backend. fail_on: ports whose next op fails; every op is logged."""
    def __init__(self, settings):
        self.live = {}
        self.ops = []
        self.fail_on = {}  # port -> how many upcoming ops on it fail

    def _maybe_fail(self, port):
        if self.fail_on.get(port):
            self.fail_on[port] -= 1
            raise RuntimeError(f"injected failure on {port}")

    def reconcile(self, records):
        self.live = {r.port: r for r in records}
        return set()

    def provision(self, record):
        self.ops.append(("provision", record.port))
        self._maybe_fail(record.port)
        self.live[record.port] = record

    def replace(self, old, new):
        self.ops.append(("replace", old.port, new.port))
        if old.port != new.port:
            self.provision(new)
            self.deprovision(old)
            return
        self._maybe_fail(new.port)
        self.live[new.port] = new

    def deprovision(self, record):
        self.ops.append(("deprovision", record.port))
        self._maybe_fail(record.port)
        self.live.pop(record.port, None)

    def existing_addresses(self):
        return {str(r.ipv6) for r in self.live.values()}

    def statuses(self, records):
        return {r.port: "running" if self.live.get(r.port) == r else "failed" for r in records}

    def healthy(self, records):
        return all(self.live.get(r.port) == r for r in records)

    def close(self):
        pass

@pytest.fixture
def settings(tmp_path):
    tokens = tmp_path / "tokens.json"
    import hashlib
    tokens.write_text(json.dumps({"tenant-a": hashlib.sha256(b"a" * 32).hexdigest(),
                                  "tenant-b": hashlib.sha256(b"b" * 32).hexdigest()}))
    return Settings(subnet="2001:db8:1::/64", bind_ipv4="127.0.0.1", interface="eth0", api_token=ADMIN,
                    data_dir=tmp_path / "data", port_start=30000, port_end=30019, client_tokens_file=tokens,
                    batch_max=10)

@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, TargetBackend)) as client:
        client.headers["Authorization"] = "Bearer " + ADMIN
        yield client

def as_tenant(name):
    return {"Authorization": "Bearer " + name[-1] * 32}

def backend(client):
    return client.app.state.manager.backend

def state(settings):
    return json.loads((settings.data_dir / "state.json").read_text())

def test_capabilities_add_new_without_removing(client, settings):
    # Default (block on): UDP ASSOCIATE is denied by render_config, so not announced (ADR 0007).
    body = client.get("/capabilities").json()
    assert body["capabilities"] == [name for name in CAPABILITIES if name != "socks5_udp_associate"]
    open_settings = settings.model_copy(update={"block_private_destinations": False,
                                                "data_dir": settings.data_dir.parent / "open"})
    with TestClient(create_app(open_settings, TargetBackend)) as open_client:
        open_client.headers["Authorization"] = "Bearer " + ADMIN
        assert open_client.get("/capabilities").json()["capabilities"] == CAPABILITIES
    assert {"isolated_mutations", "owner_scoping", "idempotency_keys", "conditional_mutations",
            "socks5_udp_associate", "edit", "batch", "per_proxy_status"} == set(CAPABILITIES)

def test_get_one_with_status(client):
    created = client.post("/proxies", json={"username": "u", "password": "p"}).json()
    got = client.get(f"/proxies/{created['port']}")
    assert got.status_code == 200
    assert got.json() == {**created, "status": "running"} and "password" not in got.json()
    assert "status" not in client.get("/proxies").json()[0]  # list contract unchanged
    backend(client).live.pop(created["port"])
    assert client.get(f"/proxies/{created['port']}").json()["status"] == "failed"
    assert client.get("/proxies/30019").status_code == 404

def test_get_one_reports_fenced(client):
    a = client.post("/proxies", json={}).json()
    backend(client).fail_on[a["port"]] = 2  # rotate and its compensation both fail
    assert client.post(f"/proxies/{a['port']}/rotate").status_code == 503
    assert client.get(f"/proxies/{a['port']}").json()["status"] == "fenced"

def test_patch_credentials_protocol_keep_identity(client, settings):
    a = client.post("/proxies", json={"username": "u", "password": "p"}).json()
    other = client.post("/proxies", json={}).json()
    backend(client).ops.clear()
    edited = client.patch(f"/proxies/{a['port']}", json={"protocol": "socks5", "username": "v", "password": "q"})
    assert edited.status_code == 200
    body = edited.json()
    assert (body["id"], body["ipv6"], body["owner"], body["port"]) == (a["id"], a["ipv6"], a["owner"], a["port"])
    assert body["protocol"] == "socks5" and body["username"] == "v" and "password" not in body
    assert backend(client).ops == [("replace", a["port"], a["port"])]  # only the target
    assert {p["port"]: p for p in state(settings)["proxies"]}[a["port"]]["password"] == "q"
    removed = client.patch(f"/proxies/{a['port']}", json={"username": None, "password": None}).json()
    assert removed["username"] is None
    assert backend(client).live[other["port"]].model_dump(mode="json")["id"] == other["id"]

@pytest.mark.parametrize("body", [{}, {"username": "only"}, {"password": "only"}, {"username": "a", "password": None},
                                  {"protocol": None}, {"port": None}, {"ipv6": "2001:db8:1::5"}, {"id": "0" * 32},
                                  {"owner": "tenant-b"}, {"protocol": "ftp"}])
def test_patch_rejects_invalid(client, body):
    a = client.post("/proxies", json={}).json()
    assert client.patch(f"/proxies/{a['port']}", json=body).status_code == 422

def test_patch_port_moves_new_before_old_and_conflicts(client, settings):
    a = client.post("/proxies", json={"port": 30000}).json()
    b = client.post("/proxies", json={"port": 30001}).json()
    backend(client).ops.clear()
    assert client.patch("/proxies/30000", json={"port": 30001}).status_code == 409
    assert client.patch("/proxies/30000", json={"port": 40000}).status_code == 422
    assert backend(client).ops == []  # conflicts have no effect
    moved = client.patch("/proxies/30000", json={"port": 30005}).json()
    assert (moved["id"], moved["ipv6"], moved["port"]) == (a["id"], a["ipv6"], 30005)
    assert backend(client).ops == [("provision", 30005), ("deprovision", 30000)]  # new up before old stops
    assert sorted(backend(client).live) == [30001, 30005]
    assert {p["port"] for p in state(settings)["proxies"]} == {30001, 30005}
    assert client.get("/proxies/30001").json()["id"] == b["id"]

def test_patch_port_failure_compensates_only_target(client, settings):
    client.post("/proxies", json={"port": 30000})
    b = client.post("/proxies", json={"port": 30001}).json()
    before = state(settings)
    backend(client).fail_on[30000] = 1  # stopping the old instance fails
    assert client.patch("/proxies/30000", json={"port": 30007}).status_code == 503
    assert state(settings) == before
    assert sorted(backend(client).live) == [30000, 30001] and backend(client).live[30001].id == b["id"]
    assert 30007 not in backend(client).live

def test_patch_if_match_after_port_reuse_and_idempotency(client):
    old = client.post("/proxies", json={"port": 30000}).json()
    assert client.delete("/proxies/30000").status_code == 204
    heir = client.post("/proxies", json={"port": 30000}).json()
    for method, path, kwargs in [("PATCH", "/proxies/30000", {"json": {"protocol": "socks5"}}),
                                 ("DELETE", "/proxies/30000", {}), ("POST", "/proxies/30000/rotate", {})]:
        assert client.request(method, path, headers={"If-Match": old["id"]}, **kwargs).status_code == 412
    assert client.get("/proxies/30000").json()["id"] == heir["id"]
    ok = client.patch("/proxies/30000", json={"protocol": "socks5"}, headers={"If-Match": heir["id"], "Idempotency-Key": "e1"})
    ops = len(backend(client).ops)
    again = client.patch("/proxies/30000", json={"protocol": "socks5"}, headers={"If-Match": heir["id"], "Idempotency-Key": "e1"})
    assert ok.status_code == again.status_code == 200 and ok.json() == again.json()
    assert len(backend(client).ops) == ops  # replay did not run again
    other = client.patch("/proxies/30000", json={"protocol": "http"}, headers={"If-Match": heir["id"], "Idempotency-Key": "e1"})
    assert other.status_code == 422

def test_same_username_different_passwords_allowed(client):
    # Separate 3proxy processes: the old "same username => same password" rule is gone.
    assert client.post("/proxies", json={"username": "shared", "password": "one"}).status_code == 201
    assert client.post("/proxies", json={"username": "shared", "password": "two"}).status_code == 201
    a = client.get("/proxies").json()[0]
    assert client.patch(f"/proxies/{a['port']}", json={"username": "shared", "password": "three"}).status_code == 200

def test_ownership_on_patch_and_batch(client):
    mine = client.post("/proxies", json={}, headers=as_tenant("tenant-a")).json()
    assert mine["owner"] == "tenant-a"
    for path in (f"/proxies/{mine['port']}",):
        assert client.patch(path, json={"protocol": "socks5"}, headers=as_tenant("tenant-b")).status_code == 404
        assert client.get(path, headers=as_tenant("tenant-b")).status_code == 404
    assert client.post("/proxies/batch", json={"count": 1, "owner": "tenant-a"},
                       headers=as_tenant("tenant-b")).status_code == 403
    made = client.post("/proxies/batch", json={"count": 2}, headers=as_tenant("tenant-b")).json()
    assert {p["owner"] for p in made["created"]} == {"tenant-b"}
    assert len(client.get("/proxies", headers=as_tenant("tenant-a")).json()) == 1

def test_batch_common_skips_taken_ports(client, settings):
    client.post("/proxies", json={"port": 30002})
    result = client.post("/proxies/batch", json={"count": 3, "start_port": 30001, "protocol": "socks5",
                                                  "credentials": "common", "username": "c", "password": "pw"})
    assert result.status_code == 201
    created = result.json()["created"]
    assert [p["port"] for p in created] == [30001, 30003, 30004] and result.json()["failed"] == []
    assert all(p["username"] == "c" and "password" not in p and p["protocol"] == "socks5" for p in created)
    assert len({p["ipv6"] for p in client.get("/proxies").json()}) == 4
    assert all(IPv6Address(p["ipv6"]) in settings.subnet for p in created)

def test_batch_random_reveals_passwords_once(client, settings):
    first = client.post("/proxies/batch", json={"count": 2, "credentials": "random", "username": "r"},
                        headers={"Idempotency-Key": "b1"})
    assert first.status_code == 201
    created = first.json()["created"]
    assert [p["username"] for p in created] == ["r30000", "r30001"]
    assert all(len(p["password"]) >= 20 for p in created) and created[0]["password"] != created[1]["password"]
    stored = {p["port"]: p["password"] for p in state(settings)["proxies"]}
    assert all(stored[p["port"]] == p["password"] for p in created)
    replay = client.post("/proxies/batch", json={"count": 2, "credentials": "random", "username": "r"},
                         headers={"Idempotency-Key": "b1"})
    assert replay.status_code == 201 and len(client.get("/proxies").json()) == 2
    assert [p.get("password") for p in replay.json()["created"]] == [None, None]
    assert "password" not in json.dumps(state(settings)["idempotency"])

@pytest.mark.parametrize("body,status", [
    ({"count": 0}, 422), ({"count": 11}, 422), ({"count": 21}, 422), ({"count": 1, "start_port": 29999}, 422),
    ({"count": 1, "credentials": "common", "username": "u"}, 422),
    ({"count": 1, "credentials": "none", "username": "u", "password": "p"}, 422),
    ({"count": 1, "credentials": "random", "password": "p"}, 422), ({"count": 1, "extra": 1}, 422),
    ({"count": 3, "start_port": 30018}, 409)])
def test_batch_validates_before_any_effect(client, settings, body, status):
    assert client.post("/proxies/batch", json=body).status_code == status
    assert backend(client).ops == [] and client.get("/proxies").json() == []

def test_batch_all_or_nothing_compensates_only_new(client, settings):
    keep = client.post("/proxies", json={"port": 30000}).json()
    before = state(settings)
    backend(client).ops.clear()
    backend(client).fail_on[30003] = 1
    assert client.post("/proxies/batch", json={"count": 4, "start_port": 30001}).status_code == 503
    assert state(settings) == before
    assert list(backend(client).live) == [30000] and backend(client).live[30000].id == keep["id"]
    assert all(op[-1] != 30000 for op in backend(client).ops)  # the existing proxy was never touched

def test_batch_partial_keeps_created_and_lists_failures(client, settings):
    backend(client).fail_on[30001] = 1
    result = client.post("/proxies/batch", json={"count": 3, "partial": True}, headers={"Idempotency-Key": "p1"})
    assert result.status_code == 207
    assert [p["port"] for p in result.json()["created"]] == [30000, 30002]
    assert [f["port"] for f in result.json()["failed"]] == [30001]
    assert sorted(p["port"] for p in state(settings)["proxies"]) == [30000, 30002]
    replay = client.post("/proxies/batch", json={"count": 3, "partial": True}, headers={"Idempotency-Key": "p1"})
    assert replay.status_code == 207 and replay.json() == result.json()

def test_batch_double_failure_fences_only_failed_target(client, settings):
    keep = client.post("/proxies", json={"port": 30000}).json()
    backend(client).fail_on[30002] = 2  # start fails and its compensation fails
    assert client.post("/proxies/batch", json={"count": 3, "start_port": 30001}).status_code == 503
    manager = client.app.state.manager
    assert manager.broken_ports == {30002} and not manager.broken
    assert [p["id"] for p in state(settings)["proxies"]] == [keep["id"]]
    assert list(backend(client).live) == [30000]
    assert client.post("/proxies", json={}).status_code == 201  # others keep mutating
    assert client.post(f"/proxies/{keep['port']}/rotate").status_code == 200

@pytest.mark.parametrize("op", ["create", "edit", "rotate", "delete"])
def test_double_failure_fences_only_target(client, settings, op):
    a = client.post("/proxies", json={"port": 30000}).json()
    b = client.post("/proxies", json={"port": 30001}).json()
    target = 30002 if op == "create" else a["port"]
    backend(client).fail_on[target] = 2
    request = {"create": ("POST", "/proxies", {"json": {"port": 30002}}),
               "edit": ("PATCH", "/proxies/30000", {"json": {"protocol": "socks5"}}),
               "rotate": ("POST", "/proxies/30000/rotate", {}),
               "delete": ("DELETE", "/proxies/30000", {})}[op]
    before = state(settings)
    assert client.request(request[0], request[1], **request[2]).status_code == 503
    manager = client.app.state.manager
    assert manager.broken_ports == {target} and not manager.broken
    assert state(settings)["proxies"] == before["proxies"]  # disk stays at the last committed state
    assert backend(client).live[30001].id == b["id"]
    assert client.post("/proxies/30001/rotate").status_code == 200
    if op != "create":
        assert client.post("/proxies/30000/rotate").status_code == 503

def test_persistence_failure_compensates_target(client, settings, monkeypatch):
    client.post("/proxies", json={"port": 30001})
    manager = client.app.state.manager
    real = manager._write
    calls = []
    def flaky(records, keys):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        real(records, keys)
    monkeypatch.setattr(manager, "_write", flaky)
    assert client.post("/proxies", json={"port": 30000}).status_code == 503
    assert list(backend(client).live) == [30001] and not manager.broken
    assert [p["port"] for p in state(settings)["proxies"]] == [30001]

def test_concurrent_mixed_mutations_never_duplicate(client):
    from concurrent.futures import ThreadPoolExecutor
    def work(i):
        if i % 3 == 0:
            return client.post("/proxies/batch", json={"count": 2}).status_code
        return client.post("/proxies", json={}).status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(work, range(9)))
    assert set(codes) <= {201, 409}
    listed = client.get("/proxies").json()
    assert len(listed) == 12
    for field in ("port", "ipv6", "id"):
        assert len({p[field] for p in listed}) == len(listed)
