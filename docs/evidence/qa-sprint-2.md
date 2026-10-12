# QA sprint 2 (T08) — main com T01–T07, T13, T16

Data: 2026-10-11. Worktree `agent/qa` após `dpm sync` (main `767761c`). Host compartilhado, 8 CPUs, load average ~28.
Escopo de rede de toda a evidência local: loopback v4-mapped (`::ffff:127.0.0.0/120`) — **não é IPv6 roteado** (critério E fica para T14).

## Resultado

| Suíte | Comando | Resultado | Tempo |
|---|---|---|---|
| Lint shell | `bash -n install.sh ipv6-proxy-server.sh tests/e2e/vm_e2e.sh` + shellcheck 0.10.0 | OK | — |
| Workflows | YAML de `tests.yml` e `vm-e2e.yml` | OK | — |
| Compilação | `py_compile` de `ipv6-proxy`, `ipv6-proxy-tui`, smoke, probe, measure_limits | OK | — |
| Skip/xfail | `grep -rn 'xfail\|mark.skip(' tests` | 0 ocorrências | — |
| Suíte sem 3proxy | `.venv/bin/python -m pytest -q -rs` | 336 passed, 23 skipped | 234 s |
| Suíte com 3proxy real, execução 1 | `TEST_3PROXY_BINARY=.../.tools/3proxy .venv/bin/python -m pytest -q` | **1 failed**, 358 passed | 310 s |
| Suíte com 3proxy real, execução 2 | idem | 359 passed | 387 s |
| Suíte com 3proxy real, execução 3 | idem | 359 passed | 433 s |
| Aceite A/B isolado (3x) | `pytest -q tests/acceptance` | 12 passed (3/3) | 81–99 s |
| `start-single` isolado (5x) | `pytest -q '...::test_b_injected_failure_keeps_b_and_c[start-single]'` | 5/5 passed | 5–8 s |
| Critério C + limites | `TEST_3PROXY_BINARY=... .venv/bin/python tests/acceptance/measure_limits.py` | todas as verificações PASS | ~6 min |

Os 23 skips do modo sem 3proxy são os testes que exigem o binário real (`TEST_3PROXY_BINARY`); todos rodaram e passaram no modo real.

## Critério C (lotes) e CLI/TUI contra a mesma API — `measure_limits.py`

API uvicorn real, um 3proxy real por porta e a CLI real em processo próprio. Só `ip addr` é simulado.
- Pela CLI: `batch -c 100 -t http -u qa --password-stdin -f http.list` e `batch -c 100 -t socks5 --random -u qs -f socks.list`. Os dois exports saem com modo 0600; o HTTP traz `qa` com a senha comum, o SOCKS5 traz usuários e senhas únicos.
- O export cobre exatamente as 200 portas da API, todas na faixa configurada, com 200 IPv6 únicos.
- curl usando **cada uma das 200 linhas exportadas** (`http://` e `socks5h://`; credenciais pelo stdin do curl, fora do argv) chega a um servidor de eco, que devolve como origem o IPv6 daquele proxy. Senha errada é recusada tanto no HTTP quanto no SOCKS5.
- `tests/e2e/e2e_probe.py streams` em 10 proxies tirados dos arquivos de export (5 HTTP CONNECT e 5 SOCKS5) por 8 s: sem queda, egress = IPv6 do proxy.
- A CLI `list` e a `export` veem os 200 (o export sai sem senhas). A TUI `ipv6-proxy-tui`, em pty real contra a mesma API, mostra a primeira porta do lote.
- Os PIDs dos 200 3proxy ficam inalterados depois de 20 ciclos de create/edit/rotate/delete em outras portas, mais o ciclo pela CLI.
- Números em `docs/evidence/limits.md`.

## Falhas

### F1 — instável: `tests/acceptance/test_criteria_ab.py::test_b_injected_failure_keeps_b_and_c[start-single]`
- Reprodução: `TEST_3PROXY_BINARY=/data/projects/ipv6-proxy-server-independente/.tools/3proxy .venv/bin/python -m pytest -q -p no:cacheprovider` (suíte inteira, host com load ~28). Falhou 1 vez em 3 execuções completas; isolado passou 5/5 e o pacote `tests/acceptance` passou 3/3.
- Esperado: as conexões B/C continuam recebendo frames depois da falha injetada no start de outra porta.
- Obtido: `AssertionError: [None, None]` em `Survivors.verify` (`tests/acceptance/test_criteria_ab.py:55`). `probe.alive()` voltou False para B e C **sem erro de socket** (`error=None`). Ou seja, as conexões não caíram; só não chegou frame novo na janela de 0,3 s de `StreamProbe.alive()` (`tests/acceptance/conftest.py:220`).
- Causa provável (teste, não produto): a janela de 0,3 s é curta demais para um host saturado. O servidor de eco envia um frame a cada 50 ms numa thread do mesmo processo, que também segura o GIL de uvicorn, httpx e das threads de leitura. O log mostra a falha injetada (`3proxy for port 21058 did not become ready`) como esperado; nada indica que B ou C foram tocados.
- Sugestão (T20): `alive()` deveria esperar até ~2 s pelo próximo frame em vez de amostrar 0,3 s fixos.

### F2 — falha conhecida (revisão T09, M2): `test_b_concurrent_api_and_cli` instável
- Bug do teste: o batch com `start_port=ports[39]` pode criar uma porta fora de `allowed_ports`. Não reproduziu nas 3 execuções completas desta rodada. Registrado por orientação do orquestrador; a correção virá em T20.

## Limites medidos (resumo; detalhes em `docs/evidence/limits.md`)
- RSS por 3proxy ocioso ≈ 4,7 MiB, logo 200 proxies ≈ 930 MiB. Cada 3proxy ocioso usa 4 fds.
- Lote de 100 pela CLI: 34–44 s. Lote de 200 pela API: 84 s. Com o host em load 28, esses tempos são limite superior.
- Latência p50 pela API com 200 proxies vivos: create 463 ms, edit 517 ms, rotate 626 ms, delete 224 ms (p95 < 1,1 s).
- `GET /proxies` com 200 proxies: 457 ms.

## Riscos
- Memória escala linearmente (cerca de 4,7 MiB por proxy): 1000 proxies (`PROXY_BATCH_MAX`) ≈ 4,6 GiB de RSS. VPS pequenas precisam de aviso na doc de instalação.
- Lote grande é lento: 200 proxies em ~84 s, ou seja, uma requisição HTTP longa (clientes com timeout curto vão falhar). Medido com supervisor local; com `systemctl` (T14) deve ser mais lento.
- Nada aqui prova IPv6 roteado, reboot real nem systemd real (critério E, T13/T14).
- Rodar a suíte regrava `tests/acceptance/evidence/*.json` (timestamps e PIDs). Não fiz commit dessas regravações.
