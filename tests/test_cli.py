import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "ipv6-proxy"
loader = importlib.machinery.SourceFileLoader("ipv6_proxy_cli", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)
TOKEN = "t" * 32
PROXY = {
    "port": 30000,
    "protocol": "http",
    "ipv4": "127.0.0.1",
    "ipv6": "2001:db8::1",
    "username": "alice",
}


class Response:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.body[:size]


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "api.env"
    path.write_text(f"PROXY_API_TOKEN={TOKEN}\nPROXY_BIND_IPV4=127.0.0.1\n")
    path.chmod(0o600)
    return path


def invoke(env_file, argv, response=None, stdin="", error=None):
    stdout = io.StringIO()
    stderr = io.StringIO()
    opener = Opener(response=response, error=error)
    code = cli.main(
        argv,
        stdin=io.StringIO(stdin),
        stdout=stdout,
        stderr=stderr,
        env_file=env_file,
        expected_uid=os.getuid(),
        effective_uid=0,
        opener=opener,
    )
    return code, stdout.getvalue(), stderr.getvalue(), opener


def json_response(status, value):
    return Response(status, json.dumps(value).encode())


def test_help_works_without_root_or_configuration():
    result = subprocess.run([sys.executable, "-S", SCRIPT, "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "create" in result.stdout
    assert not result.stderr


def test_operational_commands_require_root(env_file):
    stderr = io.StringIO()
    code = cli.main(["list"], stderr=stderr, effective_uid=1000, env_file=env_file)
    assert code == 3
    assert "sudo" in stderr.getvalue()


def test_token_file_security(tmp_path, env_file):
    assert cli.load_token(env_file, os.getuid()) == TOKEN
    env_file.chmod(0o644)
    with pytest.raises(cli.CliError, match="0600"):
        cli.load_token(env_file, os.getuid())
    link = tmp_path / "link.env"
    link.symlink_to(env_file)
    with pytest.raises(cli.CliError):
        cli.load_token(link, os.getuid())


@pytest.mark.parametrize("contents", [
    "PROXY_API_TOKEN=short\n",
    f"PROXY_API_TOKEN={TOKEN}\nPROXY_API_TOKEN={TOKEN}\n",
    f"export PROXY_API_TOKEN={TOKEN}\n",
    f"PROXY_API_TOKEN={TOKEN}\ninvalid\n",
])
def test_rejects_malformed_token_file(tmp_path, contents):
    path = tmp_path / "bad.env"
    path.write_text(contents)
    path.chmod(0o600)
    with pytest.raises(cli.CliError):
        cli.load_token(path, os.getuid())


@pytest.mark.parametrize("comment", ["# Token used by the API", "; Token used by the API", "  # Note=ignored", "\t; Note", " \t"])
def test_systemd_environment_comments_are_ignored(env_file, comment):
    env_file.write_text(f"{comment}\nPROXY_API_TOKEN={TOKEN}\n")
    assert cli.load_token(env_file, os.getuid()) == TOKEN


def test_list_accepts_maximum_supported_pool(env_file):
    records = [
        {**PROXY, "port": port, "protocol": "socks5",
         "ipv4": "255.255.255.254", "ipv6": f"2001:db8:ffff:ffff:ffff:ffff:ffff:{port:x}",
         "username": "a" * 64}
        for port in range(1024, 65536)
    ]
    response = json_response(200, records)
    assert len(response.body) > 1024 * 1024
    code, stdout, stderr, _ = invoke(env_file, ["list", "--output", "json"], response)
    assert code == 0, stderr
    assert json.loads(stdout) == records


def test_list_json_sanitizes_server_fields(env_file):
    server_value = [{**PROXY, "password": "must-not-leak", "unknown": "ignored"}]
    code, stdout, stderr, opener = invoke(
        env_file, ["list", "--output", "json"], json_response(200, server_value)
    )
    assert code == 0
    assert json.loads(stdout) == [PROXY]
    assert "must-not-leak" not in stdout + stderr
    request, timeout = opener.requests[0]
    assert request.get_method() == "GET"
    assert request.full_url == "http://127.0.0.1:8000/proxies"
    assert request.get_header("Authorization") == "Bearer " + TOKEN
    assert timeout == 60


def test_create_sends_credentials_from_stdin_without_leaking(env_file):
    code, stdout, stderr, opener = invoke(
        env_file,
        ["create", "--port", "30000", "--protocol", "http", "--username", "alice",
         "--password-stdin", "--yes", "--output", "json"],
        json_response(201, PROXY),
        stdin="secret-123\n",
    )
    assert code == 0
    assert json.loads(stdout) == PROXY
    request, _ = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url.endswith("/proxies")
    assert json.loads(request.data) == {
        "port": 30000, "protocol": "http", "username": "alice", "password": "secret-123"
    }
    assert "secret-123" not in stdout + stderr
    assert cli.MUTATION_WARNING in stderr


def test_rotate_and_delete_contract(env_file):
    code, stdout, _, opener = invoke(
        env_file, ["rotate", "30000", "--yes", "--output", "json"], json_response(200, PROXY)
    )
    assert code == 0 and json.loads(stdout) == PROXY
    assert opener.requests[0][0].get_method() == "POST"
    assert opener.requests[0][0].data is None
    assert opener.requests[0][0].full_url.endswith("/proxies/30000/rotate")

    code, stdout, _, opener = invoke(
        env_file, ["delete", "30000", "--yes", "--output", "json"], Response(204)
    )
    assert code == 0
    assert json.loads(stdout) == {"deleted": True, "port": 30000}
    assert opener.requests[0][0].get_method() == "DELETE"
    assert opener.requests[0][0].data is None


def test_mutation_requires_confirmation_without_tty(env_file):
    code, stdout, stderr, opener = invoke(env_file, ["create"], json_response(201, PROXY))
    assert code == 2
    assert not stdout
    assert "--yes" in stderr
    assert not opener.requests


def test_http_errors_have_stable_exit_codes_without_token_leak(env_file):
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8000/proxies", 409, "Conflict", {},
        io.BytesIO(b'{"detail":"Port unavailable"}'),
    )
    code, stdout, stderr, _ = invoke(env_file, ["create", "--yes"], error=error)
    assert code == 4
    assert not stdout
    assert "HTTP 409" in stderr
    assert TOKEN not in stderr


def test_validation_error_array_is_not_rendered(env_file):
    body = json.dumps({"detail": [{"loc": ["body", "port"], "msg": "bad port"}]}).encode()
    error = urllib.error.HTTPError("http://127.0.0.1:8000/proxies", 422, "", {}, io.BytesIO(body))
    code, _, stderr, _ = invoke(env_file, ["create", "--yes"], error=error)
    assert code == 4
    assert "HTTP 422" in stderr
    assert "bad port" not in stderr


def test_transport_failure_is_not_retried_and_warns_of_unknown_result(env_file):
    code, stdout, stderr, opener = invoke(
        env_file, ["rotate", "30000", "--yes"], error=urllib.error.URLError("refused")
    )
    assert code == 6
    assert not stdout
    assert len(opener.requests) == 1
    assert "resultado pode ser desconhecido" in stderr


def test_invalid_success_payload_is_rejected(env_file):
    code, stdout, stderr, _ = invoke(env_file, ["list"], json_response(200, [{"password": "x"}]))
    assert code == 5
    assert not stdout
    assert "incompleto" in stderr


def test_cli_is_only_an_api_client():
    source = SCRIPT.read_text()
    for forbidden in ("proxy_api", "systemctl", "subprocess", "ip -6", "3proxy.cfg", "manager.lock"):
        assert forbidden not in source


@pytest.mark.parametrize("field,value", [
    ("protocol", []), ("protocol", {}), ("protocol", None),
    ("port", True), ("port", "30000"), ("port", 80),
    ("username", []), ("username", "alice\x1b[2J"), ("username", "x" * 65),
    ("ipv4", []), ("ipv4", 1), ("ipv4", None), ("ipv4", "not-an-ip"),
    ("ipv6", "fe80::1%evil\x1b[2J"), ("ipv6", "fe80::1%eth0"), ("ipv6", {}), ("ipv6", True), ("ipv6", "not-an-ip"),
])
def test_malformed_proxy_fields_fail_without_traceback(env_file, field, value):
    code, stdout, stderr, _ = invoke(
        env_file, ["list"], json_response(200, [{**PROXY, field: value}])
    )
    assert code == 5 and not stdout
    assert "Traceback" not in stderr

@pytest.mark.parametrize("status,expected", [(301, 5), (401, 3), (404, 4), (409, 4), (422, 4), (500, 5), (503, 5)])
@pytest.mark.parametrize("detail", [
    "reflected-secret", [{"loc": ["body", "reflected-secret"], "msg": "reflected-secret", "input": "reflected-secret"}],
    [{"loc": None, "msg": "reflected-secret"}], [{"loc": 123, "msg": "reflected-secret"}],
    {"password": "reflected-secret"}, None,
])
def test_error_bodies_never_reflect_secrets(env_file, status, expected, detail):
    body = json.dumps({"detail": detail, "token": TOKEN}).encode()
    error = urllib.error.HTTPError(cli.API_URL, status, "reflected-secret", {}, io.BytesIO(body))
    code, stdout, stderr, _ = invoke(env_file, ["create", "--yes"], error=error)
    assert code == expected and not stdout
    assert f"HTTP {status}" in stderr
    assert "reflected-secret" not in stderr and TOKEN not in stderr

@pytest.mark.parametrize("body", [b"no json", b"\xff", b"[" * 2000 + b"]" * 2000, b"x" * (cli.MAX_BODY + 1), b"9" * 5000])
def test_bad_json_and_size_fail_closed(env_file, body):
    code, stdout, _, _ = invoke(env_file, ["list"], Response(200, body))
    assert code == 5 and not stdout

@pytest.mark.parametrize("args,response", [
    (["list"], json_response(200, {})), (["list"], json_response(200, [None])),
    (["create", "--yes"], json_response(200, PROXY)),
    (["delete", "30000", "--yes"], Response(204, b"unexpected")),
])
def test_unexpected_success_contract(env_file, args, response):
    assert invoke(env_file, args, response)[0] == 5

@pytest.mark.parametrize("args,password", [
    (["--password-stdin", "--yes"], "secret\n"),
    (["--username", "alice", "--password-stdin"], "secret\n"),
    (["--username", "alice", "--password-stdin", "--yes"], "secret"),
    (["--username", "alice", "--password-stdin", "--yes"], "one\ntwo\n"),
    (["--username", "alice", "--password-stdin", "--yes"], "x" * 129 + "\n"),
    (["--username", "alice", "--password-stdin", "--yes"], "bad$secret\n"),
    (["--username", "bad user", "--yes"], ""),
    (["--username", "alice", "--yes"], "secret\n"),
])
def test_invalid_credentials_do_not_send_requests(env_file, args, password):
    code, stdout, _, opener = invoke(env_file, ["create", *args], stdin=password)
    assert code == 2 and not stdout and not opener.requests

class Tty(io.StringIO):
    def isatty(self):
        return True

@pytest.mark.parametrize("failure", ["warning", "eof", "oserror"])
def test_getpass_never_falls_back_to_echo(env_file, monkeypatch, failure):
    import warnings
    def unavailable(*args):
        if failure == "warning":
            warnings.warn("Password input may be echoed", cli.getpass.GetPassWarning)
            pytest.fail("unsafe fallback must never be reached")
        raise EOFError() if failure == "eof" else OSError()
    monkeypatch.setattr(cli.getpass, "getpass", unavailable)
    stderr = io.StringIO()
    opener = Opener()
    code = cli.main(["create", "--username", "alice", "--yes"], stdin=Tty(),
                    stderr=stderr, env_file=env_file, expected_uid=os.getuid(),
                    effective_uid=0, opener=opener)
    assert code == 2 and not opener.requests

@pytest.mark.parametrize("answer,expected", [("y\n", 0), ("N\n", 2), ("", 2)])
def test_interactive_confirmation(env_file, monkeypatch, answer, expected):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *args: "secret")
    opener = Opener(json_response(201, PROXY))
    code = cli.main(["create", "--username", "alice"], stdin=Tty(answer),
                    stdout=io.StringIO(), stderr=io.StringIO(), env_file=env_file,
                    expected_uid=os.getuid(), effective_uid=0, opener=opener)
    assert code == expected
    assert len(opener.requests) == (1 if expected == 0 else 0)

def test_configuration_is_bounded_and_not_executed(tmp_path, env_file):
    marker = tmp_path / "must-not-exist"
    env_file.write_text(f"PROXY_API_TOKEN={TOKEN}\nOTHER=$(touch${{IFS}}{marker})\n")
    assert cli.load_token(env_file, os.getuid()) == TOKEN
    assert not marker.exists()
    env_file.write_bytes(b"x" * (cli.MAX_ENV + 1))
    with pytest.raises(cli.CliError, match="excede"):
        cli.load_token(env_file, os.getuid())
    with pytest.raises(cli.CliError):
        cli.load_token(tmp_path, os.getuid())
    with pytest.raises(cli.CliError):
        cli.load_token(tmp_path / "missing", os.getuid())

def test_configuration_wrong_owner_and_read_failure(env_file, monkeypatch):
    with pytest.raises(cli.CliError, match="root"):
        cli.load_token(env_file, os.getuid() + 1)
    def fail(*args):
        raise OSError("internal-detail")
    monkeypatch.setattr(cli.os, "read", fail)
    with pytest.raises(cli.CliError, match="Falha ao ler"):
        cli.load_token(env_file, os.getuid())

def test_fifo_configuration_does_not_block(tmp_path):
    fifo = tmp_path / "config.fifo"
    os.mkfifo(fifo, 0o600)
    runner = """import os, runpy, sys
c = runpy.run_path(sys.argv[1])
try:
    c['load_token'](sys.argv[2], os.getuid())
except c['CliError']:
    sys.exit(3)
"""
    result = subprocess.run([sys.executable, "-c", runner, str(SCRIPT), str(fifo)],
                            capture_output=True, timeout=5)
    assert result.returncode == 3 and not result.stderr

def test_incomplete_http_response_is_transport_failure(env_file):
    import http.client
    code, stdout, stderr, _ = invoke(env_file, ["create", "--yes"],
                                    error=http.client.IncompleteRead(b"secret"))
    assert code == 6 and not stdout
    assert "secret" not in stderr and "desconhecido" in stderr

@pytest.fixture
def live_api(tmp_path):
    """One real HTTP listener/manager shared by CLI processes and HTTP clients."""
    import socket
    import threading
    import time
    import uvicorn
    from proxy_api.app import create_app
    from proxy_api.config import Settings

    class Backend:
        def __init__(self, settings):
            self.records = []
            self.applying = False

        def apply(self, records):
            assert not self.applying, "backend mutations must be serialized"
            self.applying = True
            try:
                time.sleep(0.005)  # Give overlapping requests a chance to collide.
                self.records = list(records)
            finally:
                self.applying = False

        def existing_addresses(self):
            return {str(r.ipv6) for r in self.records}

        def healthy(self, records):
            return self.records == records

        def close(self):
            pass

    settings = Settings(subnet="2001:db8::/64", bind_ipv4="127.0.0.1", interface="lo",
                        api_token=TOKEN, data_dir=tmp_path / "state")
    app = create_app(settings, Backend)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="none"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline, "API did not start"
                time.sleep(0.01)
            yield url, app, settings
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "API did not stop"


def process_cli(env_file, url, *args):
    # Test-only injection: production CLI always uses root + the fixed loopback URL.
    runner = """import os, runpy, sys
c = runpy.run_path(sys.argv[1])
m = c['main']
m.__globals__['API_URL'] = sys.argv[3]
sys.exit(m(sys.argv[4:], env_file=sys.argv[2], expected_uid=os.getuid(), effective_uid=0))
"""
    return subprocess.run([sys.executable, "-c", runner, str(SCRIPT), str(env_file), url,
                           *args], capture_output=True, text=True, timeout=20)


def test_live_http_cli_share_serialized_state(live_api, env_file):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import httpx
    url, app, settings = live_api
    with httpx.Client(base_url=url, headers={"Authorization": "Bearer " + TOKEN},
                      trust_env=False, timeout=20) as http:
        barrier = threading.Barrier(12)

        def create(index):
            barrier.wait(timeout=10)
            if index % 2:
                response = http.post("/proxies", json={"protocol": "socks5"})
                assert response.status_code == 201
                return response.json()
            result = process_cli(env_file, url, "create", "--yes", "--output", "json")
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        with ThreadPoolExecutor(max_workers=12) as pool:
            records = list(pool.map(create, range(12)))
        assert len({r["port"] for r in records}) == 12
        assert len({r["ipv6"] for r in records}) == 12
        listed = http.get("/proxies").json()
        result = process_cli(env_file, url, "list", "--output", "json")
        assert result.returncode == 0 and json.loads(result.stdout) == listed
        assert {r["port"] for r in listed} == {r["port"] for r in records}

        # Both clients mutate different records concurrently, without losing peers.
        barrier = threading.Barrier(12)
        def mutate(index):
            barrier.wait(timeout=10)
            port = records[index]["port"]
            if index % 2:
                assert http.delete(f"/proxies/{port}").status_code == 204
                return None
            result = process_cli(env_file, url, "rotate", str(port), "--yes", "--output", "json")
            assert result.returncode == 0, result.stderr
            rotated = json.loads(result.stdout)
            assert rotated["ipv6"] != records[index]["ipv6"]
            assert {k: v for k, v in rotated.items() if k != "ipv6"} == {
                k: v for k, v in records[index].items() if k != "ipv6"}
            return rotated
        with ThreadPoolExecutor(max_workers=12) as pool:
            survivors = [r for r in pool.map(mutate, range(12)) if r]
        listed = http.get("/proxies").json()
        assert sorted(listed, key=lambda r: r["port"]) == sorted(survivors, key=lambda r: r["port"])
        persisted = json.loads((settings.data_dir / "proxies.json").read_text())
        backend = [r.model_dump(mode="json") for r in app.state.manager.backend.records]
        assert persisted == backend
        assert [{**{k: v for k, v in r.items() if k != "password"}, "ipv4": "127.0.0.1"}
                for r in persisted] == listed


def test_live_http_cli_same_port_conflict(live_api, env_file):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import httpx
    url, _, _ = live_api
    barrier = threading.Barrier(2)
    def cli_create():
        barrier.wait(timeout=10)
        return process_cli(env_file, url, "create", "--port", "31000", "--yes", "--output", "json")
    with httpx.Client(base_url=url, headers={"Authorization": "Bearer " + TOKEN}, trust_env=False) as http:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(cli_create)
            barrier.wait(timeout=10)
            response = http.post("/proxies", json={"port": 31000})
            result = pending.result(timeout=20)
        assert (response.status_code, result.returncode) in {(201, 4), (409, 0)}
        if result.returncode:
            assert not result.stdout and "HTTP 409" in result.stderr
        listed = http.get("/proxies").json()
        assert len(listed) == 1 and listed[0]["port"] == 31000


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_transport_ignores_environment_proxies_and_refuses_redirects(env_file, monkeypatch, status):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            if self.path == "/proxies":
                self.send_response(status)
                self.send_header("Location", "/credential-sink")
            else:
                self.send_response(200)
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setattr(cli, "API_URL", f"http://127.0.0.1:{server.server_port}")
            for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
                monkeypatch.setenv(key, "http://127.0.0.1:1")
            for key in ("no_proxy", "NO_PROXY"):
                monkeypatch.setenv(key, "")
            stderr = io.StringIO()
            code = cli.main(["list"], env_file=env_file, expected_uid=os.getuid(),
                            effective_uid=0, stdout=io.StringIO(), stderr=stderr)
            assert code == 5 and f"HTTP {status}" in stderr.getvalue()
            assert requests == [("/proxies", "Bearer " + TOKEN)]
        finally:
            server.shutdown()
            thread.join(timeout=5)
