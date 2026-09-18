import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .backend import LinuxBackend
from .config import Settings
from .manager import Manager
from .models import ProxyCreate, ProxyView


def create_app(settings=None, backend_factory=LinuxBackend):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.from_env()
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.data_dir.chmod(0o700)
        app.state.settings = config
        app.state.manager = Manager(config, backend_factory(config))
        try:
            yield
        finally:
            app.state.manager.close()

    app = FastAPI(title="IPv6 Proxy Server API", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def authorized(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), app.state.settings.api_token.encode()
        ):
            raise HTTPException(401, "Invalid API token", headers={"WWW-Authenticate": "Bearer"})

    def view(record):
        return ProxyView(**record.model_dump(exclude={"password"}), ipv4=app.state.settings.bind_ipv4)

    @app.get("/proxies", response_model=list[ProxyView], dependencies=[Depends(authorized)])
    def list_proxies():
        manager = app.state.manager
        with manager.lock:
            if manager.broken or not manager.backend.healthy(manager.records):
                raise HTTPException(503, "3proxy unavailable; inspect service logs")
            return [view(record) for record in manager.records]

    @app.post("/proxies", response_model=ProxyView, status_code=201, dependencies=[Depends(authorized)])
    def create_proxy(request: ProxyCreate):
        return view(app.state.manager.create(request))

    @app.post("/proxies/{port}/rotate", response_model=ProxyView, dependencies=[Depends(authorized)])
    def rotate_proxy(port: int):
        return view(app.state.manager.rotate(port))

    @app.delete("/proxies/{port}", status_code=204, dependencies=[Depends(authorized)])
    def delete_proxy(port: int):
        app.state.manager.delete(port)
        return Response(status_code=204)

    return app


app = create_app()
