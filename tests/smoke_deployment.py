"""Run explicitly on the disposable CI VM after `install.sh` (needs root and systemd).

    sudo python3 tests/smoke_deployment.py "$PWD/install.sh"

Checks the final layout: one ipv6-proxy@<port>.service per proxy, none of them
inside the API's cgroup; CLI create/list/rotate/edit/batch/delete; API restart
and installer re-runs (`install.sh`, `--update`, `--cli-only`) leave env, state,
units and proxy PIDs untouched.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CLI = "/usr/local/bin/ipv6-proxy"
DATA_DIR = Path("/var/lib/ipv6-proxy-api")
PRESERVED = [Path("/etc/ipv6-proxy-api.env"), DATA_DIR / "state.json",
             Path("/etc/systemd/system/ipv6-proxy-api.service"),
             Path("/etc/systemd/system/ipv6-proxy@.service")]
BATCH_START = 30100


def command(*arguments, stdin=None, check=True):
    return subprocess.run([CLI, *arguments], input=stdin, text=True, capture_output=True, timeout=90, check=check)


def json_command(*arguments, stdin=None):
    return json.loads(command(*arguments, "--output", "json", stdin=stdin).stdout)


def systemctl(*arguments):
    return subprocess.run(["systemctl", *arguments], text=True, capture_output=True, timeout=60)


def main_pid(unit):
    return int(systemctl("show", "--property=MainPID", "--value", unit).stdout.strip())


def snapshot():
    """Content/mode/owner of env, state, units and every per-proxy config."""
    paths = PRESERVED + sorted((DATA_DIR / "proxies").glob("*.cfg"))
    return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode, p.stat().st_uid)
            for p in paths}


def proxy_pids():
    """{port: MainPID} of every active or failed ipv6-proxy@<port>; each must be running.

    No --all: units stopped by a delete are inactive and must not be listed."""
    units = systemctl("list-units", "--plain", "--no-legend", "ipv6-proxy@*.service").stdout.split()
    pids = {}
    for unit in (u for u in units if u.startswith("ipv6-proxy@")):
        port = int(unit.split("@")[1].split(".")[0])
        pids[port] = main_pid(unit)
        assert pids[port] > 0, f"{unit} has no running process"
    return pids


def pids():
    return {"api": main_pid("ipv6-proxy-api"), **proxy_pids()}


def wait_api():
    for attempt in range(30):
        try:
            return json_command("list")
        except subprocess.CalledProcessError:
            if attempt == 29:
                raise
            time.sleep(1)


def verify_no_3proxy_in_api_cgroup():
    group = systemctl("show", "--property=ControlGroup", "--value", "ipv6-proxy-api").stdout.strip()
    procs = Path("/sys/fs/cgroup", group.lstrip("/"), "cgroup.procs").read_text().split()
    names = [Path(f"/proc/{pid}/comm").read_text().strip() for pid in procs if Path(f"/proc/{pid}").exists()]
    assert "3proxy" not in names, f"3proxy running inside the API cgroup {group}: {names}"


def assert_untouched(before, before_pids, records, what):
    assert snapshot() == before, f"{what} changed env/state/units/proxy configs"
    assert pids() == before_pids, f"{what} restarted the API or a proxy"
    assert json_command("list") == records, f"{what} changed the proxy list"


def verify_installer_reruns(installer):
    """Re-running without changes is a no-op ("Nada a atualizar"), not a failure."""
    before, before_pids, records = snapshot(), pids(), json_command("list")
    for arguments in ([], ["--update"]):
        run = subprocess.run(["bash", str(installer), *arguments], text=True, capture_output=True, timeout=600)
        assert run.returncode == 0, f"install.sh {arguments} exit {run.returncode}: {run.stderr[-2000:]}"
        assert "Nada a atualizar" in run.stdout, f"install.sh {arguments}: {run.stdout[-2000:]}"
        assert_untouched(before, before_pids, records, f"install.sh {arguments}")


def verify_cli_only(installer):
    before, before_pids, records = snapshot(), pids(), json_command("list")
    # Exercise both first-time addition to an older API and subsequent CLI update.
    Path(CLI).unlink()
    for _ in range(2):
        subprocess.run(["bash", str(installer), "--cli-only"], check=True, timeout=60)
        assert_untouched(before, before_pids, records, "--cli-only")
        metadata = Path(CLI).stat()
        assert metadata.st_uid == 0 and metadata.st_gid == 0
        assert metadata.st_mode & 0o777 == 0o755
    command("--help")


def verify_api_restart():
    """Restarting the API must not restart any ipv6-proxy@<port>."""
    before_proxies, records = proxy_pids(), json_command("list")
    old_api = main_pid("ipv6-proxy-api")
    assert systemctl("restart", "ipv6-proxy-api").returncode == 0
    assert wait_api() == records, "Restart must preserve every proxy, port and IPv6"
    assert main_pid("ipv6-proxy-api") not in (0, old_api)
    assert proxy_pids() == before_proxies, "API restart restarted proxy instances"
    verify_no_3proxy_in_api_cgroup()


def main():
    if os.geteuid() != 0:
        sys.exit("smoke_deployment.py: run with sudo on a disposable host with the API installed")
    installer = Path(sys.argv[1]).resolve()
    assert json_command("list") == []
    first = json_command("create", "--port", "30000", "--username", "ci", "--password-stdin", "--yes",
                         stdin="ci-test\n")
    batch = []
    try:
        assert proxy_pids().keys() == {30000}, "create must start exactly ipv6-proxy@30000"
        verify_no_3proxy_in_api_cgroup()
        verify_cli_only(installer)
        table = command("list").stdout
        assert "30000" in table and "ci" in table
        second = json_command("rotate", "30000", "--yes")
        assert first["ipv6"] != second["ipv6"] and first["port"] == second["port"]
        edited = json_command("edit", "30000", "--protocol", "socks5", "--yes")
        assert edited == {**second, "protocol": "socks5"}, "edit must keep port, IPv6 and username"
        batch = json.loads(command("batch", "-c", "3", "--start-port", str(BATCH_START), "--no-auth",
                                   "--format", "json", "--yes").stdout)
        assert [p["port"] for p in batch] == [BATCH_START, BATCH_START + 1, BATCH_START + 2], batch
        assert len({p["ipv6"] for p in batch} | {edited["ipv6"]}) == 4, "batch IPv6s must be unique"
        assert proxy_pids().keys() == {30000, *(p["port"] for p in batch)}, "one unit per proxy"
        verify_api_restart()
        verify_installer_reruns(installer)
    finally:
        for port in [30000, *(p["port"] for p in batch)]:
            command("delete", str(port), "--yes", check=False)
    assert json_command("list") == []
    assert proxy_pids() == {}, "delete must stop its ipv6-proxy@<port>"
    missing = command("delete", "30000", "--yes", check=False)
    assert missing.returncode == 4 and not missing.stdout
    print("Deployed CLI/API: per-proxy units, CLI-only/re-run/--update preservation, create, list, rotate, "
          "edit, batch, API restart without proxy restarts, 0 3proxy in API cgroup and delete passed")


if __name__ == "__main__":
    main()
