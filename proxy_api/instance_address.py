"""Restore one proxy instance's own IPv6 before its 3proxy starts.

Run by ipv6-proxy@<port>.service as `ExecStartPre=+... <port>`: after a reboot
the /128 addresses are gone and units start without the API, so each instance
restores exactly its own address. It only adds an address recorded in the
API's ownership journal; an address it does not own is refused (the unit then
fails instead of egressing from someone else's IP). Idempotent: an address
already present is left alone. Never removes anything; stop/shutdown keep the
address, which the API releases on delete. Stdlib only, runs as a script.
"""
import json
import re
import subprocess
import sys
from ipaddress import IPv6Address
from pathlib import Path

DATA_DIR = Path("/var/lib/ipv6-proxy-api")
INTERFACE_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,15}$")

def address_path(config_dir, port):
    return Path(config_dir) / f"{int(port)}.addr"

def run_ip(*args):
    return subprocess.run(["ip", *args], check=True, capture_output=True, text=True, timeout=15)

def ensure(port, data_dir=DATA_DIR, ip=run_ip):
    """Return 'present' or 'added'; raise on anything not ours or malformed."""
    spec = json.loads(address_path(Path(data_dir) / "proxies", port).read_text())
    address, interface = str(IPv6Address(spec["ipv6"])), spec["interface"]
    if not INTERFACE_PATTERN.fullmatch(interface):
        raise ValueError(f"invalid interface {interface!r}")
    owned = {str(IPv6Address(item)) for item in json.loads((Path(data_dir) / "owned-addresses.json").read_text())}
    if address not in owned:
        raise PermissionError(f"{address} is not in the ownership journal; refusing to add it")
    shown = json.loads(ip("-j", "-6", "addr", "show", "dev", interface).stdout)
    if address in {entry["local"] for item in shown for entry in item.get("addr_info", [])}:
        return "present"
    ip("-6", "addr", "add", f"{address}/128", "dev", interface, "nodad")
    return "added"

def main(argv):
    if len(argv) != 2 or not argv[1].isdigit():
        print("usage: instance_address.py <port>", file=sys.stderr)
        return 2
    try:
        print(f"ipv6-proxy@{argv[1]}: address {ensure(int(argv[1]))}")
    except Exception as error:
        print(f"ipv6-proxy@{argv[1]}: {error}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
