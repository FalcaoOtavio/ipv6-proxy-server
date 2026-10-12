"""Real pseudo-terminal: the installed ipv6-proxy-tui opens, navigates and quits."""
import os
import sys
import time
from pathlib import Path

import pexpect

from test_tui import Api, TOKEN

ROOT = Path(__file__).resolve().parent.parent

def escape(child):
    """A lone Esc followed at once by a key is read as Alt+key: let it time out."""
    child.send("\x1b")
    time.sleep(0.5)

def test_tui_in_real_pty(tmp_path):
    api = Api(tmp_path)
    try:
        api.client.create({"port": 30000, "protocol": "socks5"})
        api.client.create({"port": 30001})
        env = {**os.environ, "PROXY_TUI_API_URL": api.url, "PROXY_API_TOKEN": TOKEN, "TERM": "xterm-256color",
               "PROXY_TUI_REFRESH": "0"}
        # The venv interpreter runs the entrypoint, as the installer wires it.
        child = pexpect.spawn(sys.executable, [str(ROOT / "ipv6-proxy-tui")], env=env, dimensions=(24, 80),
                              encoding="utf-8", timeout=20)
        try:
            child.expect("30001")
            child.expect("2 proxy")
            child.send("?")
            child.expect("Atalhos")
            escape(child)
            child.send("\x1b[B")  # arrow down
            child.send("d")
            child.expect("Digite 30001")
            escape(child)
            child.send("q")
            child.expect(pexpect.EOF)
        finally:
            child.close()
        assert child.exitstatus == 0, child.before
        assert set(api.records()) == {30000, 30001}
    finally:
        api.stop()

def test_installed_entrypoint_finds_app_and_venv_from_any_cwd(tmp_path):
    """T10: /usr/local/bin/ipv6-proxy-tui must find /opt/ipv6-proxy-server/proxy_tui and its venv."""
    import shutil
    root = tmp_path / "root"
    bin_dir, app_dir = root / "usr/local/bin", root / "opt/ipv6-proxy-server"
    bin_dir.mkdir(parents=True)
    shutil.copy(ROOT / "ipv6-proxy-tui", bin_dir / "ipv6-proxy-tui")
    shutil.copytree(ROOT / "proxy_tui", app_dir / "proxy_tui")
    # The installed venv: the only interpreter with Textual. A wrapper stands in for it.
    (app_dir / ".venv/bin").mkdir(parents=True)
    venv_python = app_dir / ".venv/bin/python"
    venv_python.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    venv_python.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    env.update(PROXY_API_TOKEN="short", TERM="xterm-256color", PROXY_TUI_REFRESH="0")
    # System python (no Textual) from an unrelated cwd, as sudo runs it.
    child = pexpect.spawn("/usr/bin/python3", ["-S", str(bin_dir / "ipv6-proxy-tui")], env=env,
                          cwd=str(tmp_path), dimensions=(24, 80), encoding="utf-8", timeout=20)
    try:
        child.expect("PROXY_API_TOKEN ausente")
        child.send("q")
        child.expect(pexpect.EOF)
    finally:
        child.close()
    assert child.exitstatus == 0, child.before

def test_entrypoint_without_app_fails_with_hint(tmp_path):
    import shutil
    import subprocess
    shutil.copy(ROOT / "ipv6-proxy-tui", tmp_path / "ipv6-proxy-tui")
    result = subprocess.run([sys.executable, str(tmp_path / "ipv6-proxy-tui")], capture_output=True, text=True,
                            cwd=str(ROOT), timeout=20)
    assert result.returncode == 1 and "install.sh --tui-only" in result.stderr
    assert "Traceback" not in result.stderr
