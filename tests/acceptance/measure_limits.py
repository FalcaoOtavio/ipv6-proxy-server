"""Criterion C + measured limits against real 3proxy (manual QA run, writes evidence).

    TEST_3PROXY_BINARY=/path/3proxy .venv/bin/python tests/acceptance/measure_limits.py [--out docs/evidence/limits.md]

Same harness as the A/B acceptance tests (real uvicorn API, real 3proxy per port,
only `ip addr` simulated). Steps:
1. CLI batch 100 HTTP (common credentials) and 100 SOCKS5 (random) with -f export.
2. curl through EVERY exported line (HTTP proxy / socks5h) to a local echo server
   that answers with the peer address: must equal that proxy's IPv6.
3. tests/e2e/e2e_probe.py streams over proxies parsed from the export files.
4. CLI list/export and the TUI (real pty) see the same API.
5. Resources: RSS and fds per 3proxy, API fds, batch time for 100/200, latency of
   create/edit/rotate/delete via API and CLI.
Exit 1 on any failed check. Egress is v4-mapped loopback, NOT routed IPv6.
"""
import argparse
import http.server
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from ipaddress import IPv6Address
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from acceptance import conftest  # noqa: E402

PROBE = ROOT / "tests" / "e2e" / "e2e_probe.py"
TARGET_HOST = "::ffff:127.0.0.1"
failures = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)
    return ok


class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.client_address[0].encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class EchoServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def rss_kb(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def fds(pid):
    return len(os.listdir(f"/proc/{pid}/fd"))


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, round(q * (len(values) - 1)))]


def summary_ms(values):
    ms = [v * 1000 for v in values]
    return {"n": len(ms), "p50_ms": round(statistics.median(ms), 1), "p95_ms": round(pct(ms, 0.95), 1),
            "max_ms": round(max(ms), 1)}


def parse_export(path):
    proxies = []
    for line in path.read_text().splitlines():
        ipv4, port, *auth = line.split(":")
        proxies.append({"ipv4": ipv4, "port": int(port), "username": auth[0] if auth else None,
                        "password": auth[1] if len(auth) > 1 else None})
    return proxies


def curl_through(proxy, scheme, url):
    """Credentials via curl's stdin config, never argv."""
    auth = f"{proxy['username']}:{proxy['password']}@" if proxy["username"] else ""
    config = f'proxy = "{scheme}://{auth}127.0.0.1:{proxy["port"]}"\nurl = "{url}"\n'
    run = subprocess.run(["curl", "--config", "-", "--silent", "--show-error", "--max-time", "20",
                          "--noproxy", ""], input=config, capture_output=True, text=True, timeout=30)
    return run.returncode, run.stdout.strip(), run.stderr.strip()


def cli(h, env_file, *argv, stdin=None):
    """Real CLI process (same shim as the acceptance tests); returns (run, seconds)."""
    started = time.monotonic()
    run = subprocess.run([sys.executable, "-c", conftest.CLI_SHIM, str(ROOT / "ipv6-proxy"), h.base_url,
                          str(env_file), *argv], input=stdin, capture_output=True, text=True, timeout=600)
    return run, time.monotonic() - started


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "evidence" / "limits.md")
    parser.add_argument("--latency-samples", type=int, default=20)
    args = parser.parse_args()
    if not conftest.BINARY:
        sys.exit("Set TEST_3PROXY_BINARY to the real 3proxy binary")

    # 200 batch proxies + latency proxies + a margin of skipped/occupied ports.
    block = conftest.free_port_block
    conftest.free_port_block = lambda: block(250)
    workdir = Path(subprocess.check_output(["mktemp", "-d"], text=True).strip())
    h = conftest.Harness(workdir)
    h.http.timeout = 600
    echo = EchoServer(("::", 0), Echo)
    threading.Thread(target=echo.serve_forever, daemon=True).start()
    url = f"http://[{TARGET_HOST}]:{echo.server_address[1]}/"
    env_file = conftest.cli_env_file(workdir, conftest.ADMIN)
    results = {"network_scope": conftest.NETWORK_SCOPE, "host": {"nproc": os.cpu_count(),
               "loadavg": Path("/proc/loadavg").read_text().split()[:3]}}
    try:
        # 1. Batches through the CLI with export files.
        http_list, socks_list = workdir / "http.list", workdir / "socks.list"
        p0 = h.ports[0]
        run, t_http = cli(h, env_file, "batch", "-c", "100", "--start-port", str(p0), "-t", "http",
                          "-u", "qa", "--password-stdin", "-f", str(http_list), "--yes", stdin="qa-common-pw\n")
        check(run.returncode == 0, f"CLI batch 100 HTTP (rc={run.returncode}) {run.stderr.strip()[-300:]}")
        results["batch_100_http_s"] = round(t_http, 2)

        run, t_socks = cli(h, env_file, "batch", "-c", "100", "--start-port", str(p0 + 100), "-t", "socks5",
                           "--random", "-u", "qs", "-f", str(socks_list), "--yes")
        results["batch_100_socks5_s"] = round(t_socks, 2)
        check(run.returncode == 0, f"CLI batch 100 SOCKS5 random (rc={run.returncode}) {run.stderr.strip()[-300:]}")

        listed = {p["port"]: p for p in h.call("GET", "/proxies").json()}
        check(len(listed) == 200, f"API lista 200 proxies (obtido {len(listed)})")
        exported = {"http": parse_export(http_list), "socks5h": parse_export(socks_list)}
        for name, path in (("http", http_list), ("socks", socks_list)):
            check(path.stat().st_mode & 0o777 == 0o600, f"export {name} com modo 0600")
        check(len(exported["http"]) == 100 and all(p["password"] == "qa-common-pw" and p["username"] == "qa"
                                                     for p in exported["http"]), "export HTTP: 100 linhas qa:senha comum")
        socks_users = {p["username"] for p in exported["socks5h"]}
        socks_pw = {p["password"] for p in exported["socks5h"]}
        check(len(exported["socks5h"]) == 100 and len(socks_pw) == 100 and len(socks_users) == 100,
              "export SOCKS5: 100 linhas com usuario/senha aleatoria unicos")
        ports = sorted(listed)
        check(ports == sorted(p["port"] for v in exported.values() for p in v), "export cobre exatamente as portas da API")
        check(ports[0] >= p0 and ports[-1] <= h.ports[-1], "portas dentro da faixa configurada")
        check(len({p["ipv6"] for p in listed.values()}) == 200, "200 IPv6 unicos")

        # 2. curl through every exported proxy.
        bad, started = [], time.monotonic()
        for scheme, proxies in exported.items():
            for proxy in proxies:
                rc, body, err = curl_through(proxy, scheme, url)
                expected = listed[proxy["port"]]["ipv6"]
                try:
                    ok = rc == 0 and IPv6Address(body) == IPv6Address(expected)
                except ValueError:
                    ok = False
                if not ok:
                    bad.append({"port": proxy["port"], "scheme": scheme, "rc": rc, "body": body[:80], "err": err[:120]})
        results["curl_200_s"] = round(time.monotonic() - started, 2)
        check(not bad, f"curl via todas as 200 linhas exportadas sai pelo IPv6 do proxy (falhas: {bad[:5]})")
        # Wrong password must be refused (407 / SOCKS auth failure).
        wrong = dict(exported["http"][0], password="wrong-pw")
        rc, body, _ = curl_through(wrong, "http", url)
        check(rc != 0 or body != listed[wrong["port"]]["ipv6"], "senha errada recusada (HTTP)")
        wrong = dict(exported["socks5h"][0], password="wrong-pw")
        rc, body, _ = curl_through(wrong, "socks5h", url)
        check(rc != 0, "senha errada recusada (SOCKS5)")

        # 3. e2e_probe streams over proxies taken from the export files.
        sample = exported["http"][:5] + exported["socks5h"][:5]
        env = dict(os.environ, **{f"E2E_PASSWORD_{p['port']}": p["password"] for p in sample})
        specs = []
        for p in sample:
            scheme = "http" if p in exported["http"] else "socks5"
            specs += ["--proxy", f"{scheme}{p['port']}:{scheme}:{p['port']}:{p['username']}:{listed[p['port']]['ipv6']}"]
        probe = subprocess.run([sys.executable, str(PROBE), "streams", "--seconds", "8", "--target-host", TARGET_HOST,
                                "--target-port", str(free_port()), *specs], env=env, capture_output=True, text=True,
                               timeout=120)
        results["e2e_probe"] = json.loads(probe.stdout or "{}")
        check(probe.returncode == 0, f"e2e_probe streams em 10 proxies do export (rc={probe.returncode}) "
                                     f"{probe.stderr[-300:]}")

        # 4. CLI and TUI against the same API.
        run, _ = cli(h, env_file, "list", "--output", "json")
        check(run.returncode == 0 and len(json.loads(run.stdout)) == 200, "CLI list ve os 200")
        run, _ = cli(h, env_file, "export")
        check(run.returncode == 0 and len(run.stdout.splitlines()) == 200, "CLI export lista os 200 (sem senha)")
        check("qa-common-pw" not in run.stdout, "CLI export nao contem senhas")
        tui_env = dict(os.environ, PROXY_TUI_API_URL=h.base_url, PROXY_API_TOKEN=conftest.ADMIN, PROXY_TUI_REFRESH="0")
        tui = subprocess.run([sys.executable, str(PROBE), "tui", "--seconds", "12", "--expect", str(ports[0]),
                              "--", sys.executable, str(ROOT / "ipv6-proxy-tui")], env=tui_env,
                             capture_output=True, text=True, timeout=60)
        results["tui"] = json.loads(tui.stdout or "{}")
        check(tui.returncode == 0, f"TUI em pty real renderiza a lista da mesma API (porta {ports[0]})")

        # 5a. Resources with 200 live proxies.
        pids = {port: h.pid(port) for port in ports}
        check(all(pids.values()), "200 processos 3proxy vivos")
        rss = [rss_kb(pid) for pid in pids.values()]
        fd = [fds(pid) for pid in pids.values()]
        results["resources_200"] = {
            "rss_kb_per_3proxy": {"min": min(rss), "median": statistics.median(rss), "max": max(rss),
                                  "total_mb": round(sum(rss) / 1024, 1)},
            "fds_per_3proxy": {"min": min(fd), "median": statistics.median(fd), "max": max(fd)},
            "api_process_fds": fds(os.getpid()), "api_process_rss_kb": rss_kb(os.getpid()),
        }

        # 5b. Latency of single operations through the API (each touches one proxy).
        lat = {"create": [], "edit": [], "rotate": [], "delete": []}
        free_ports = [p for p in h.ports if p not in listed][:args.latency_samples]
        for port in free_ports:
            t = time.monotonic(); r = h.call("POST", "/proxies", json={"port": port, "protocol": "http"})
            lat["create"].append(time.monotonic() - t)
            if not check(r.status_code == 201, f"API create {port}") or r.status_code != 201:
                continue
            pid = r.json()["id"]
            t = time.monotonic(); r = h.call("PATCH", f"/proxies/{port}", json={"protocol": "socks5"},
                                             headers={"If-Match": pid})
            lat["edit"].append(time.monotonic() - t)
            check(r.status_code == 200, f"API PATCH {port}")
            t = time.monotonic(); r = h.call("POST", f"/proxies/{port}/rotate")
            lat["rotate"].append(time.monotonic() - t)
            check(r.status_code == 200, f"API rotate {port}")
            t = time.monotonic(); r = h.call("DELETE", f"/proxies/{port}", headers={"If-Match": pid})
            lat["delete"].append(time.monotonic() - t)
            check(r.status_code == 204, f"API delete {port}")
        results["api_latency_with_200_live"] = {k: summary_ms(v) for k, v in lat.items() if v}
        port = free_ports[0]
        cli_lat = {}
        for name, argv in (("create", ["create", "--port", str(port), "--yes", "--output", "json"]),
                           ("edit", ["edit", str(port), "-t", "socks5", "--yes", "--output", "json"]),
                           ("rotate", ["rotate", str(port), "--yes", "--output", "json"]),
                           ("delete", ["delete", str(port), "--yes", "--output", "json"])):
            run, elapsed = cli(h, env_file, *argv)
            check(run.returncode == 0, f"CLI {name} {port} {run.stderr.strip()[-200:]}")
            cli_lat[name] = round(elapsed * 1000, 1)
        results["cli_latency_ms_incl_python_start"] = cli_lat
        check(all(h.pid(p) == pids[p] for p in ports), "PIDs dos 200 inalterados apos create/edit/rotate/delete de outros")

        # 5c. Delete all, then one batch of 200 through the API.
        started = time.monotonic()
        for port in ports:
            r = h.call("DELETE", f"/proxies/{port}")
            if r.status_code != 204:
                check(False, f"delete {port} -> {r.status_code}")
        results["delete_200_sequential_s"] = round(time.monotonic() - started, 2)
        check(h.call("GET", "/proxies").json() == [], "lista vazia apos remover 200")
        started = time.monotonic()
        r = h.call("POST", "/proxies/batch", json={"count": 200, "start_port": p0, "protocol": "socks5",
                                                  "credentials": "random", "username": "big"})
        results["batch_200_api_s"] = round(time.monotonic() - started, 2)
        check(r.status_code == 201 and len(r.json()["created"]) == 200, f"API batch 200 -> {r.status_code}")
        started = time.monotonic()
        r = h.call("GET", "/proxies")
        results["list_200_ms"] = round((time.monotonic() - started) * 1000, 1)
        check(r.status_code == 200 and len(r.json()) == 200, "GET /proxies com 200")

        # 5d. Same 200 with the production default (SSRF block on). Loopback traffic cannot be
        # measured then (::ffff:127.0.0.1 is a denied destination): prove the refusal instead.
        for port in [p["port"] for p in r.json()]:
            h.call("DELETE", f"/proxies/{port}")
        h.settings.block_private_destinations = True
        started = time.monotonic()
        r = h.call("POST", "/proxies/batch", json={"count": 200, "start_port": p0, "protocol": "http",
                                                  "credentials": "random", "username": "ssrf"})
        results["batch_200_api_ssrf_on_s"] = round(time.monotonic() - started, 2)
        created = r.json().get("created", []) if r.status_code in (201, 207) else []
        check(r.status_code == 201 and len(created) == 200, f"API batch 200 com SSRF ligado -> {r.status_code}")
        if created:
            first = created[0]
            config = h.config(first["port"]).decode()
            check("deny * * ::/128,::1/128,::ffff:0:0/96" in config, "cfg gerado contem o deny de destinos privados")
            auth = f"{first['username']}:{first['password']}@"
            config_in = f'proxy = "http://{auth}127.0.0.1:{first["port"]}"\nurl = "{url}"\n'
            run = subprocess.run(["curl", "--config", "-", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                  "--max-time", "20", "--noproxy", ""], input=config_in,
                                 capture_output=True, text=True, timeout=30)
            results["ssrf_on_loopback_http_code"] = run.stdout
            check(run.stdout == "403", f"SSRF ligado: proxy recusa destino loopback com 403 (obtido {run.stdout})")
            rss_on = [rss_kb(h.pid(p["port"])) for p in created]
            results["rss_kb_per_3proxy_ssrf_on_median"] = statistics.median(rss_on)
    finally:
        echo.shutdown()
        h.close()
        conftest.free_port_block = block
    write_report(args.out, results)
    print(json.dumps(results, indent=1, default=str))
    print(f"{len(failures)} falha(s)" if failures else "todas as verificacoes passaram")
    return 1 if failures else 0


def free_port():
    with socket.socket(socket.AF_INET6) as s:
        s.bind(("::", 0))
        return s.getsockname()[1]


def write_report(path, r):
    res = r.get("resources_200", {})
    lines = [
        "# Limites medidos (QA)", "",
        f"Gerado por `tests/acceptance/measure_limits.py` em {time.strftime('%Y-%m-%d %H:%M:%S %Z')}.",
        f"Escopo de rede: {r['network_scope']}. 3proxy real 0.9.4, supervisor local (um processo por porta),",
        f"API uvicorn real no mesmo processo do script. Host: {r['host']['nproc']} CPUs, load average "
        f"{' '.join(r['host']['loadavg'])} (host compartilhado; tempos sao limite superior, nao benchmark).", "",
        "| Medida | Valor |", "|---|---|",
        f"| Lote 100 HTTP via CLI (credencial comum, `-f`) | {r.get('batch_100_http_s')} s |",
        f"| Lote 100 SOCKS5 via CLI (`--random`, `-f`) | {r.get('batch_100_socks5_s')} s |",
        f"| Lote 200 SOCKS5 via API (`POST /proxies/batch`) | {r.get('batch_200_api_s')} s |",
        f"| Lote 200 HTTP via API com SSRF ligado (padrao de producao) | {r.get('batch_200_api_ssrf_on_s')} s |",
        f"| RSS mediano por 3proxy com SSRF ligado | {r.get('rss_kb_per_3proxy_ssrf_on_median')} KiB |",
        f"| Remover 200 (DELETE sequencial) | {r.get('delete_200_sequential_s')} s |",
        f"| `GET /proxies` com 200 | {r.get('list_200_ms')} ms |",
        f"| curl pelas 200 linhas exportadas (sequencial) | {r.get('curl_200_s')} s |",
    ]
    if res:
        rss, fd = res["rss_kb_per_3proxy"], res["fds_per_3proxy"]
        lines += [
            f"| RSS por 3proxy (min/mediana/max) | {rss['min']} / {rss['median']} / {rss['max']} KiB |",
            f"| RSS total dos 200 3proxy | {rss['total_mb']} MiB |",
            f"| fds por 3proxy ocioso (min/mediana/max) | {fd['min']} / {fd['median']} / {fd['max']} |",
            f"| fds do processo da API com 200 proxies | {res['api_process_fds']} |",
        ]
    lines += ["", "## Latencia por operacao (API, com 200 proxies vivos)", "",
              "| Operacao | n | p50 | p95 | max |", "|---|---|---|---|---|"]
    for op, s in r.get("api_latency_with_200_live", {}).items():
        lines.append(f"| {op} | {s['n']} | {s['p50_ms']} ms | {s['p95_ms']} ms | {s['max_ms']} ms |")
    lines += ["", "CLI (inclui partida do Python e leitura do env): " +
              ", ".join(f"{k} {v} ms" for k, v in r.get("cli_latency_ms_incl_python_start", {}).items()), "",
              "## Observacoes", "",
              "- SSRF (`PROXY_BLOCK_PRIVATE_DESTINATIONS`): o harness loopback roda com o bloqueio DESLIGADO,",
              "  porque o destino de teste (`::ffff:127.0.0.1`) e um destino privado negado. Lote, curl, streams,",
              "  RSS e latencia acima foram medidos assim. A fase final liga o bloqueio (padrao de producao),",
              f"  remede o lote de 200 e o RSS e confirma a recusa do loopback (HTTP {r.get('ssrf_on_loopback_http_code')}).",
              "- Cada proxy e um processo 3proxy: memoria e fds escalam linearmente com o numero de proxies.",
              "- Em systemd cada instancia adiciona uma unidade `ipv6-proxy@<porta>`; o custo de `systemctl` nao esta",
              "  incluido aqui (supervisor local). Medir na VM (T14).",
              "- A faixa v4-mapped de teste (/120) limita a 255 enderecos; em producao o limite e a subnet e",
              "  `PROXY_BATCH_MAX` (padrao 1000).",
              f"- Falhas nesta execucao: {len(failures)}" + (": " + "; ".join(failures) if failures else "."), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
