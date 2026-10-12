# Evidência T11 — TUI em pty real (80x24 e 60x20)

Gerada por `tests/tui_validation.py` (API uvicorn real + LocalProcessSupervisor + 3proxy real; só `ip addr`
simulado). Cada passo: `<feature>-<passo>-<viewport>.txt` (tela do pty via pyte) e `.png` (render da mesma
grade). `results.json` lista critério a critério o que foi visto.

    TEST_3PROXY_BINARY=/data/projects/ipv6-proxy-server-independente/.tools/3proxy .venv/bin/python tests/tui_validation.py

PNG exige Playwright + Chromium no `/usr/local/bin/python3` (ferramenta do host; não é dependência do projeto).
