#!/usr/bin/env python3
"""Stdlib-only probes for tests/e2e/vm_e2e.sh (runs on the VM, no venv needed).

streams: local TCP target on [::]:PORT that greets each client with its peer
         address and then ticks every 0.2 s; one tunnel per proxy in
         $E2E_PROXIES (JSON list of {name, scheme, port, username, password,
         ipv6}) held for --seconds. Passwords come from the environment, never argv.
tui:     run a command in a pseudo-terminal, read its screen for --seconds,
         send 'q' and report whether it rendered and stayed up.
Both print one JSON object and exit 1 on failure.
"""
import argparse
import base64
import ipaddress
import json
import os
import pty
import select
import socket
import struct
import subprocess
import sys
import threading
import time

TICK = 0.2


def serve(port, stop):
    server = socket.create_server(("::", port), family=socket.AF_INET6, dualstack_ipv6=True, reuse_port=False)
    server.settimeout(0.5)

    def handle(conn, peer):
        with conn:
            try:
                conn.sendall(f"{peer[0]}\n".encode())
                while not stop.is_set():
                    conn.sendall(b".")
                    time.sleep(TICK)
            except OSError:
                pass

    while not stop.is_set():
        try:
            conn, peer = server.accept()
        except socket.timeout:
            continue
        threading.Thread(target=handle, args=(conn, peer), daemon=True).start()
    server.close()


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("proxy closed the connection during handshake")
        data += chunk
    return data


def open_tunnel(proxy, target_port, target_host="::1"):
    sock = socket.create_connection(("127.0.0.1", proxy["port"]), timeout=10)
    user, password = proxy.get("username"), proxy.get("password")
    if proxy["scheme"] == "http":
        request = f"CONNECT [{target_host}]:{target_port} HTTP/1.1\r\nHost: [{target_host}]:{target_port}\r\n"
        if user:
            request += "Proxy-Authorization: Basic " + base64.b64encode(f"{user}:{password}".encode()).decode() + "\r\n"
        sock.sendall((request + "\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += recv_exact(sock, 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200" not in status:
            raise ConnectionError(f"CONNECT refused: {status}")
    else:
        methods = b"\x02" if user else b"\x00"
        sock.sendall(b"\x05\x01" + methods)
        if recv_exact(sock, 2) != b"\x05" + methods:
            raise ConnectionError("SOCKS5 method rejected")
        if user:
            sock.sendall(b"\x01" + bytes([len(user)]) + user.encode() + bytes([len(password)]) + password.encode())
            if recv_exact(sock, 2)[1] != 0:
                raise ConnectionError("SOCKS5 authentication failed")
        sock.sendall(b"\x05\x01\x00\x04" + ipaddress.IPv6Address(target_host).packed + struct.pack(">H", target_port))
        reply = recv_exact(sock, 4)
        if reply[1] != 0:
            raise ConnectionError(f"SOCKS5 connect failed: code {reply[1]}")
        recv_exact(sock, {1: 4, 4: 16}.get(reply[3], 0) + 2)
    line = b""
    while not line.endswith(b"\n"):
        line += recv_exact(sock, 1)
    return sock, line.decode().strip()


def hold(proxy, target_port, seconds, result):
    entry = result[proxy["name"]] = {"port": proxy["port"], "ok": False}
    try:
        sock, egress = open_tunnel(proxy, target_port)
        entry["egress"] = egress
        sock.settimeout(2)
        last = start = time.monotonic()
        max_gap = 0.0
        while time.monotonic() - start < seconds:
            if not sock.recv(64):
                raise ConnectionError("stream closed by proxy")
            now = time.monotonic()
            max_gap, last = max(max_gap, now - last), now
        sock.close()
        entry["max_gap"] = round(max_gap, 3)
        expected = proxy.get("ipv6")
        egress_ok = expected is None or ipaddress.ip_address(egress) == ipaddress.ip_address(expected)
        entry["egress_ok"] = egress_ok
        entry["ok"] = egress_ok and max_gap < 1.0
    except (OSError, ValueError) as error:
        entry["error"] = str(error)


def streams(args):
    proxies = json.loads(os.environ["E2E_PROXIES"])
    stop = threading.Event()
    threading.Thread(target=serve, args=(args.target_port, stop), daemon=True).start()
    time.sleep(0.3)
    result = {}
    threads = [threading.Thread(target=hold, args=(p, args.target_port, args.seconds, result)) for p in proxies]
    for thread in threads:
        thread.start()
    if args.ready_file:
        open(args.ready_file, "w").close()
    for thread in threads:
        thread.join()
    stop.set()
    print(json.dumps(result, sort_keys=True))
    return 0 if result and all(e["ok"] for e in result.values()) else 1


def tui(args):
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM="xterm-256color", COLUMNS="120", LINES="40")
        os.execvp(args.command[0], args.command)
    os.set_blocking(fd, False)
    screen, deadline, exited = b"", time.monotonic() + args.seconds, None
    while time.monotonic() < deadline and exited is None:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                screen += os.read(fd, 65536)
            except OSError:
                pass
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            exited = os.waitstatus_to_exitcode(status)
    alive = exited is None
    if alive:
        try:
            os.write(fd, b"q")
            time.sleep(1)
            if os.waitpid(pid, os.WNOHANG)[0] == 0:
                os.kill(pid, 15)
                os.waitpid(pid, 0)
        except OSError:
            pass
    text = screen.decode(errors="replace")
    missing = [word for word in args.expect if word not in text]
    result = {"rendered_bytes": len(screen), "alive_after_seconds": alive, "early_exit_code": exited,
              "missing": missing, "ok": alive and len(screen) > 0 and not missing}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("streams")
    s.add_argument("--target-port", type=int, default=18080)
    s.add_argument("--seconds", type=float, default=15)
    s.add_argument("--ready-file")
    t = sub.add_parser("tui")
    t.add_argument("--seconds", type=float, default=5)
    t.add_argument("--expect", action="append", default=[])
    t.add_argument("command", nargs="+")
    args = parser.parse_args(argv)
    return streams(args) if args.cmd == "streams" else tui(args)


if __name__ == "__main__":
    sys.exit(main())
