"""T22 S1-UDP: the SSRF deny ACL must also hold for SOCKS5 UDP ASSOCIATE.

Real 3proxy 0.9.4; needs TEST_3PROXY_BINARY. 3proxy checks the ACL only against
the address the client declares in the ASSOCIATE request, then relays every
datagram to whatever destination its header names. A client that declares a
public address would therefore reach [::1] over UDP (docs/reviews/T22-security.md N1).
Fix (ADR 0007): with the block on, render_config denies UDPASSOC outright. The
block-off path is proven in test_isolation_3proxy.py (UDP egress test).
"""
import os
import socket
import struct
import subprocess
import threading
import time

import pytest

from proxy_api.backend import render_config
from proxy_api.config import Settings
from proxy_api.models import ProxyRecord

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_3PROXY_BINARY"),
                                reason="Set TEST_3PROXY_BINARY for the real 3proxy UDP check")

def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

@pytest.fixture
def socks(tmp_path):
    """(control socket after auth, [::1] UDP echo port, datagrams the echo got) for a default-settings socks5 proxy."""
    echo = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    echo.bind(("::1", 0))
    received = []

    def serve():
        while True:
            try:
                data, peer = echo.recvfrom(2048)
            except OSError:
                return
            received.append(data)
            echo.sendto(b"secret:" + data, peer)

    threading.Thread(target=serve, daemon=True).start()
    settings = Settings(subnet="2001:db8::/64", bind_ipv4="127.0.0.1", interface="lo",
                        api_token="x" * 32, data_dir=tmp_path)  # production default: blocking on
    record = ProxyRecord(port=free_port(), protocol="socks5", username="u", password="p", ipv6="::1")
    config = tmp_path / "socks.cfg"
    config.write_text(render_config(settings, record))
    process = subprocess.Popen([os.environ["TEST_3PROXY_BINARY"], str(config)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                control = socket.create_connection(("127.0.0.1", record.port), timeout=3)
                break
            except OSError:
                assert time.monotonic() < deadline, "3proxy did not listen"
                time.sleep(0.05)
        control.sendall(b"\x05\x01\x02")
        assert control.recv(2) == b"\x05\x02"
        control.sendall(b"\x01\x01u\x01p")
        assert control.recv(2) == b"\x01\x00"
        yield control, echo.getsockname()[1], received
        control.close()
    finally:
        process.kill()
        process.wait()
        echo.close()

def associate(control, declared):
    """Sends UDP ASSOCIATE declaring `declared` (atyp+addr+port); relay (host, port) or None."""
    control.sendall(b"\x05\x03\x00" + declared)
    try:
        reply = control.recv(64)
    except socket.timeout:
        return None
    if len(reply) < 10 or reply[1] != 0:
        return None
    if reply[3] == 1:
        host, port = socket.inet_ntoa(reply[4:8]), struct.unpack(">H", reply[8:10])[0]
    else:
        host, port = socket.inet_ntop(socket.AF_INET6, reply[4:20]), struct.unpack(">H", reply[20:22])[0]
    return ("127.0.0.1" if host in ("0.0.0.0", "::") else host), port

PUBLIC_V6 = b"\x04" + socket.inet_pton(socket.AF_INET6, "2606:4700::1111") + b"\x00\x00"

def assert_refused(control):
    """No success reply. 3proxy 0.9.4 sends no REP for a denied UDPASSOC: it holds
    the TCP control connection without a relay and closes it on its 30 s timeout."""
    try:
        reply = control.recv(64)
    except socket.timeout:
        return
    assert len(reply) < 2 or reply[1] != 0, f"UDP ASSOCIATE granted: {reply!r}"

@pytest.mark.parametrize("declared", [PUBLIC_V6, b"\x01" + b"\x00" * 6], ids=["public-v6", "0.0.0.0"])
def test_udp_associate_is_refused_by_default(socks, declared):
    control, _, _ = socks
    control.sendall(b"\x05\x03\x00" + declared)
    assert_refused(control)

def test_udp_relay_never_reaches_host_loopback(socks):
    control, echo_port, received = socks
    assert associate(control, PUBLIC_V6) is None, "UDP ASSOCIATE accepted with the SSRF block on"
    # No relay was handed out; also aim a SOCKS-UDP datagram at the proxy port itself.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        header = b"\x00\x00\x00\x04" + socket.inet_pton(socket.AF_INET6, "::1") + struct.pack(">H", echo_port)
        udp.sendto(header + b"ping", control.getpeername())
    time.sleep(1)
    assert received == [], "a datagram reached the host's [::1]"
