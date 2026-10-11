# IPv6 Proxy Server Independente

> Contexto do projeto para todos os agentes. Mantido pelo orquestrador (curto!). Docs completos em `../docs/` (fora do repo) e em `docs/devpromax/` na entrega.

## Stack
Python 3.12, FastAPI/uvicorn/pydantic (`proxy_api/`), 3proxy 0.9.4, systemd, Bash (`install.sh`, `ipv6-proxy-server.sh`), Textual (TUI).

## Comandos
- setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
- test: `.venv/bin/python -m pytest -q`
- test (3proxy real): `TEST_3PROXY_BINARY=/data/projects/ipv6-proxy-server-independente/.tools/3proxy .venv/bin/python -m pytest -q`
- lint: `bash -n install.sh ipv6-proxy-server.sh && shellcheck install.sh` (se shellcheck disponível)

## Convenções
- Conventional Commits; um branch por agente (`agent/<nome>`); merge só pelo orquestrador.
- Variáveis de ambiente documentadas em `.env.example`.
