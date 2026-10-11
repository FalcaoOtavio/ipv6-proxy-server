
import hashlib
import time
import pytest
from fastapi.testclient import TestClient
import proxy_api.manager as manager_module
from proxy_api.app import create_app
from proxy_api.config import Settings
from test_idempotency_faults import CountingBackend

@pytest.mark.parametrize("forget", ["ttl", "capacity"])
def test_old_delete_never_removes_new_occupant(tmp_path, monkeypatch, forget):
    settings = Settings(subnet="2001:db8:1::/64", bind_ipv4="127.0.0.1", interface="lo",
                        api_token="synthetic-admin-" + "a" * 32, data_dir=tmp_path)
    headers = {"Authorization": "Bearer " + settings.api_token, "X-Proxy-Owner": "tenant-a"}
    if forget == "capacity":
        # Same eviction branch as the 10k limit, without 10k disposable proxies.
        monkeypatch.setattr(manager_module, "IDEMPOTENCY_MAX", 1)
    with TestClient(create_app(settings, lambda _: CountingBackend())) as client:
        first = client.post("/proxies", headers=headers, json={"port": 30001, "protocol": "http"})
        assert first.status_code == 201
        delete_headers = {**headers, "Idempotency-Key": "old-lease-delete"}
        assert client.delete("/proxies/30001", headers=delete_headers).status_code == 204
        heir_headers = {**headers, "Idempotency-Key": "new-lease-create"}
        heir = client.post("/proxies", headers=heir_headers,
                           json={"port": 30001, "protocol": "http"})
        assert heir.status_code == 201
        before = client.get("/proxies", headers=headers).json()
        assert len(before) == 1
        if forget == "ttl":
            later = time.time() + manager_module.IDEMPOTENCY_TTL + 1
            monkeypatch.setattr(manager_module.time, "time", lambda: later)
        # The Ashford tombstone retries the original DELETE indefinitely.
        retry = client.delete("/proxies/30001", headers=delete_headers)
        after = client.get("/proxies", headers=headers).json()
        assert after == before, f"{forget}: old retry status={retry.status_code} removed new occupant"
