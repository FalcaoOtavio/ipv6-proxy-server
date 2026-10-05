"""Real 3proxy proof for every capability /capabilities announces.

Opt-in: TEST_3PROXY_BINARY=/path/to/3proxy. No root and no interface changes:
egress addresses are v4-mapped IPv6 (::ffff:127.0.0.N), which Linux binds on
loopback, so the destination observes each process's real source address.
"""
import hashlib
import json
import os
import socket
import struct
import threading
import time

import pytest
from fastapi.testclient import TestClient

from proxy_api.app import CAPABILITIES, create_app
from proxy_api.backend import LinuxBackend
from proxy_api.config import Settings

BINARY = os.environ.get("TEST_3PROXY_BINARY")
pytestmark = pytest.mark.skipif(not BINARY, reason="Set TEST_3PROXY_BINARY for real integration")
ADMIN = "a" * 32
TENANT_A, TENANT_B = "tenant-a-" + "1" * 24, "tenant-b-" + "2" * 24

class LoopbackBackend(LinuxBackend):
    """Real 3proxy processes; only the `ip addr` calls are simulated."""
    def __init__(self, settings):
        self.fake_addresses = set()
        super().__init__(settings)

    def existing_addresses(self):
        return set(self.fake_addresses)

    def _ip(self, *args):
        address = args[3].split("/")[0]
        (self.fake_addresses.add if args[2] == "add" else self.fake_addresses.discard)(address)

def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    for item in sockets:
        item.bind(("127.0.0.1", 0))
    ports = [item.getsockname()[1] for item in sockets]
    for item in sockets:
        item.close()
    return ports

class EchoServer:
    """TCP: greets with the peer address, then echoes. UDP: echoes data|peer."""
    def __init__(self):
        self.tcp = socket.socket(socket.AF_INET6)
        self.tcp.bind(("::", 0))
        self.tcp.listen()
        self.udp = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.udp.bind(("::", 0))
        self.port, self.udp_port = self.tcp.getsockname()[1], self.udp.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._udp, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, peer = self.tcp.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn, peer), daemon=True).start()

    def _serve(self, conn, peer):
        with conn:
            conn.sendall(peer[0].encode() + b"\n")
            while data := conn.recv(4096):
                conn.sendall(data)

    def _udp(self):
        while True:
            try:
                data, peer = self.udp.recvfrom(4096)
            except OSError:
                return
            self.udp.sendto(data + b"|" + peer[0].encode(), peer)

    def close(self):
        self.tcp.close()
        self.udp.close()

def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data

def socks_auth(sock, user, password):
    sock.sendall(b"\x05\x01\x02")
    assert recv_exact(sock, 2) == b"\x05\x02"
    sock.sendall(b"\x01" + bytes([len(user)]) + user.encode() + bytes([len(password)]) + password.encode())
    assert recv_exact(sock, 2) == b"\x01\x00"

def read_socks_reply(sock):
    head = recv_exact(sock, 4)
    assert head[1] == 0, f"SOCKS reply {head[1]}"
    raw = recv_exact(sock, 4 if head[3] == 1 else 16)
    port = struct.unpack(">H", recv_exact(sock, 2))[0]
    family = socket.AF_INET if head[3] == 1 else socket.AF_INET6
    return socket.inet_ntop(family, raw), port

MAPPED_TARGET = socket.inet_pton(socket.AF_INET6, "::ffff:127.0.0.1")

def tunnel(proxy, echo):
    """Open a stream to echo through proxy; return (socket, observed egress)."""
    sock = socket.create_connection(("127.0.0.1", proxy["port"]), timeout=5)
    if proxy["protocol"] == "socks5":
        socks_auth(sock, proxy["username"], proxy["password"])
        sock.sendall(b"\x05\x01\x00\x04" + MAPPED_TARGET + struct.pack(">H", echo.port))
        read_socks_reply(sock)
    else:
        credential = __import__("base64").b64encode(f"{proxy['username']}:{proxy['password']}".encode()).decode()
        sock.sendall(f"CONNECT [::ffff:127.0.0.1]:{echo.port} HTTP/1.1\r\n"
                     f"Proxy-Authorization: Basic {credential}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += recv_exact(sock, 1)
        assert head.split(b" ")[1] == b"200", head
    line = b""
    while not line.endswith(b"\n"):
        line += recv_exact(sock, 1)
    return sock, line.decode().strip()

class Heartbeat(threading.Thread):
    """Round-trips a numbered ping every 20 ms over one long-lived tunnel."""
    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock, self.count, self.error = sock, 0, None
        self.halt = threading.Event()

    def run(self):
        try:
            while not self.halt.is_set():
                ping = f"{self.count:08d}".encode()
                self.sock.sendall(ping)
                assert recv_exact(self.sock, 8) == ping
                self.count += 1
                time.sleep(0.02)
        except Exception as exc:
            self.error = exc

    def alive_for(self, seconds):
        start = self.count
        time.sleep(seconds)
        return self.error is None and self.count > start

@pytest.fixture
def env(tmp_path):
    tokens = tmp_path / "client-tokens.json"
    tokens.write_text(json.dumps({"tenant-a": hashlib.sha256(TENANT_A.encode()).hexdigest(),
                                  "tenant-b": hashlib.sha256(TENANT_B.encode()).hexdigest()}))
    ports = free_ports(6)
    settings = Settings(subnet="::ffff:127.0.0.0/120", bind_ipv4="127.0.0.1", interface="lo",
                        api_token=ADMIN, data_dir=tmp_path / "data", binary=BINARY,
                        port_start=min(ports), port_end=max(ports), client_tokens_file=tokens)
    echo = EchoServer()
    with TestClient(create_app(settings, LoopbackBackend)) as client:
        yield client, ports, echo
    echo.close()

def call(client, method, path, token, key=None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    return client.request(method, path, headers=headers, **kwargs)

def make(client, token, port, protocol, user, key=None):
    body = {"port": port, "protocol": protocol, "username": user, "password": user + "-pw"}
    result = call(client, "POST", "/proxies", token, key, json=body)
    assert result.status_code == 201, result.text
    return {**result.json(), "password": body["password"]}

def pid(client, port):
    return client.app.state.manager.backend.processes[port][1].pid

def test_capabilities_are_exactly_the_proven_set(env):
    client, _, _ = env
    assert call(client, "GET", "/capabilities", "wrong" * 8).status_code == 401
    assert call(client, "GET", "/capabilities", TENANT_A).json() == {
        "spec": "ashford.proxy/1", "capabilities": CAPABILITIES}
    assert set(CAPABILITIES) == {"isolated_mutations", "owner_scoping", "idempotency_keys",
                                 "conditional_mutations", "socks5_udp_associate"}

def test_mutating_a_keeps_b_stream_and_process(env):
    client, ports, echo = env
    b = make(client, TENANT_B, ports[0], "socks5", "bob")
    a = make(client, TENANT_A, ports[1], "http", "alice")
    stream, b_egress = tunnel(b, echo)
    assert b_egress == b["ipv6"]
    beat = Heartbeat(stream)
    beat.start()
    b_pid = pid(client, b["port"])
    try:
        assert beat.alive_for(0.3)
        _, before = tunnel(a, echo)
        assert before == a["ipv6"]
        # create, rotate and delete A while B carries traffic.
        c = make(client, TENANT_A, ports[2], "socks5", "carol")
        assert beat.alive_for(0.3)
        rotated = call(client, "POST", f"/proxies/{a['port']}/rotate", TENANT_A)
        assert rotated.status_code == 200
        _, after = tunnel({**a, **rotated.json()}, echo)
        assert after == rotated.json()["ipv6"] != before  # rotate swaps the observed egress IPv6
        assert beat.alive_for(0.3)
        assert call(client, "DELETE", f"/proxies/{a['port']}", TENANT_A).status_code == 204
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", a["port"]), timeout=1).close()
        assert call(client, "DELETE", f"/proxies/{c['port']}", TENANT_A).status_code == 204
        assert beat.alive_for(0.3)
        assert pid(client, b["port"]) == b_pid
        assert beat.error is None and beat.count > 40
        # Control: the probe does detect a dropped B, so the passes above are real.
        client.app.state.manager.backend._stop(b["port"])
        beat.join(timeout=5)
        assert beat.error is not None
    finally:
        beat.halt.set()
        stream.close()

def test_owner_scoping_is_bound_to_the_token(env):
    client, ports, _ = env
    a = make(client, TENANT_A, ports[0], "http", "alice")
    b = make(client, TENANT_B, ports[1], "http", "bob")
    assert a["owner"] == "tenant-a" and b["owner"] == "tenant-b"
    assert [p["port"] for p in call(client, "GET", "/proxies", TENANT_A).json()] == [a["port"]]
    for method, path in [("POST", f"/proxies/{b['port']}/rotate"), ("DELETE", f"/proxies/{b['port']}")]:
        assert call(client, method, path, TENANT_A).status_code == 404  # same as missing
    spoof = {"Authorization": f"Bearer {TENANT_A}", "X-Proxy-Owner": "tenant-b"}
    assert client.get("/proxies", headers=spoof).status_code == 403
    body = {"protocol": "http", "owner": "tenant-b"}
    assert call(client, "POST", "/proxies", TENANT_A, json=body).status_code == 403
    assert {p["port"] for p in call(client, "GET", "/proxies", TENANT_B).json()} == {b["port"]}
    # Admin token sees everything; X-Proxy-Owner narrows it (cooperative scope).
    assert len(call(client, "GET", "/proxies", ADMIN).json()) == 2
    narrowed = client.delete(f"/proxies/{b['port']}", headers={"Authorization": f"Bearer {ADMIN}",
                                                               "X-Proxy-Owner": "tenant-a"})
    assert narrowed.status_code == 404
    assert len(call(client, "GET", "/proxies", ADMIN).json()) == 2

def test_idempotency_keys(env):
    client, ports, _ = env
    first = make(client, TENANT_A, ports[0], "http", "alice", key="create-1")
    replay = call(client, "POST", "/proxies", TENANT_A, "create-1",
                  json={"port": ports[0], "protocol": "http", "username": "alice", "password": "alice-pw"})
    assert replay.status_code == 201 and replay.json() == {k: v for k, v in first.items() if k != "password"}
    assert len(call(client, "GET", "/proxies", TENANT_A).json()) == 1
    other = call(client, "POST", "/proxies", TENANT_A, "create-1", json={"protocol": "socks5"})
    assert other.status_code == 422  # key reused for a different request
    # Lost DELETE response: the retry must not remove whoever inherited the port.
    assert call(client, "DELETE", f"/proxies/{ports[0]}", TENANT_A, "del-1").status_code == 204
    heir = make(client, TENANT_A, ports[0], "http", "heir")
    heir_pid = pid(client, ports[0])
    assert call(client, "DELETE", f"/proxies/{ports[0]}", TENANT_A, "del-1").status_code == 204
    assert [p["port"] for p in call(client, "GET", "/proxies", TENANT_A).json()] == [heir["port"]]
    assert pid(client, ports[0]) == heir_pid
    # Keys are per owner: tenant B reusing the string does not replay A's result.
    assert call(client, "DELETE", f"/proxies/{ports[0]}", TENANT_B, "del-1").status_code == 404
    assert call(client, "POST", "/proxies", TENANT_A, "bad key!", json={}).status_code == 422
    rotated = call(client, "POST", f"/proxies/{ports[0]}/rotate", TENANT_A, "rot-1").json()
    again = call(client, "POST", f"/proxies/{ports[0]}/rotate", TENANT_A, "rot-1").json()
    assert again == rotated  # one rotation, not two

def test_socks5_udp_associate_egresses_from_the_proxy_ipv6(env):
    client, ports, echo = env
    proxy = make(client, TENANT_A, ports[0], "socks5", "udpuser")
    control = socket.create_connection(("127.0.0.1", proxy["port"]), timeout=5)
    try:
        socks_auth(control, proxy["username"], proxy["password"])
        control.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        relay_host, relay_port = read_socks_reply(control)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(3)
        header = b"\x00\x00\x00\x04" + MAPPED_TARGET + struct.pack(">H", echo.udp_port)
        udp.sendto(header + b"ping", (relay_host, relay_port))
        data, _ = udp.recvfrom(4096)
        payload = data[len(header):]
        assert payload == b"ping|" + proxy["ipv6"].encode()
        udp.close()
    finally:
        control.close()

def test_failures_in_a_never_drop_b(env, monkeypatch):
    client, ports, echo = env
    b = make(client, TENANT_B, ports[0], "socks5", "bob")
    a = make(client, TENANT_A, ports[1], "http", "alice")
    stream, _ = tunnel(b, echo)
    beat = Heartbeat(stream)
    beat.start()
    backend = client.app.state.manager.backend
    b_pid = pid(client, b["port"])
    try:
        # A's 3proxy dies: B keeps flowing; the next mutation of A restarts only A.
        backend.processes[a["port"]][1].kill()
        assert beat.alive_for(0.3)
        # Health is owner-scoped: A sees its dead port, B is untouched, admin sees the pool degraded.
        assert call(client, "GET", "/proxies", TENANT_A).status_code == 503
        assert call(client, "GET", "/proxies", TENANT_B).status_code == 200
        assert call(client, "GET", "/proxies", ADMIN).status_code == 503
        assert pid(client, b["port"]) == b_pid
        rotated = call(client, "POST", f"/proxies/{a['port']}/rotate", TENANT_A)
        assert rotated.status_code == 200
        assert tunnel({**a, **rotated.json()}, echo)[1] == rotated.json()["ipv6"]
        assert call(client, "GET", "/proxies", TENANT_B).status_code == 200
        # A new proxy with a config 3proxy rejects: 503 + rollback, B untouched.
        import proxy_api.backend as backend_module
        real = backend_module.render_config
        monkeypatch.setattr(backend_module, "render_config",
                            lambda settings, record: "bogus-directive\n" if record.port == ports[2]
                            else real(settings, record))
        failed = call(client, "POST", "/proxies", TENANT_A,
                      json={"port": ports[2], "protocol": "http", "username": "x", "password": "y"})
        assert failed.status_code == 503
        assert ports[2] not in backend.processes
        assert {p["port"] for p in call(client, "GET", "/proxies", ADMIN).json()} == {a["port"], b["port"]}
        assert not client.app.state.manager.broken
        assert beat.alive_for(0.3)
        assert pid(client, b["port"]) == b_pid and beat.error is None
    finally:
        beat.halt.set()
        stream.close()
