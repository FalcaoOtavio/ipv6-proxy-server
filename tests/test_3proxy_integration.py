"""Opt-in real IPv4-to-IPv6 forwarding test; needs 3proxy and curl."""
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proxy_api.backend import render_config
from proxy_api.config import Settings
from proxy_api.models import ProxyRecord


@pytest.mark.skipif(not os.environ.get("TEST_3PROXY_BINARY"), reason="Set TEST_3PROXY_BINARY for real integration")
def test_real_ipv4_to_ipv6_and_auth(tmp_path):
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = self.client_address[0].encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = Server(("::1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sockets = [socket.socket() for _ in range(2)]
    for item in sockets:
        item.bind(("127.0.0.1", 0))
    ports = [item.getsockname()[1] for item in sockets]
    for item in sockets:
        item.close()
    settings = Settings(subnet="::/126", bind_ipv4="127.0.0.1", interface="lo",
                        api_token="x" * 32, data_dir=tmp_path)
    records = [ProxyRecord(port=ports[0], protocol="http", username="alice", password="secret", ipv6="::1"),
               ProxyRecord(port=ports[1], protocol="socks5", username="bob", password="other", ipv6="::1")]
    processes = []
    for record in records:
        config = tmp_path / f"3proxy-{record.port}.cfg"
        config.write_text(render_config(settings, record))
        processes.append(subprocess.Popen([os.environ["TEST_3PROXY_BINARY"], str(config)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        for port in ports:
            deadline = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    assert time.monotonic() < deadline
                    assert all(process.poll() is None for process in processes)
                    time.sleep(0.05)
        for scheme, port, credentials in [("http", ports[0], "alice:secret"),
                                           ("socks5h", ports[1], "bob:other")]:
            command = ["curl", "--silent", "--show-error", "--fail", "--max-time", "5",
                       "--noproxy", "", "--proxy", f"{scheme}://127.0.0.1:{port}",
                       "--proxy-user", credentials, f"http://[::1]:{server.server_port}/"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            assert result.stdout == "::1"
            ipv4_command = command[:-1] + [f"http://127.0.0.1:{server.server_port}/"]
            assert subprocess.run(ipv4_command, capture_output=True, timeout=10).returncode != 0
            command[command.index(credentials)] = "bob:other" if scheme == "http" else "alice:secret"
            assert subprocess.run(command, capture_output=True, timeout=10).returncode != 0
    finally:
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
