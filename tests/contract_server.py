"""Real API + real 3proxy for the Ashford Go client contract test (T91K).

Test-only launcher: LoopbackBackend from test_isolation_3proxy (real 3proxy
processes, v4-mapped egress, simulated `ip addr`), served by uvicorn, plus a
/_fault control surface that only exists here. Never deploy this file.

Env: TEST_3PROXY_BINARY, CONTRACT_DIR (scratch), CONTRACT_ADMIN_TOKEN,
CONTRACT_CLIENT_TOKEN, CONTRACT_OWNER. Prints "READY <port> <first> <last>".
"""
import asyncio
import hashlib
import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

import proxy_api.backend as backend_module  # noqa: E402
import proxy_api.manager as manager_module  # noqa: E402
from proxy_api.app import create_app  # noqa: E402
from proxy_api.config import Settings  # noqa: E402
from test_isolation_3proxy import LoopbackBackend  # noqa: E402

def free_range(count):
    for _ in range(200):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        base = probe.getsockname()[1]
        probe.close()
        if base + count > 65535:
            continue
        try:
            held = []
            for port in range(base, base + count):
                s = socket.socket()
                s.bind(("127.0.0.1", port))
                held.append(s)
            for s in held:
                s.close()
            return base, base + count - 1
        except OSError:
            for s in held:
                s.close()
    raise RuntimeError("no free port range")

scratch = Path(os.environ["CONTRACT_DIR"])
tokens = scratch / "client-tokens.json"
tokens.write_text(json.dumps({os.environ["CONTRACT_OWNER"]:
                              hashlib.sha256(os.environ["CONTRACT_CLIENT_TOKEN"].encode()).hexdigest()}))
first, last = free_range(6)
settings = Settings(subnet="::ffff:127.0.0.0/120", bind_ipv4="127.0.0.1", interface="lo",
                    api_token=os.environ["CONTRACT_ADMIN_TOKEN"], data_dir=scratch / "data",
                    binary=os.environ["TEST_3PROXY_BINARY"], port_start=first, port_end=last,
                    client_tokens_file=tokens)
api = create_app(settings, LoopbackBackend)
faults = {"delay": {}, "broken": set()}
real_render = backend_module.render_config
backend_module.render_config = lambda s, record: (
    "bogus-directive\n" if record.port in faults["broken"] else real_render(s, record))

async def app(scope, receive, send):
    if scope["type"] != "http" or not scope["path"].startswith("/_fault/"):
        return await api(scope, receive, send)
    parts = scope["path"].split("/")[2:]
    manager = api.state.manager
    body = {}
    if parts[0] == "delay":  # /_fault/delay/<METHOD>/<seconds>: next such response is held
        faults["delay"][parts[1]] = float(parts[2])
    elif parts[0] == "expire-keys":  # TTL passed for every non-pinned key
        with manager.lock:
            for entry in manager.keys.values():
                if not entry.get("pinned"):
                    entry["at"] -= manager_module.IDEMPOTENCY_TTL + 1
    elif parts[0] == "break":  # 3proxy rejects this port's config from now on
        faults["broken"].add(int(parts[1]))
    elif parts[0] == "records":  # persisted pool + fenced ports, bypassing health
        with manager.lock:
            body = {"ids": [r.id for r in manager.records], "fenced": sorted(manager.broken_ports),
                    "broken": manager.broken}
    elif parts[0] == "pid":
        process = manager.backend.processes.get(int(parts[1]))
        body = {"pid": process[1].pid if process and process[1].poll() is None else None}
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})

async def delaying(scope, receive, send):
    """Runs the request to completion, then holds the response: lost reply."""
    if scope["type"] == "http" and scope["method"] in faults["delay"] and not scope["path"].startswith("/_fault/"):
        wait = faults["delay"].pop(scope["method"])
        held = []
        async def capture(message):
            held.append(message)
        await app(scope, receive, capture)
        await asyncio.sleep(wait)
        for message in held:
            await send(message)
        return
    await app(scope, receive, send)

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
config = uvicorn.Config(delaying, fd=None, log_level="warning", lifespan="on")
server = uvicorn.Server(config)
print(f"READY {sock.getsockname()[1]} {first} {last}", flush=True)
server.run(sockets=[sock])
