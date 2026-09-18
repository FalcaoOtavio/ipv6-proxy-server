"""Run explicitly on the disposable CI VM after installing the systemd service."""
import json
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    env = dict(line.split("=", 1) for line in
               Path("/etc/ipv6-proxy-api.env").read_text().splitlines())

    def request(method, path, payload=None):
        req = Request("http://127.0.0.1:8000" + path, method=method,
                      headers={"Authorization": "Bearer " + env["PROXY_API_TOKEN"],
                               "Content-Type": "application/json"},
                      data=json.dumps(payload).encode() if payload is not None else None)
        # Mutations may wait 10s for 3proxy shutdown, then start new listeners.
        with urlopen(req, timeout=60) as response:
            body = response.read()
            return json.loads(body) if body else None

    first = request("POST", "/proxies", {"port": 30000, "username": "ci", "password": "ci-test"})
    try:
        second = request("POST", "/proxies/30000/rotate")
        assert first["ipv6"] != second["ipv6"]
        assert first["port"] == second["port"]
        subprocess.run(["systemctl", "restart", "ipv6-proxy-api"], check=True)
        for attempt in range(30):
            try:
                records = request("GET", "/proxies")
                break
            except OSError:
                if attempt == 29:
                    raise
                time.sleep(1)
        assert records == [second], "Restart must preserve the proxy and its IPv6"
    finally:
        request("DELETE", "/proxies/30000")
    assert request("GET", "/proxies") == []
    print("Deployed API: create, rotate, restart persistence and delete passed")


if __name__ == "__main__":
    main()
