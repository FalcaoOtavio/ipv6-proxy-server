import fcntl
import json
import logging
import secrets
from ipaddress import IPv6Address
from threading import RLock

from fastapi import HTTPException

from .backend import atomic_json
from .models import ProxyRecord

logger = logging.getLogger(__name__)


class Manager:
    def __init__(self, settings, backend):
        self.settings = settings
        self.backend = backend
        self.lock = RLock()
        self.broken = False
        self.path = settings.data_dir / "proxies.json"
        self.file_lock = (settings.data_dir / "manager.lock").open("a")
        try:
            fcntl.flock(self.file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file_lock.close()
            raise RuntimeError("Another manager owns this data directory; use one worker")
        try:
            raw = json.loads(self.path.read_text()) if self.path.exists() else []
            self.records = [ProxyRecord.model_validate(item) for item in raw]
            if len({r.port for r in self.records}) != len(self.records):
                raise ValueError("Duplicate persisted ports")
            if len({r.ipv6 for r in self.records}) != len(self.records):
                raise ValueError("Duplicate persisted IPv6 addresses")
            for record in self.records:
                if record.ipv6 not in settings.subnet or not settings.port_start <= record.port <= settings.port_end:
                    raise ValueError("Persisted proxies do not match configured subnet/port range")
            backend.apply(self.records)
        except Exception:
            backend.close()
            self.file_lock.close()
            raise

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

    def _commit(self, records):
        if self.broken:
            raise HTTPException(503, "Recovery required; restart service and inspect logs")
        try:
            self.backend.apply(records)
            atomic_json(self.path, [record.model_dump(mode="json") for record in records])
        except Exception:
            logger.exception("Proxy update failed; restoring previous state")
            try:
                self.backend.apply(self.records)
                atomic_json(self.path, [r.model_dump(mode="json") for r in self.records])
            except Exception:
                self.broken = True
                self.backend.close()
                logger.exception("Rollback failed; service requires recovery")
            raise HTTPException(503, "Proxy update failed; inspect service logs")
        self.records = records

    def create(self, request):
        with self.lock:
            ports = {record.port for record in self.records}
            port = request.port
            if port is None:
                port = next((p for p in range(self.settings.port_start, self.settings.port_end + 1) if p not in ports), None)
            if port is None or port in ports:
                raise HTTPException(409, "Port unavailable")
            if not self.settings.port_start <= port <= self.settings.port_end:
                raise HTTPException(422, "Port outside configured range")
            if request.username and any(r.username == request.username and r.password != request.password for r in self.records):
                raise HTTPException(409, "Same username must use the same password across proxies")
            record = ProxyRecord(**request.model_dump(exclude={"port"}), port=port, ipv6=self._address())
            self._commit([*self.records, record])
            return record

    def get(self, port):
        with self.lock:
            record = next((r for r in self.records if r.port == port), None)
            if record is None:
                raise HTTPException(404, "Proxy not found")
            return record

    def rotate(self, port):
        with self.lock:
            old = self.get(port)
            new = old.model_copy(update={"ipv6": self._address()})
            self._commit([new if r.port == port else r for r in self.records])
            return new

    def delete(self, port):
        with self.lock:
            self.get(port)
            self._commit([r for r in self.records if r.port != port])

    def close(self):
        self.backend.close()
        self.file_lock.close()
