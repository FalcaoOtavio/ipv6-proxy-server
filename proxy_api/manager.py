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
from .models import ProxyRecord, ProxyUpdate

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
            # Adopt healthy instances untouched; a proxy that cannot come up is
            # fenced alone instead of keeping the whole API down.
            self.broken_ports = set(backend.reconcile(self.records))
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

    def _address(self, taken=()):
        used = {int(r.ipv6) for r in self.records} | {int(ip) for ip in taken}
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

    def _commit(self, records, partial=False):
        """Bring the backend from self.records to records, touching only changed ports.

        New/edited ports go first and removals last, so a port move starts the
        new instance before stopping the old one. Without partial, any failure
        undoes the ports already done and raises 503. With partial, a failed
        port is undone alone and the rest stays. Returns (done, failed): done
        is the list of (before, after) transitions now live.
        """
        if self.broken:
            raise HTTPException(503, "Recovery required; restart service and inspect logs")
        old = {r.port: r for r in self.records}
        new = {r.port: r for r in records}
        changed = {p for p in old.keys() | new.keys() if old.get(p) != new.get(p)}
        if changed & self.broken_ports:
            raise HTTPException(503, "Port requires recovery; restart service and inspect logs")
        done, failed = [], set()
        for port in sorted(changed, key=lambda p: (p not in new, p)):
            step = (old.get(port), new.get(port))
            try:
                self._transition(*step)
                done.append(step)
            except Exception:
                logger.exception("Proxy update failed for port %s", port)
                self._undo([step])
                failed.add(port)
                if not partial:
                    self._undo(reversed(done))
                    raise HTTPException(503, "Proxy update failed; inspect service logs")
        return done, failed

    def _undo(self, steps):
        for before, after in steps:
            port = (after or before).port
            try:
                self._transition(after, before)
            except Exception:
                # Only this port is uncertain: fence it (no mutations, no
                # process) until a restart repairs it; others are untouched.
                self.broken_ports.add(port)
                logger.exception("Rollback failed for port %s; it requires recovery", port)
                try:
                    for record in {r.ipv6: r for r in (before, after) if r}.values():
                        self.backend.deprovision(record)
                except Exception:
                    self.broken = True
                    logger.exception("Could not fence port %s; writes blocked until recovery", port)

    def _transition(self, before, after):
        """Move one port from record `before` to `after` (either may be None)."""
        if before is None:
            self.backend.provision(after)
        elif after is None:
            self.backend.deprovision(before)
        else:
            self.backend.replace(before, after)

    def mutate(self, scope, key, request_fingerprint, plan, pinned=False, partial=False):
        """Run one mutation, at most once per (scope, key).

        plan() returns (records, status, body[, reveal]) without side effects;
        body is the stored response and never holds passwords. body may
        instead be a function settle(failed_ports) -> (status, body), used by
        batches whose response depends on what came up. reveal(body) adds
        one-time secrets to the first response only; replays never carry them.
        A replay returns the stored response; a key reused for a different
        request is refused.

        Persisting records + keys in one atomic rename is the commit point. A
        crash before it leaves the previous state on disk and the next start
        converges the processes back to it, so a retried key performs the
        mutation exactly once from the API's view.

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
            records, status, body, *extra = plan()
            reveal = extra[0] if extra else None
            done, failed = self._commit(records, partial)
            if failed:
                previous = {r.port: r for r in self.records}
                records = [r for r in records if r.port not in failed]
                records += [previous[p] for p in failed if p in previous]
            if callable(body):
                status, body = body(failed)
            if key is not None:
                keys[slot] = {"request": request_fingerprint, "status": status, "body": body, "at": now,
                              "pinned": pinned}
                evictable = sorted((k for k in keys if not keys[k].get("pinned")), key=lambda k: keys[k]["at"])
                for stale in evictable[:max(0, len(evictable) - IDEMPOTENCY_MAX)]:
                    del keys[stale]
            try:
                self._write(records, keys)
            except Exception:
                logger.exception("Persisting state failed; restoring changed ports")
                try:
                    self._write(self.records, self.keys)
                except Exception:
                    # Disk no longer trusted: refuse every write, but keep the
                    # running ports serving. Closing the backend here would drop
                    # tenants that were never part of this mutation.
                    self.broken = True
                    logger.exception("State rollback failed; writes blocked until recovery")
                self._undo(reversed(done))
                raise HTTPException(503, "Proxy update failed; inspect service logs")
            self.records, self.keys = records, keys
            return status, (reveal(body) if reveal and body is not None else body)

    def view(self, record):
        body = record.model_dump(mode="json", exclude={"password"})
        return {**body, "ipv4": str(self.settings.bind_ipv4)}

    def visible(self, owner):
        """owner=None is the admin view; any other value sees only its own proxies."""
        return [r for r in self.records if owner is None or r.owner == owner]

    def statuses(self, records):
        states = self.backend.statuses([r for r in records if r.port not in self.broken_ports])
        return {r.port: "fenced" if r.port in self.broken_ports else states[r.port] for r in records}

    def _free_ports(self, start, ports):
        if not self.settings.port_start <= start <= self.settings.port_end:
            raise HTTPException(422, "Port outside configured range")
        return (p for p in range(start, self.settings.port_end + 1) if p not in ports)

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

    def plan_update(self, port, update: ProxyUpdate, owner=None, expected_id=None):
        """Edit credentials/protocol/port; id, owner and ipv6 are kept.

        A port move is one transaction: the instance on the new port comes up
        before the old one stops, so only this proxy's sessions drop.
        """
        current = self.get(port, owner, expected_id)
        changes = update.model_dump(include=update.model_fields_set)
        if changes.get("port", port) != port:
            if changes["port"] in {r.port for r in self.records}:
                raise HTTPException(409, "Port unavailable")
            if not self.settings.port_start <= changes["port"] <= self.settings.port_end:
                raise HTTPException(422, "Port outside configured range")
        new = ProxyRecord.model_validate({**current.model_dump(), **changes})
        records = [r for r in self.records if r.port != port] + [new]
        return records, 200, self.view(new)

    def plan_batch(self, request, owner=None):
        """count new proxies on the next free ports from start_port, ascending.

        Ports need not be contiguous: occupied ones are skipped. Everything is
        validated (ports, unique IPv6s) before any process starts.
        """
        if request.count > self.settings.batch_max:
            raise HTTPException(422, f"count above the configured limit ({self.settings.batch_max})")
        ports = {r.port for r in self.records}
        free = self._free_ports(request.start_port or self.settings.port_start, ports)
        chosen = [p for _, p in zip(range(request.count), free)]
        if len(chosen) < request.count:
            raise HTTPException(409, "Not enough free ports in the configured range")
        created, secrets_by_port, taken = [], {}, []
        for port in chosen:
            username = password = None
            if request.credentials == "common":
                username, password = request.username, request.password
            elif request.credentials == "random":
                username = f"{request.username or 'u'}{port}"
                password = secrets.token_urlsafe(18).replace("-", "A").replace("_", "B")
                secrets_by_port[port] = password
            address = self._address(taken)
            taken.append(address)
            created.append(ProxyRecord(port=port, ipv6=address, protocol=request.protocol, username=username,
                                       password=password, owner=owner or request.owner))

        def settle(failed):
            body = {"created": [self.view(r) for r in created if r.port not in failed],
                    "failed": [{"port": p, "error": "proxy did not start; inspect its log"} for p in sorted(failed)]}
            return (201 if not failed else 207), body

        def reveal(body):
            # Random passwords exist only in this first response (and the 0600 state).
            return {**body, "created": [{**item, "password": secrets_by_port[item["port"]]}
                                        if item["port"] in secrets_by_port else item for item in body["created"]],
                    "failed": body["failed"]}

        return [*self.records, *created], None, settle, reveal if secrets_by_port else None

    def plan_delete(self, port, owner=None, expected_id=None):
        self.get(port, owner, expected_id)
        return [r for r in self.records if r.port != port], 204, None

    def close(self):
        self.backend.close()
        self.file_lock.close()
