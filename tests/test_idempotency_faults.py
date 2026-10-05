"""Fault injection: crashes around the commit point, then restart and retry.

A crash is a BaseException (skips the in-process rollback, like SIGKILL) and
the restart is a fresh Manager on the same data_dir, as systemd would do.
"""
import json

import pytest

from proxy_api.config import Settings
from proxy_api.manager import Manager, fingerprint
from proxy_api.models import ProxyCreate

class Crash(BaseException):
    pass

class CountingBackend:
    """Fake backend that remembers what is running and how often it changed."""
    def __init__(self):
        self.records, self.applies = [], 0

    def apply(self, records):
        self.applies += 1
        self.records = list(records)

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

def restart(manager, settings):
    manager.close()
    return Manager(settings, CountingBackend())

def run(manager, key, op, port=None):
    plans = {"create": lambda: manager.plan_create(ProxyCreate(protocol="http")),
             "rotate": lambda: manager.plan_rotate(port, "t"),
             "delete": lambda: manager.plan_delete(port, "t")}
    return manager.mutate("t", key, fingerprint(op, port), plans[op])

def crash_after_effect(manager):
    """Backend applied, state not yet renamed into place."""
    def write(records, keys):
        raise Crash()
    manager._write = write

def crash_after_persist(manager):
    """State committed, response never reaches the client."""
    original = manager._write
    def write(records, keys):
        original(records, keys)
        raise Crash()
    manager._write = write

def seeded(settings):
    manager = Manager(settings, CountingBackend())
    manager.mutate("t", None, None, lambda: manager.plan_create(ProxyCreate(protocol="http", owner="t")))
    return manager

@pytest.mark.parametrize("op", ["create", "rotate", "delete"])
def test_crash_after_persist_replays_without_repeating(settings, op):
    manager = seeded(settings)
    port = manager.records[0].port
    before = [r.model_dump() for r in manager.records]
    crash_after_persist(manager)
    with pytest.raises(Crash):
        run(manager, "k1", op, port)
    committed = json.loads((settings.data_dir / "state.json").read_text())
    manager = restart(manager, settings)
    after_restart = [r.model_dump() for r in manager.records]
    assert after_restart != before  # the mutation did commit once
    status, body = run(manager, "k1", op, port)
    status2, body2 = run(manager, "k1", op, port)
    assert (status, body) == (status2, body2)
    assert [r.model_dump() for r in manager.records] == after_restart  # never again
    assert manager.backend.applies == 1  # only the restart convergence
    stored = next(iter(committed["idempotency"].values()))
    assert (stored["status"], stored["body"]) == (status, body)
    if op != "delete":
        assert body["ipv6"] in {str(r.ipv6) for r in manager.records}
        assert "password" not in body

@pytest.mark.parametrize("op", ["create", "rotate", "delete"])
def test_crash_after_effect_rolls_back_on_restart_and_retry_applies_once(settings, op):
    manager = seeded(settings)
    port = manager.records[0].port
    before = [r.model_dump() for r in manager.records]
    crash_after_effect(manager)
    with pytest.raises(Crash):
        run(manager, "k1", op, port)
    manager = restart(manager, settings)
    # Nothing committed: restart converges the backend back to the old state.
    assert [r.model_dump() for r in manager.records] == before
    assert manager.backend.records == manager.records
    status, body = run(manager, "k1", op, port)
    once = [r.model_dump() for r in manager.records]
    assert once != before
    assert run(manager, "k1", op, port) == (status, body)
    manager = restart(manager, settings)
    assert run(manager, "k1", op, port) == (status, body)
    assert [r.model_dump() for r in manager.records] == once

def test_key_reuse_for_other_request_is_refused(settings):
    manager = seeded(settings)
    port = manager.records[0].port
    run(manager, "k1", "rotate", port)
    with pytest.raises(Exception) as error:
        run(manager, "k1", "delete", port)
    assert getattr(error.value, "status_code", None) == 422
    assert manager.records[0].port == port

def test_legacy_state_migrates_without_loss(settings):
    legacy = [{"port": 30000, "protocol": "http", "username": "legacy", "password": "keep-me",
               "ipv6": "2001:db8:1::abcd"}]
    (settings.data_dir / "proxies.json").write_text(json.dumps(legacy))
    manager = Manager(settings, CountingBackend())
    assert manager.records[0].password == "keep-me" and manager.records[0].owner is None
    state = json.loads((settings.data_dir / "state.json").read_text())
    assert state["version"] == 2 and state["proxies"][0]["password"] == "keep-me"
    assert json.loads((settings.data_dir / "proxies.json.v1-backup").read_text()) == legacy
    assert not (settings.data_dir / "proxies.json").exists()
    manager = restart(manager, settings)
    assert [r.model_dump(mode="json", exclude_none=True) for r in manager.records] == legacy
    # Ownerless legacy proxy is invisible and immutable for any owner token.
    assert manager.visible("t") == []
    manager.close()
