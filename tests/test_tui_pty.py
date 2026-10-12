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
