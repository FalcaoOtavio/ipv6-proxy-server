import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from proxy_api.instance_address import ensure

def layout(tmp_path, port, ipv6, owned, interface="eth0"):
    (tmp_path / "proxies").mkdir(exist_ok=True)
    (tmp_path / "proxies" / f"{port}.addr").write_text(json.dumps({"ipv6": ipv6, "interface": interface}))
    (tmp_path / "owned-addresses.json").write_text(json.dumps(owned))

class Ip:
    def __init__(self, present=()):
        self.present = set(present)
        self.calls = []

    def __call__(self, *args):
        if args[0] == "-j":
            return SimpleNamespace(stdout=json.dumps([{"addr_info": [{"local": a} for a in self.present]}]))
        self.calls.append(args)
        self.present.add(args[3].split("/")[0])

def test_adds_only_its_own_address_and_is_idempotent(tmp_path):
    layout(tmp_path, 30000, "2001:db8::a", ["2001:db8::a", "2001:db8::b"])
    ip = Ip(present={"2001:db8::1"})
    assert ensure(30000, tmp_path, ip) == "added"
    assert ip.calls == [("-6", "addr", "add", "2001:db8::a/128", "dev", "eth0", "nodad")]
    assert ensure(30000, tmp_path, ip) == "present"
    assert len(ip.calls) == 1  # second run: no command; never touches ::b of another instance

def test_refuses_address_not_in_journal(tmp_path):
    layout(tmp_path, 30000, "2001:db8::f", ["2001:db8::a"])
    ip = Ip()
    with pytest.raises(PermissionError, match="ownership journal"):
        ensure(30000, tmp_path, ip)
    assert ip.calls == []

def test_refuses_malformed_input(tmp_path):
    layout(tmp_path, 30000, "2001:db8::a", ["2001:db8::a"], interface="eth0;reboot")
    with pytest.raises(ValueError):
        ensure(30000, tmp_path, Ip())
    with pytest.raises(FileNotFoundError):
        ensure(30001, tmp_path, Ip())

def test_script_rejects_bad_arguments():
    script = Path(__file__).resolve().parent.parent / "proxy_api" / "instance_address.py"
    for argv in ([], ["*"], ["1", "2"]):
        result = subprocess.run([sys.executable, str(script), *argv], capture_output=True, text=True)
        assert result.returncode == 2
