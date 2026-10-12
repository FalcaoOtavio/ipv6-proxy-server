"""CLI batch/edit/export against a real in-process API (uvicorn) with an in-memory backend."""
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "ipv6-proxy"
TOKEN = "t" * 32
LINE_RUNNER = """import os, runpy, sys
c = runpy.run_path(sys.argv[1])
m = c['main']
m.__globals__['API_URL'] = sys.argv[3]
sys.exit(m(sys.argv[4:], env_file=sys.argv[2], expected_uid=os.getuid(), effective_uid=0))
"""

class Backend:
    def __init__(self, settings):
        self.records = {}

    def reconcile(self, records):
        self.records = {r.port: r for r in records}
        return set()

    def provision(self, record):
        self.records[record.port] = record

    def replace(self, old, new):
        del self.records[old.port]
        self.records[new.port] = new

    def deprovision(self, record):
        del self.records[record.port]

    def existing_addresses(self):
        return {str(r.ipv6) for r in self.records.values()}

    def healthy(self, records):
        return all(self.records.get(r.port) == r for r in records)

    def statuses(self, records):
        return {r.port: "running" for r in records}

    def close(self):
        pass

@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "api.env"
    path.write_text(f"PROXY_API_TOKEN={TOKEN}\n")
    path.chmod(0o600)
    return path

@pytest.fixture
def api(tmp_path, caplog):
    import uvicorn
    from proxy_api.app import create_app
    from proxy_api.config import Settings

    settings = Settings(subnet="2001:db8::/64", bind_ipv4="127.0.0.1", interface="lo",
                        api_token=TOKEN, data_dir=tmp_path / "state")
    app = create_app(settings, Backend)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="info", ws="none"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline, "API did not start"
                time.sleep(0.01)
            yield url, app
        finally:
            server.should_exit = True
            thread.join(timeout=10)

def run(env_file, url, *args, stdin=""):
    return subprocess.run([sys.executable, "-c", LINE_RUNNER, str(SCRIPT), str(env_file), url, *args],
                          input=stdin, capture_output=True, text=True, timeout=60)

def stored(app):
    return {r.port: r for r in app.state.manager.records}

def assert_private(path):
    mode = path.lstat().st_mode
    assert stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600

def test_batch_100_http_random_exports_one_time_passwords(api, env_file, tmp_path, caplog):
    url, app = api
    out = tmp_path / "http.list"
    result = run(env_file, url, "batch", "-c", "100", "--start-port", "30000", "-t", "http",
                 "--random", "-u", "cli", "-f", str(out), "--yes")
    assert result.returncode == 0, result.stderr
    assert "Criados: 100; falhas: 0." in result.stderr
    assert_private(out)
    lines = out.read_text().splitlines()
    assert len(lines) == 100
    records = stored(app)
    passwords = set()
    for line in lines:
        ipv4, port, user, password = line.split(":")
        record = records[int(port)]
        assert (ipv4, user, password) == ("127.0.0.1", f"cli{port}", record.password)
        assert record.protocol == "http"
        passwords.add(password)
    assert len(passwords) == 100
    leaked = result.stdout + result.stderr + caplog.text
    assert not any(p in leaked for p in passwords)

def test_batch_100_socks5_common_password_from_stdin(api, env_file, tmp_path, caplog):
    url, app = api
    secret = "Common-pass.123"
    result = run(env_file, url, "batch", "-c", "100", "-t", "socks5", "-u", "team",
                 "--password-stdin", "--yes", "--format", "json", stdin=secret + "\n")
    assert result.returncode == 0, result.stderr
    exported = json.loads(result.stdout)
    assert len(exported) == 100 and len({e["ipv6"] for e in exported}) == 100
    assert {(e["protocol"], e["username"], e["password"]) for e in exported} == {("socks5", "team", secret)}
    assert all(r.password == secret for r in stored(app).values())
    assert secret not in result.stderr + caplog.text

    # export: parseable, existing passwords are not available from the API.
    out = tmp_path / "all.list"
    result = run(env_file, url, "export", "-f", str(out))
    assert result.returncode == 0, result.stderr
    assert_private(out)
    rows = [line.split(":") for line in out.read_text().splitlines()]
    assert len(rows) == 100 and all(len(r) == 3 and r[2] == "team" for r in rows)
    assert secret not in out.read_text()
    assert "sem senha" in result.stderr

def test_batch_without_auth_lists_ipv4_port(api, env_file):
    url, _ = api
    result = run(env_file, url, "batch", "-c", "3", "--no-auth", "--yes")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["127.0.0.1:30000", "127.0.0.1:30001", "127.0.0.1:30002"]

def test_output_file_is_never_clobbered_or_followed(api, env_file, tmp_path):
    url, app = api
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "link.list"
    link.symlink_to(victim)
    for target in (victim, link):
        result = run(env_file, url, "batch", "-c", "1", "--no-auth", "--yes", "-f", str(target))
        assert result.returncode == 2 and "--force" in result.stderr
    assert victim.read_text() == "keep" and not stored(app)  # checked before calling the API

    result = run(env_file, url, "batch", "-c", "1", "--random", "--yes", "-f", str(link), "--force")
    assert result.returncode == 0, result.stderr
    assert victim.read_text() == "keep"  # --force replaces the link itself
    assert_private(link)
    assert link.read_text().count(":") == 3

@pytest.mark.parametrize("args,stdin", [
    (["-c", "0", "--no-auth"], ""),
    (["-c", "70000", "--no-auth"], ""),
    (["-c", "2", "--start-port", "80", "--no-auth"], ""),
    (["-c", "2"], ""),
    (["--no-auth"], ""),
    (["-c", "2", "--random", "--no-auth"], ""),
    (["-c", "2", "--no-auth", "-u", "x"], ""),
    (["-c", "2", "--random", "--password-stdin"], "x\n"),
    (["-c", "2", "-u", "bad user", "--password-stdin"], "x\n"),
    (["-c", "2", "-u", "a" * 59, "--password-stdin"], "x\n"),
    (["-c", "2", "-u", "team", "--password-stdin"], "bad$pass\n"),
    (["-c", "2", "-u", "team"], ""),
])
def test_validation_errors_happen_before_the_api(api, env_file, args, stdin):
    url, app = api
    result = run(env_file, url, "batch", *args, "--yes", stdin=stdin)
    assert result.returncode == 2 and not result.stdout
    assert not stored(app)
    assert "bad$pass" not in result.stderr

def test_partial_failure_reports_ports_and_exit_code(api, env_file):
    url, app = api
    backend = app.state.manager.backend
    original = backend.provision
    def flaky(record):
        if record.port == 30001:
            raise RuntimeError("boom")
        original(record)
    backend.provision = flaky
    result = run(env_file, url, "batch", "-c", "3", "--no-auth", "--partial", "--yes")
    assert result.returncode == 7
    assert result.stdout.splitlines() == ["127.0.0.1:30000", "127.0.0.1:30002"]
    assert "Criados: 2; falhas: 1." in result.stderr and "30001" in result.stderr

def test_all_or_nothing_failure_writes_no_file(api, env_file, tmp_path):
    url, app = api
    app.state.manager.backend.provision = lambda record: (_ for _ in ()).throw(RuntimeError("boom"))
    out = tmp_path / "x.list"
    result = run(env_file, url, "batch", "-c", "2", "--random", "--yes", "-f", str(out))
    assert result.returncode == 5 and "HTTP 503" in result.stderr
    assert not out.exists() and not stored(app)

def test_edit_uses_if_match_and_preserves_other_fields(api, env_file):
    url, app = api
    assert run(env_file, url, "batch", "-c", "2", "--no-auth", "--yes").returncode == 0
    before = stored(app)[30000]
    result = run(env_file, url, "edit", "30000", "--new-port", "30500", "-t", "socks5", "-u", "neo",
                 "--password-stdin", "--yes", "--output", "json", stdin="Red-pill1\n")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["port"] == 30500
    after = stored(app)[30500]
    assert (after.id, after.ipv6, after.protocol, after.username, after.password) == (
        before.id, before.ipv6, "socks5", "neo", "Red-pill1")
    assert "Red-pill1" not in result.stdout + result.stderr
    assert 30001 in stored(app)

    result = run(env_file, url, "edit", "30500", "--no-auth", "--yes")
    assert result.returncode == 0, result.stderr
    assert stored(app)[30500].username is None

def test_edit_detects_concurrent_replacement(api, env_file, monkeypatch):
    """A port deleted and recreated between GET and PATCH answers 412, the heir is untouched."""
    import importlib.machinery, importlib.util, io
    url, app = api
    assert run(env_file, url, "batch", "-c", "1", "--no-auth", "--yes").returncode == 0
    loader = importlib.machinery.SourceFileLoader("cli_mod", str(SCRIPT))
    cli = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(cli)
    monkeypatch.setattr(cli, "API_URL", url)
    real_request = cli.request
    def racing(method, path, *args, **kwargs):
        result = real_request(method, path, *args, **kwargs)
        if method == "GET":
            real_request("DELETE", "/proxies/30000", TOKEN)
            real_request("POST", "/proxies", TOKEN, {"port": 30000})
        return result
    monkeypatch.setattr(cli, "request", racing)
    stderr = io.StringIO()
    code = cli.main(["edit", "30000", "-t", "socks5", "--yes"], stdout=io.StringIO(), stderr=stderr,
                    env_file=env_file, expected_uid=os.getuid(), effective_uid=0)
    heir = stored(app)[30000]
    assert code == 4 and "HTTP 412" in stderr.getvalue()
    assert heir.protocol == "http"

def test_edit_requires_a_change(api, env_file):
    url, _ = api
    result = run(env_file, url, "edit", "30000", "--yes")
    assert result.returncode == 2 and "Nada a alterar" in result.stderr

# pexpect forks from a process with the API thread running; the child execs at once.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_interactive_batch_over_pty(api, env_file, tmp_path):
    pexpect = pytest.importorskip("pexpect")
    url, app = api
    out = tmp_path / "pty.list"
    child = pexpect.spawn(sys.executable, ["-c", LINE_RUNNER, str(SCRIPT), str(env_file), url, "batch"],
                          encoding="utf-8", timeout=20)
    try:
        child.expect("Quantidade de proxies")
        child.sendline("abc")
        child.expect("Valor invalido")
        child.expect("Quantidade de proxies")
        child.sendline("5")
        child.expect("Porta inicial")
        child.sendline("31000")
        child.expect("Protocolo")
        child.sendline("socks5")
        child.expect("Credenciais")
        child.sendline("comum")
        child.expect("Usuario")
        child.sendline("ops")
        child.expect("Arquivo de saida")
        child.sendline(str(out))
        child.expect("Senha do proxy")
        child.sendline("Hidden-pw9")
        child.expect(r"Continuar\? \[y/N\]")
        transcript = child.before
        child.sendline("s")
        child.expect(pexpect.EOF)
        transcript += child.before
    finally:
        child.close()
    assert child.exitstatus == 0, transcript
    assert "Hidden-pw9" not in transcript  # getpass: no echo
    assert_private(out)
    assert out.read_text().splitlines()[0] == "127.0.0.1:31000:ops:Hidden-pw9"
    assert {r.protocol for r in stored(app).values()} == {"socks5"} and len(stored(app)) == 5

def test_no_command_accepts_a_password_argument():
    for command in ("create", "batch", "edit"):
        help_text = subprocess.run([sys.executable, str(SCRIPT), command, "--help"],
                                   capture_output=True, text=True).stdout
        assert "--password-stdin" in help_text
        assert "--password " not in help_text and " -p " not in help_text

def test_abbreviated_password_flag_is_rejected_without_echo():
    result = subprocess.run([sys.executable, str(SCRIPT), "batch", "--password", "S3cret-x"],
                            capture_output=True, text=True)
    assert result.returncode == 2 and "S3cret-x" not in result.stdout
    assert "S3cret-x" not in result.stderr and "nao reconhecidos" in result.stderr

def test_batch_retry_with_same_key_does_not_duplicate(api, env_file):
    """A7: rerun after a lost response replays the stored result; random passwords are not recoverable."""
    url, app = api
    args = ("batch", "-c", "3", "--random", "--yes", "--idempotency-key", "retry-1")
    first = run(env_file, url, *args)
    assert first.returncode == 0, first.stderr
    again = run(env_file, url, *args)
    assert again.returncode == 0, again.stderr
    assert len(stored(app)) == 3
    assert [line.split(":")[:3] for line in again.stdout.splitlines()] == \
        [line.split(":")[:3] for line in first.stdout.splitlines()]
    assert "nao sao recuperaveis" in again.stderr
    assert not any(r.password in again.stdout + again.stderr for r in stored(app).values())

def test_batch_count_follows_proxy_batch_max(api, env_file):
    url, app = api
    env_file.write_text(f"PROXY_API_TOKEN={TOKEN}\nPROXY_BATCH_MAX=5\n")
    result = run(env_file, url, "batch", "-c", "6", "--no-auth", "--yes")
    assert result.returncode == 2 and "PROXY_BATCH_MAX=5" in result.stderr
    assert not stored(app)
    # Default (1000) when the env file does not set it; the API is the final judge.
    env_file.write_text(f"PROXY_API_TOKEN={TOKEN}\n")
    result = run(env_file, url, "batch", "-c", "1001", "--no-auth", "--yes")
    assert result.returncode == 2 and "PROXY_BATCH_MAX=1000" in result.stderr

def test_list_and_export_survive_a_failed_proxy(api, env_file):
    """A1: legacy GET /proxies answers 503 when one proxy is down; the CLI must still list all."""
    url, app = api
    assert run(env_file, url, "batch", "-c", "2", "--no-auth", "--yes").returncode == 0
    backend = app.state.manager.backend
    backend.healthy = lambda records: False
    backend.statuses = lambda records: {r.port: "failed" if r.port == 30001 else "running" for r in records}
    listed = run(env_file, url, "list")
    assert listed.returncode == 0, listed.stderr
    assert "failed" in listed.stdout and "30001" in listed.stderr
    exported = run(env_file, url, "export")
    assert exported.returncode == 0
    assert exported.stdout.splitlines() == ["127.0.0.1:30000", "127.0.0.1:30001"]
