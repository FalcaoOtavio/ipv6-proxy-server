import json
from types import SimpleNamespace

import pytest

from proxy_api.backend import LinuxBackend, atomic_json
from proxy_api.config import Settings


def test_recovery_removes_only_owned_addresses(tmp_path):
    settings = Settings(subnet="2001:db8::/64", bind_ipv4="127.0.0.1",
                        interface="eth0", api_token="x" * 32, data_dir=tmp_path)
    atomic_json(tmp_path / "owned-addresses.json", ["2001:db8::2", "2001:db8::3"])
    backend = LinuxBackend(settings)
    removed = []

    def ip(*args):
        if args[0] == "-j":
            return SimpleNamespace(stdout=json.dumps([{"addr_info": [
                {"local": "2001:db8::1"}, {"local": "2001:db8::2"}]}]))
        removed.append(args)

    backend._ip = ip
    backend.apply([])
    assert removed == [("-6", "addr", "del", "2001:db8::2/128", "dev", "eth0")]
    assert json.loads(backend.journal.read_text()) == []


def test_failed_cleanup_keeps_ownership_for_restart(tmp_path):
    settings = Settings(subnet="2001:db8::/64", bind_ipv4="127.0.0.1",
                        interface="eth0", api_token="x" * 32, data_dir=tmp_path)
    atomic_json(tmp_path / "owned-addresses.json", ["2001:db8::2"])
    backend = LinuxBackend(settings)
    backend.existing_addresses = lambda: {"2001:db8::2"}

    def fail(*args):
        raise RuntimeError("ip failed")

    backend._ip = fail
    with pytest.raises(RuntimeError, match="ip failed"):
        backend.apply([])
    assert json.loads(backend.journal.read_text()) == ["2001:db8::2"]
