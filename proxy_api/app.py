import hashlib
import json
import re
import secrets
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .backend import LinuxBackend
from .config import Settings
from .manager import Manager, fingerprint
from .models import (ID_PATTERN, OWNER_PATTERN, BatchCreate, ProxyCreate, ProxyStatusView, ProxyUpdate,
                     ProxyView)

SPEC = "ashford.proxy/1"
# Announce only what tests/test_isolation_3proxy.py and test_supervisor_3proxy.py prove against real 3proxy.
CAPABILITIES = ["isolated_mutations", "owner_scoping", "idempotency_keys", "conditional_mutations",
                "socks5_udp_associate", "edit", "batch", "per_proxy_status"]
# UDP ASSOCIATE bypasses 3proxy's destination ACL (ADR 0007), so with the SSRF
# block on it is denied in render_config and must not be announced.
UDP_CAPABILITY = "socks5_udp_associate"

def capabilities_for(settings):
    if settings.block_private_destinations:
        return [name for name in CAPABILITIES if name != UDP_CAPABILITY]
    return CAPABILITIES
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

def load_client_tokens(path):
    """{owner: sha256-hex} -> {sha256-hex: owner}; malformed files stop startup."""
    if path is None:
        return {}
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("client tokens file must be a JSON object")
    tokens = {}
    for owner, digest in raw.items():
        if not re.fullmatch(OWNER_PATTERN, owner) or not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise ValueError("client tokens file has an invalid owner or digest")
        if digest in tokens:
            raise ValueError("client tokens file reuses a token for two owners")
        tokens[digest] = owner
    return tokens

def create_app(settings=None, backend_factory=LinuxBackend):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.from_env()
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.data_dir.chmod(0o700)
        app.state.settings = config
        app.state.client_tokens = load_client_tokens(config.client_tokens_file)
        app.state.manager = Manager(config, backend_factory(config))
        try:
            yield
        finally:
            app.state.manager.close()

    app = FastAPI(title="IPv6 Proxy Server API", version="0.2.0+ashford", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def principal(credentials: HTTPAuthorizationCredentials = Depends(bearer),
                  x_proxy_owner: Optional[str] = Header(default=None)):
        """Return the owner scope: None for the admin view, else an owner name.

        Client tokens are bound to one owner server-side; that is the isolation
        boundary. X-Proxy-Owner only narrows the admin token (cooperative scope
        for a single trusted caller), and a client token cannot widen itself.
        """
        if credentials is None:
            raise HTTPException(401, "Invalid API token", headers={"WWW-Authenticate": "Bearer"})
        token = credentials.credentials.encode()
        if secrets.compare_digest(token, app.state.settings.api_token.encode()):
            owner = x_proxy_owner
        else:
            owner = app.state.client_tokens.get(hashlib.sha256(token).hexdigest())
            if owner is None:
                raise HTTPException(401, "Invalid API token", headers={"WWW-Authenticate": "Bearer"})
            if x_proxy_owner not in (None, owner):
                raise HTTPException(403, "Token cannot act for another owner")
        if owner is not None and not re.fullmatch(OWNER_PATTERN, owner):
            raise HTTPException(422, "Invalid X-Proxy-Owner")
        return owner

    def idempotency_key(key: Optional[str] = Header(default=None, alias="Idempotency-Key")):
        if key is not None and not IDEMPOTENCY_PATTERN.fullmatch(key):
            raise HTTPException(422, "Invalid Idempotency-Key")
        return key

    def if_match(value: Optional[str] = Header(default=None, alias="If-Match")):
        """Expected proxy id; quotes (ETag style) are accepted and stripped."""
        if value is None:
            return None
        value = value.strip().removeprefix("W/").strip('"')
        if not re.fullmatch(ID_PATTERN, value):
            raise HTTPException(422, "Invalid If-Match")
        return value

    def run(owner, key, request_parts, plan, pinned=False, partial=False):
        status, body = app.state.manager.mutate(owner or "", key, fingerprint(*request_parts), plan, pinned, partial)
        return Response(status_code=status) if body is None else JSONResponse(body, status_code=status)

    @app.get("/capabilities")
    def capabilities(owner=Depends(principal)):
        return {"spec": SPEC, "capabilities": capabilities_for(app.state.settings)}

    @app.get("/proxies", response_model=list[ProxyStatusView] | list[ProxyView])
    def list_proxies(owner=Depends(principal), include: Optional[Literal["status"]] = None):
        manager = app.state.manager
        with manager.lock:
            # Health is scoped to what the caller sees: a fenced port of one
            # owner must not turn every other owner's listing into a 503.
            visible = manager.visible(owner)
            if include == "status":
                # Per-item status replaces the all-or-503 health gate: it reports the failure itself.
                statuses = manager.statuses(visible)
                return [{**manager.view(record), "status": statuses[record.port]} for record in visible]
            if manager.broken:
                raise HTTPException(503, "3proxy unavailable; inspect service logs")
            fenced = sorted(r.port for r in visible if r.port in manager.broken_ports)
            if fenced:
                raise HTTPException(503, f"Fenced port(s) {', '.join(map(str, fenced))}; see "
                                         "GET /proxies?include=status and POST /proxies/{port}/recover (admin)")
            if not manager.backend.healthy(visible):
                raise HTTPException(503, "3proxy unavailable; inspect service logs")
            return [manager.view(record) for record in visible]

    @app.get("/proxies/{port}", response_model=ProxyStatusView)
    def get_proxy(port: int, owner=Depends(principal)):
        """One proxy with its status; answers even when that proxy is down."""
        manager = app.state.manager
        with manager.lock:
            record = manager.get(port, owner)
            return {**manager.view(record), "status": manager.statuses([record])[port]}

    @app.post("/proxies/batch", status_code=201)
    def create_batch(request: BatchCreate, owner=Depends(principal), key=Depends(idempotency_key)):
        if owner is not None and request.owner not in (None, owner):
            raise HTTPException(403, "Token cannot create proxies for another owner")
        return run(owner, key, ["POST", "/proxies/batch", request.model_dump(mode="json")],
                   lambda: app.state.manager.plan_batch(request, owner), partial=request.partial)

    @app.patch("/proxies/{port}", response_model=ProxyView)
    def update_proxy(port: int, request: ProxyUpdate, owner=Depends(principal), key=Depends(idempotency_key),
                     expected=Depends(if_match)):
        parts = ["PATCH", f"/proxies/{port}", expected, request.model_dump(mode="json", include=request.model_fields_set)]
        return run(owner, key, parts, lambda: app.state.manager.plan_update(port, request, owner, expected))

    @app.post("/proxies", response_model=ProxyView, status_code=201)
    def create_proxy(request: ProxyCreate, owner=Depends(principal), key=Depends(idempotency_key)):
        if owner is not None:
            if request.owner not in (None, owner):
                raise HTTPException(403, "Token cannot create proxies for another owner")
            request = request.model_copy(update={"owner": owner})
        return run(owner, key, ["POST", "/proxies", request.model_dump(mode="json")],
                   lambda: app.state.manager.plan_create(request))

    @app.post("/proxies/{port}/rotate", response_model=ProxyView)
    def rotate_proxy(port: int, owner=Depends(principal), key=Depends(idempotency_key),
                     expected=Depends(if_match)):
        return run(owner, key, ["POST", f"/proxies/{port}/rotate", expected],
                   lambda: app.state.manager.plan_rotate(port, owner, expected))

    @app.post("/proxies/{port}/recover", response_model=Optional[ProxyStatusView])
    def recover_proxy(port: int, owner=Depends(principal), key=Depends(idempotency_key),
                      expected=Depends(if_match)):
        """Admin: re-evaluate one fenced port (adopt if up, else bring it up).

        Idempotent by nature: a port that is not fenced is left untouched and
        its status returned, so Idempotency-Key is only validated, not stored."""
        if owner is not None:  # None only for the admin token without X-Proxy-Owner
            raise HTTPException(403, "Admin token required (without X-Proxy-Owner)")
        status, body = app.state.manager.recover(port, expected)
        return Response(status_code=status) if body is None else JSONResponse(body, status_code=status)

    @app.delete("/proxies/{port}", status_code=204)
    def delete_proxy(port: int, owner=Depends(principal), key=Depends(idempotency_key),
                     expected=Depends(if_match)):
        # Keys of keyless-precondition deletes are pinned (see Manager.mutate).
        return run(owner, key, ["DELETE", f"/proxies/{port}", expected],
                   lambda: app.state.manager.plan_delete(port, owner, expected), pinned=expected is None)

    return app

app = create_app()
