import fcntl
import hashlib
import json
import logging
import secrets
import time
from ipaddress import IPv6Address
from threading import RLock

from fastapi import HTTPException

from .backend import atomic_json
from .models import ProxyRecord

logger = logging.getLogger(__name__)

# Idempotency entries live in the same state document as the proxies, so a
# mutation and its replayable result commit in one atomic rename.
IDEMPOTENCY_TTL = 24 * 3600
IDEMPOTENCY_MAX = 10000
# Pinned keys are never forgotten; past this many, new ones are refused (428)
# instead of silently dropping an old one.
PINNED_MAX = 100000
STATE_VERSION = 2

def fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()

class Manager:
    def __init__(self, settings, backend):
        self.settings = settings
        self.backend = backend
        self.lock = RLock()
        self.broken = False
        self.broken_ports = set()
        self.path = settings.data_dir / "state.json"
        self.legacy_path = settings.data_dir / "proxies.json"
        self.file_lock = (settings.data_dir / "manager.lock").open("a")
        try:
            fcntl.flock(self.file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file_lock.close()
            raise RuntimeError("Another manager owns this data directory; use one worker")
        try:
            raw, self.keys = self._load()
            self.records = [ProxyRecord.model_validate(item) for item in raw]
            if len({r.port for r in self.records}) != len(self.records):
                raise ValueError("Duplicate persisted ports")
            if len({r.ipv6 for r in self.records}) != len(self.records):
                raise ValueError("Duplicate persisted IPv6 addresses")
            for record in self.records:
                if record.ipv6 not in settings.subnet or not settings.port_start <= record.port <= settings.port_end:
                    raise ValueError("Persisted proxies do not match configured subnet/port range")
            if len({r.id for r in self.records}) != len(self.records):
                raise ValueError("Duplicate persisted proxy ids")
            backend.apply(self.records)
            if not self.path.exists() or any("id" not in item for item in raw):
                # Upgrade from the 0.1.0 layout: state.json becomes the source of
                # truth; proxies.json is kept, renamed, as the rollback copy.
                self._write(self.records, self.keys)
                if self.legacy_path.exists():
                    self.legacy_path.rename(self.legacy_path.with_suffix(".json.v1-backup"))
        except Exception:
            backend.close()
            self.file_lock.close()
            raise

    def _load(self):
        if self.path.exists():
            state = json.loads(self.path.read_text())
            if state.get("version") != STATE_VERSION:
                raise ValueError("Unsupported state.json version")
            return state["proxies"], state["idempotency"]
        if self.legacy_path.exists():
            return json.loads(self.legacy_path.read_text()), {}
        return [], {}

    def _write(self, records, keys):
        atomic_json(self.path, {"version": STATE_VERSION,
                                "proxies": [record.model_dump(mode="json") for record in records],
                                "idempotency": keys})

    def _address(self):
        used = {int(r.ipv6) for r in self.records}
        used |= {int(IPv6Address(ip)) for ip in self.backend.existing_addresses()}
        network = self.settings.subnet
        base = int(network.network_address)
        capacity = network.num_addresses - 1
        occupied = {ip for ip in used if base < ip < base + network.num_addresses}
        if len(occupied) >= capacity:
            raise HTTPException(409, "IPv6 subnet exhausted; rotation needs a free address")
        candidate = secrets.randbelow(capacity) + 1
        while base + candidate in used:
            candidate = candidate % capacity + 1
        return IPv6Address(base + candidate)

    def _commit(self, records, keys):
        """Apply records, then persist records + keys in one atomic rename.

        The rename is the commit point. A crash before it leaves the previous
        state on disk and the next start converges the processes back to it, so
        a retried key performs the mutation exactly once from the API's view.
        """
        if self.broken:
            raise HTTPException(503, "Recovery required; restart service and inspect logs")
        old = {r.port: r for r in self.records}
        new = {r.port: r for r in records}
        changed = {p for p in old.keys() | new.keys() if old.get(p) != new.get(p)}
        if changed & self.broken_ports:
            raise HTTPException(503, "Port requires recovery; restart service and inspect logs")
        try:
            self.backend.apply(self._serving(records))
            self._write(records, keys)
        except Exception:
            logger.exception("Proxy update failed; restoring previous state")
            try:
                self._write(self.records, self.keys)
            except Exception:
                # Disk no longer trusted: refuse every write, but keep the
                # running ports serving. Closing the backend here would drop
                # tenants that were never part of this mutation.
                self.broken = True
                logger.exception("State rollback failed; writes blocked until recovery")
            try:
                self.backend.apply(self._serving(self.records))
            except Exception:
                # Only the ports this mutation touched are uncertain. They are
                # fenced (no mutations, left out of later applies) until a
                # restart repairs them; every other port keeps its process.
                self.broken_ports |= changed
                logger.exception("Rollback failed for ports %s; they require recovery", sorted(changed))
                try:
                    self.backend.apply(self._serving(self.records))
                except Exception:
                    self.broken = True
                    logger.exception("Could not fence failed ports; writes blocked until recovery")
            raise HTTPException(503, "Proxy update failed; inspect service logs")
        self.records, self.keys = records, keys

    def _serving(self, records):
        return [r for r in records if r.port not in self.broken_ports]

    def mutate(self, scope, key, request_fingerprint, plan, pinned=False):
        """Run one mutation, at most once per (scope, key).

        plan() returns (records, status, body) without side effects; body is
        the stored response and never holds passwords. A replay returns the
        stored response; a key reused for a different request is refused.

        pinned keys never expire nor get evicted. DELETE without If-Match needs
        that: once its key is forgotten, a late retry is indistinguishable from
        a fresh delete of whoever inherited the port. Cost ~200 bytes per
        delete in state.json, capped by PINNED_MAX; If-Match is the stateless way and the one the
        Ashford client uses.
        """
        with self.lock:
            now = time.time()
            keys = {k: v for k, v in self.keys.items() if v.get("pinned") or now - v["at"] < IDEMPOTENCY_TTL}
            slot = fingerprint(scope, key)
            if key is not None and slot in keys:
                if keys[slot]["request"] != request_fingerprint:
                    raise HTTPException(422, "Idempotency-Key reused for a different request")
                return keys[slot]["status"], keys[slot]["body"]
            if key is not None and pinned and sum(1 for v in keys.values() if v.get("pinned")) >= PINNED_MAX:
                raise HTTPException(428, "Idempotent DELETE capacity reached; send If-Match")
            records, status, body = plan()
            if key is not None:
                keys[slot] = {"request": request_fingerprint, "status": status, "body": body, "at": now,
                              "pinned": pinned}
                evictable = sorted((k for k in keys if not keys[k].get("pinned")), key=lambda k: keys[k]["at"])
                for stale in evictable[:max(0, len(evictable) - IDEMPOTENCY_MAX)]:
                    del keys[stale]
            self._commit(records, keys)
            return status, body

    def view(self, record):
        body = record.model_dump(mode="json", exclude={"password"})
        return {**body, "ipv4": str(self.settings.bind_ipv4)}

    def visible(self, owner):
        """owner=None is the admin view; any other value sees only its own proxies."""
        return [r for r in self.records if owner is None or r.owner == owner]

    def plan_create(self, request):
        ports = {record.port for record in self.records}
        port = request.port
        if port is None:
            port = next((p for p in range(self.settings.port_start, self.settings.port_end + 1) if p not in ports), None)
        if port is None or port in ports:
            raise HTTPException(409, "Port unavailable")
        if not self.settings.port_start <= port <= self.settings.port_end:
            raise HTTPException(422, "Port outside configured range")
        record = ProxyRecord(**request.model_dump(exclude={"port"}), port=port, ipv6=self._address())
        return [*self.records, record], 201, self.view(record)

    def get(self, port, owner=None, expected_id=None):
        with self.lock:
            # Another owner's proxy answers exactly like a missing one.
            record = next((r for r in self.visible(owner) if r.port == port), None)
            if record is None:
                raise HTTPException(404, "Proxy not found")
            if expected_id is not None and record.id != expected_id:
                # The port now belongs to another proxy (reused after a delete).
                raise HTTPException(412, "Proxy identity does not match If-Match")
            return record

    def plan_rotate(self, port, owner=None, expected_id=None):
        new = self.get(port, owner, expected_id).model_copy(update={"ipv6": self._address()})
        return [new if r.port == port else r for r in self.records], 200, self.view(new)

    def plan_delete(self, port, owner=None, expected_id=None):
        self.get(port, owner, expected_id)
        return [r for r in self.records if r.port != port], 204, None

    def close(self):
        self.backend.close()
        self.file_lock.close()
