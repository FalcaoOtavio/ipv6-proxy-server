"""Run explicitly on the disposable CI VM after installing the systemd service."""
import json
import subprocess
import sys
from pathlib import Path
import time


CLI = "/usr/local/bin/ipv6-proxy"


def command(*arguments, stdin=None, check=True):
    return subprocess.run(
        [CLI, *arguments], input=stdin, text=True, capture_output=True,
        timeout=90, check=check,
    )


def json_command(*arguments, stdin=None):
    return json.loads(command(*arguments, "--output", "json", stdin=stdin).stdout)


def verify_cli_only(installer):
    def snapshot():
        paths = [Path("/etc/ipv6-proxy-api.env"),
                 Path("/etc/systemd/system/ipv6-proxy-api.service")]
        paths += [p for p in Path("/var/lib/ipv6-proxy-api").iterdir()
                  if p.is_file() and p.suffix != ".log"]
        return {str(p): (p.read_bytes(), p.stat().st_mode, p.stat().st_uid, p.stat().st_gid)
                for p in paths}
    def pid():
        return subprocess.check_output(
            ["systemctl", "show", "--property=MainPID", "--value", "ipv6-proxy-api"], text=True).strip()
    before = snapshot()
    before_pid = pid()
    assert int(before_pid) > 0
    before_records = json_command("list")
    # Exercise both first-time addition to an older API and subsequent CLI update.
    Path(CLI).unlink()
    for _ in range(2):
        subprocess.run(["bash", str(installer), "--cli-only"], check=True, timeout=30)
        assert snapshot() == before, "CLI-only must preserve token, unit and proxy state"
        assert pid() == before_pid, "CLI-only must not restart the service"
        assert json_command("list") == before_records
        metadata = Path(CLI).stat()
        assert metadata.st_uid == 0 and metadata.st_gid == 0
        assert metadata.st_mode & 0o777 == 0o755
    command("--help")


def main():
    installer = Path(sys.argv[1]).resolve()
    assert json_command("list") == []
    first = json_command(
        "create", "--port", "30000", "--username", "ci", "--password-stdin", "--yes",
        stdin="ci-test\n",
    )
    try:
        verify_cli_only(installer)
        table = command("list").stdout
        assert "30000" in table and "ci" in table
        second = json_command("rotate", "30000", "--yes")
        assert first["ipv6"] != second["ipv6"]
        assert first["port"] == second["port"]
        subprocess.run(["systemctl", "restart", "ipv6-proxy-api"], check=True)
        for attempt in range(30):
            try:
                records = json_command("list")
                break
            except subprocess.CalledProcessError:
                if attempt == 29:
                    raise
                time.sleep(1)
        assert records == [second], "Restart must preserve the proxy and its IPv6"
    finally:
        json_command("delete", "30000", "--yes")
    assert json_command("list") == []
    missing = command("delete", "30000", "--yes", check=False)
    assert missing.returncode == 4 and not missing.stdout
    print("Deployed CLI/API: CLI-only preservation, create, list, rotate, restart persistence and delete passed")


if __name__ == "__main__":
    main()
