"""Acceptance harness for criteria A/B: real uvicorn API, real 3proxy, real sockets.

Network scope (read before trusting the evidence): egress addresses are
v4-mapped IPv6 (::ffff:127.0.0.N) on loopback. That is "loopback/ULA
complementar, NAO IPv6 roteado": it proves per-process egress selection and
isolation, not routed IPv6 reachability (criterion E, T14).

Only `ip addr` is simulated (no root here); 3proxy, the supervisor, the API
server and the CLI all run for real.
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from proxy_api.app import create_app
from proxy_api.backend import LinuxBackend
from proxy_api.config import Settings
from proxy_api.supervisor import LocalProcessSupervisor

BINARY = os.environ.get("TEST_3PROXY_BINARY")
ROOT = Path(__file__).parents[2]
EVIDENCE_DIR = Path(__file__).parent / "evidence"
ADMIN = "acceptance-admin-token-" + "x" * 16
NETWORK_SCOPE = "loopback v4-mapped (::ffff:127.0.0.0/120) - loopback/ULA complementar, NAO IPv6 roteado"
MAPPED_TARGET = socket.inet_pton(socket.AF_INET6, "::ffff:127.0.0.1")
FRAME = struct.Struct(">Qd")  # sequence, server send time
FRAME_INTERVAL = 0.05
PORT_BLOCK = 48

class AuditSupervisor(LocalProcessSupervisor):
    """Real local supervisor that records every call with its target port."""

    def __init__(self, binary, directory):
        super().__init__(binary, directory)
        self.calls = []
        self.audit_lock = threading.Lock()

    def _log(self, op, port=None):
        with self.audit_lock:
            self.calls.append({"t": time.time(), "op": op, "port": port})

    def start(self, port, config_path):
        self._log("start", port)
        return super().start(port, config_path)

    def stop(self, port):
        self._log("stop", port)
        return super().stop(port)

    def restart(self, port):
        self._log("restart", port)
        return super().restart(port)

    def list_managed(self):
        self._log("list_managed")
        return super().list_managed()

    def mutating_since(self, mark):
        with self.audit_lock:
            return [c for c in self.calls[mark:] if c["op"] in ("start", "stop", "restart")]

    def mark(self):
        with self.audit_lock:
            return len(self.calls)

class LoopbackBackend(LinuxBackend):
    """Real 3proxy processes; only `ip addr` is simulated (v4-mapped binds on lo without it)."""

    def __init__(self, settings):
        self.fake_addresses = set()
        super().__init__(settings, AuditSupervisor(settings.binary, settings.data_dir / "proxies"))

    def existing_addresses(self):
        return set(self.fake_addresses)

    def _ip(self, *args):
        address = args[3].split("/")[0]
        (self.fake_addresses.add if args[2] == "add" else self.fake_addresses.discard)(address)

def free_port_block(size=PORT_BLOCK):
    """A contiguous run of free TCP ports on 127.0.0.1 (batch allocates contiguously)."""
    for base in range(21000, 60000, size):
        held = []
        try:
            for port in range(base, base + size):
                sock = socket.socket()
                held.append(sock)
                sock.bind(("127.0.0.1", port))
            return list(range(base, base + size))
        except OSError:
            continue
        finally:
            for sock in held:
                sock.close()
    raise RuntimeError("no free port block")

class StreamServer:
    """Target: greets with the peer address, then sends one frame every 50 ms until the client leaves."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET6)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("::", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.peers = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, peer = self.sock.accept()
            except OSError:
                return
            self.peers.append(peer[0])
            threading.Thread(target=self._serve, args=(conn, peer), daemon=True).start()

    def _serve(self, conn, peer):
        with conn:
            try:
                conn.sendall(peer[0].encode() + b"\n")
                seq = 0
                while True:
                    conn.sendall(FRAME.pack(seq, time.time()))
                    seq += 1
                    time.sleep(FRAME_INTERVAL)
            except OSError:
                return

    def close(self):
        self.sock.close()

def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("EOF")
        data += chunk
    return data

def open_tunnel(proxy, password, target_port):
    """HTTP CONNECT or SOCKS5 CONNECT to the target; returns (socket, egress seen by target)."""
    sock = socket.create_connection(("127.0.0.1", proxy["port"]), timeout=5)
    user = proxy.get("username")
    if proxy["protocol"] == "socks5":
        if user:
            sock.sendall(b"\x05\x01\x02")
            assert recv_exact(sock, 2) == b"\x05\x02"
            sock.sendall(b"\x01" + bytes([len(user)]) + user.encode() + bytes([len(password)]) + password.encode())
            assert recv_exact(sock, 2) == b"\x01\x00"
        else:
            sock.sendall(b"\x05\x01\x00")
            assert recv_exact(sock, 2) == b"\x05\x00"
        sock.sendall(b"\x05\x01\x00\x04" + MAPPED_TARGET + struct.pack(">H", target_port))
        head = recv_exact(sock, 4)
        assert head[1] == 0, f"SOCKS reply {head[1]}"
        recv_exact(sock, (4 if head[3] == 1 else 16) + 2)
    else:
        auth = ""
        if user:
            auth = "Proxy-Authorization: Basic " + base64.b64encode(f"{user}:{password}".encode()).decode() + "\r\n"
        sock.sendall(f"CONNECT [::ffff:127.0.0.1]:{target_port} HTTP/1.1\r\n{auth}\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += recv_exact(sock, 1)
        assert head.split(b" ")[1] == b"200", head
    line = b""
    while not line.endswith(b"\n"):
        line += recv_exact(sock, 1)
    return sock, line.decode().strip()

class StreamProbe(threading.Thread):
    """Reads frames from one long-lived tunnel and records arrival times."""

    def __init__(self, name, sock, egress):
        super().__init__(daemon=True)
        self.name_, self.sock, self.egress = name, sock, egress
        self.sock.settimeout(5)
        self.arrivals, self.seqs, self.error = [], [], None
        self.bytes = 0
        self.halt = threading.Event()
        self.started_at = time.time()

    def run(self):
        try:
            while not self.halt.is_set():
                seq, _ = FRAME.unpack(recv_exact(self.sock, FRAME.size))
                self.bytes += FRAME.size
                self.seqs.append(seq)
                self.arrivals.append(time.monotonic())
        except Exception as exc:  # EOF, RST or timeout all count as a broken stream
            if not self.halt.is_set():
                self.error = repr(exc)

    def stop(self):
        self.halt.set()
        self.sock.close()
        self.join(timeout=6)

    def stats(self):
        gaps = [b - a for a, b in zip(self.arrivals, self.arrivals[1:])]
        return {"name": self.name_, "egress": self.egress, "frames": len(self.seqs), "bytes": self.bytes,
                "duration_s": round(self.arrivals[-1] - self.arrivals[0], 3) if self.arrivals else 0,
                "max_gap_s": round(max(gaps), 3) if gaps else None,
                "contiguous": self.seqs == list(range(self.seqs[0], self.seqs[0] + len(self.seqs))) if self.seqs else False,
                "error": self.error, "started_at": self.started_at, "ended_at": time.time()}

    def alive(self):
        """Frames still arriving right now."""
        count = len(self.seqs)
        time.sleep(0.3)
        return self.error is None and len(self.seqs) > count

WRAPPER = """#!/bin/sh
# Fault injection for "start": if <port>.fail holds N>0, this start exits 1 and N decrements.
# Otherwise exec keeps the PID: the process IS the real 3proxy.
f="{faults}/$(basename "$1" .cfg).fail"
if [ -s "$f" ]; then
  n=$(cat "$f"); n=$((n - 1))
  if [ "$n" -le 0 ]; then rm -f "$f"; else echo "$n" > "$f"; fi
  exit 1
fi
exec {binary} "$@"
"""

class Harness:
    def __init__(self, tmp_path):
        self.ports = free_port_block()
        self.data_dir = tmp_path / "data"
        self.faults = tmp_path / "faults"
        self.faults.mkdir()
        wrapper = tmp_path / "3proxy-wrapper"
        wrapper.write_text(WRAPPER.format(faults=self.faults, binary=Path(BINARY).resolve()))
        wrapper.chmod(0o755)
        self.settings = Settings(subnet="::ffff:127.0.0.0/120", bind_ipv4="127.0.0.1", interface="lo", block_private_destinations=False,
                                 api_token=ADMIN, data_dir=self.data_dir, binary=wrapper,
                                 port_start=self.ports[0], port_end=self.ports[-1], supervisor="local")
        self.app = create_app(self.settings, LoopbackBackend)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.api_port = listener.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.api_port}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [listener]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        self.http = httpx.Client(base_url=self.base_url, headers={"Authorization": f"Bearer {ADMIN}"}, timeout=60)
        self.target = StreamServer()
        self.passwords = set()

    @property
    def manager(self):
        return self.app.state.manager

    @property
    def supervisor(self):
        return self.manager.backend.supervisor

    def call(self, method, path, **kwargs):
        return self.http.request(method, path, **kwargs)

    def create(self, port, protocol, username=None, password=None):
        body = {"port": port, "protocol": protocol}
        if username:
            body.update(username=username, password=password)
            self.passwords.add(password)
        result = self.call("POST", "/proxies", json=body)
        assert result.status_code == 201, result.text
        return result.json()

    def fail_starts(self, port, times=1):
        (self.faults / f"{port}.fail").write_text(f"{times}\n")

    def pid(self, port):
        return self.supervisor.main_pid(port)

    def config(self, port):
        return (self.data_dir / "proxies" / f"{port}.cfg").read_bytes()

    def state(self):
        return json.loads((self.data_dir / "state.json").read_text())

    def close(self):
        self.http.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.target.close()
        supervisor = LocalProcessSupervisor(self.settings.binary, self.data_dir / "proxies")
        for port in supervisor.list_managed():
            supervisor.stop(port)

@pytest.fixture
def harness(tmp_path):
    if not BINARY:
        # Same convention as tests/test_isolation_3proxy.py; a skip never counts as A/B approval.
        pytest.skip("Acceptance needs real 3proxy: set TEST_3PROXY_BINARY")
    item = Harness(tmp_path)
    try:
        yield item
    finally:
        item.close()

def write_evidence(name, data, passwords=()):
    EVIDENCE_DIR.mkdir(exist_ok=True)
    text = json.dumps({"network_scope": NETWORK_SCOPE, "generated_at": time.time(), **data}, indent=2,
                      sort_keys=True, default=str)
    leaked = [p for p in passwords if p and p in text]
    assert not leaked, "evidence would contain a password"
    (EVIDENCE_DIR / f"{name}.json").write_text(text + "\n")

def cli_env_file(tmp_path, token):
    path = tmp_path / "cli.env"
    path.write_text(f"PROXY_API_TOKEN={token}\n")
    path.chmod(0o600)
    return path

CLI_SHIM = """
import importlib.machinery, importlib.util, os, sys
loader = importlib.machinery.SourceFileLoader("ipv6_proxy_cli", sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)
cli.API_URL = sys.argv[2]
# Same code path as `sudo ipv6-proxy`; only root ownership of the env file is relaxed to this uid.
sys.exit(cli.main(sys.argv[4:], env_file=sys.argv[3], expected_uid=os.getuid(), effective_uid=0))
"""

def run_cli(base_url, env_file, *argv):
    """Real CLI in its own process against the real API server."""
    return subprocess.run([sys.executable, "-c", CLI_SHIM, str(ROOT / "ipv6-proxy"), base_url, str(env_file), *argv],
                          capture_output=True, text=True, timeout=120)
