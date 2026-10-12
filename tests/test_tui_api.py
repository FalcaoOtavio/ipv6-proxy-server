"""GET /proxies?include=status: per-item status, owner-scoped, one supervisor read."""
import hashlib
import json

from fastapi.testclient import TestClient

from proxy_api.app import create_app
from test_api import FakeBackend, settings  # noqa: F401  (fixture)

class CountingBackend(FakeBackend):
    calls = 0

    def healthy(self, records):
        return all(r in self.records for r in records)

    def statuses(self, records):
        CountingBackend.calls += 1
        return super().statuses(records)

def test_include_status_reports_each_proxy_without_503(settings):  # noqa: F811
    with TestClient(create_app(settings, CountingBackend)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token
        a = client.post("/proxies", json={"port": 30000}).json()
        b = client.post("/proxies", json={"port": 30001}).json()
        assert client.get("/proxies").json() == [a, b]  # default contract untouched
        client.app.state.manager.backend.records.pop()  # b died
        assert client.get("/proxies").status_code == 503
        listed = client.get("/proxies", params={"include": "status"})
        assert listed.status_code == 200
        assert listed.json() == [{**a, "status": "running"}, {**b, "status": "failed"}]
        assert client.get("/proxies", params={"include": "bogus"}).status_code == 422
        assert client.get("/proxies", params={"include": "status"}, headers={"Authorization": ""}).status_code == 401

def test_include_status_is_owner_scoped_and_reads_supervisor_once(settings, tmp_path):  # noqa: F811
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"alice": hashlib.sha256(b"a" * 40).hexdigest()}))
    scoped = settings.model_copy(update={"client_tokens_file": tokens})
    with TestClient(create_app(scoped, CountingBackend)) as api:
        admin = {"Authorization": "Bearer " + settings.api_token}
        api.post("/proxies", json={"port": 30000, "owner": "alice"}, headers=admin)
        api.post("/proxies", json={"port": 30001, "owner": "bob"}, headers=admin)
        CountingBackend.calls = 0
        mine = api.get("/proxies?include=status", headers={"Authorization": "Bearer " + "a" * 40}).json()
        assert [(p["port"], p["status"]) for p in mine] == [(30000, "running")]
        assert len(api.get("/proxies?include=status", headers=admin).json()) == 2
        assert CountingBackend.calls == 2
