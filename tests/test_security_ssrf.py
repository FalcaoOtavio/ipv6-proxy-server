"""T10 SSRF acceptance: a proxy client must not reach the host's own loopback.

Real 3proxy 0.9.4; needs TEST_3PROXY_BINARY and curl. render_config denies
loopback/internal destinations by default (auth iponly/strong + deny before
allow); the override exists only for loopback tests and must really open it.
"""
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proxy_api.backend import render_config
from proxy_api.config import Settings
from proxy_api.models import ProxyRecord

pytestmark = [
    pytest.mark.skipif(not os.environ.get("TEST_3PROXY_BINARY") or not shutil.which("curl"),
                       reason="Set TEST_3PROXY_BINARY (and install curl) for the real 3proxy SSRF check"),
]

def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

@pytest.mark.parametrize("block", [True, False], ids=["default-blocks", "override-allows"])
def test_proxies_refuse_host_loopback(tmp_path, block):
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "6")
            self.end_headers()
            self.wfile.write(b"secret")

        def log_message(self, *args):
            pass

    server = Server(("::1", 0), Handler)  # stands in for a loopback-only admin service
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = Settings(subnet="2001:db8::/64", bind_ipv4="127.0.0.1", interface="lo",
                        api_token="x" * 32, data_dir=tmp_path,
                        **({} if block else {"block_private_destinations": False}))
    assert Settings.model_fields["block_private_destinations"].default is True
    # Egress from ::1 so the loopback target is reachable at the socket level:
    # only the proxy's own ACL may refuse it.
    records = [ProxyRecord(port=free_port(), protocol="http", ipv6="::1"),
               ProxyRecord(port=free_port(), protocol="http", username="u", password="p", ipv6="::1"),
               ProxyRecord(port=free_port(), protocol="socks5", username="u", password="p", ipv6="::1")]
    processes = []
    try:
        for record in records:
            config = tmp_path / f"{record.port}.cfg"
            config.write_text(render_config(settings, record))
            processes.append(subprocess.Popen([os.environ["TEST_3PROXY_BINARY"], str(config)],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for record in records:
            deadline = time.monotonic() + 5
            while True:
                try:
                    socket.create_connection(("127.0.0.1", record.port), timeout=0.1).close()
                    break
                except OSError:
                    assert time.monotonic() < deadline, f"3proxy on {record.port} did not listen"
                    time.sleep(0.05)
        leaked, codes = [], []
        for record in records:
            scheme = "http" if record.protocol == "http" else "socks5h"
            auth = f"{record.username}:{record.password}@" if record.username else ""
            result = subprocess.run(["curl", "-s", "-m", "5", "-w", "\\n%{http_code}", "--noproxy", "", "-x",
                                     f"{scheme}://{auth}127.0.0.1:{record.port}",
                                     f"http://[::1]:{server.server_port}/"], capture_output=True, timeout=10)
            body, _, code = result.stdout.rpartition(b"\n")
            codes.append(code.decode())
            if body == b"secret":
                leaked.append(f"{record.protocol}{'+auth' if auth else ''}:{record.port}")
        if block:
            assert not leaked, f"proxies relayed to host loopback: {leaked}"
            assert codes[:2] == ["403", "403"], codes  # http proxies answer 403 (deny ACL)
        else:
            assert len(leaked) == len(records), f"override did not open loopback: {codes}"
    finally:
        for process in processes:
            process.kill()
            process.wait()
        server.shutdown()
        server.server_close()
