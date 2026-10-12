# Re-QA final pós-correções (T21)

Data: 2026-10-12. Main `5fd8149` (cp-0015, T17–T20). Host compartilhado, 8 CPUs, load ~20–28.
Escopo de rede de toda a evidência local: loopback v4-mapped / `::1`. **Não é IPv6 roteado.** Reboot e systemd reais ficam para T14.

## Suítes

| Suíte | Comando | Resultado |
|---|---|---|
| Sem 3proxy | `.venv/bin/python -m pytest -q -rs` | 383 passed, 26 skipped (todos os que exigem `TEST_3PROXY_BINARY`) |
| Com 3proxy real, execução 1 | `TEST_3PROXY_BINARY=.../.tools/3proxy .venv/bin/python -m pytest -q` | 409 passed (433 s) |
| Com 3proxy real, execução 2 | idem | 409 passed (405 s) |
| Com 3proxy real, execução 3 | idem | 409 passed (411 s) |
| Aceite A/B 5x | `pytest -q tests/acceptance` | 5/5, 12 passed cada |
| Critério C e limites | `tests/acceptance/measure_limits.py` | todas as verificações PASS; ver `docs/evidence/limits.md` |
| pip-audit | `pip-audit -r requirements.lock --require-hashes` (2.9.0) | No known vulnerabilities found |
| bash -n | `install.sh`, `tests/e2e/vm_e2e.sh` | OK |
| shellcheck 0.11.0 (`.tools/sc-venv`, usado por `test_shellcheck_clean`) | `shellcheck install.sh tests/e2e/vm_e2e.sh` | limpo |
| shellcheck 0.10.0 / 0.9.0 | idem | **install.sh rc=1** (ver Q1); `vm_e2e.sh` limpo |
| Dry-run do vm_e2e | `--dry-run --phase pre-reboot\|post-reboot\|no-reboot` (`--reboot`, fase inválida) | 9, 2 e 8 passos; `step reboot`; rc=2 com mensagem |

Nenhum teste instável nesta rodada: as 3 execuções completas e as 5 de aceite passaram.

## Achados da revisão: verificação prática

| Item | Verificação (repro) | Resultado |
|---|---|---|
| A1: proxy caído derrubava `GET /proxies`, o healthcheck e a CLI | API real e 3proxy real: dois proxies criados, `kill -9` no 3proxy de um deles. `GET /proxies` dá 503, mas `GET /proxies?include=status` dá 200 com `failed` naquela porta. `ipv6-proxy list` e `export` (rc 0) listam os 2 e avisam da porta fora do ar. `wait_api` do instalador usa `?include=status`. `test_unrelated_fenced_proxy_does_not_roll_back_update` passa. | **RESOLVIDO** |
| A2: `die` dentro da transação não fazia rollback | `install.sh` como biblioteca, `begin_tx update` e então `die`: rollback executado (stop da API, disable da instância, restore, start da API), rc 1. Com erro de `set -e` (`false`), também faz rollback e informa a linha. | **RESOLVIDO** |
| A3: falha no meio do rollback deixava a API parada | Mesma repro com `restore_installation` falhando: `FALHOU: restaurar arquivos do backup` e o rollback segue até `systemctl start ipv6-proxy-api.service`. | **RESOLVIDO** |
| A4: unidade inativa reconciliada era um falso "reiniciado" | `restarted_unchanged` com antes `30001 0 aaa` / depois `30001 555 aaa` não marca 30001; `30003 222 ccc` / `30003 333 ccc` é marcado (esperado). | **RESOLVIDO** |
| A5: prazo fixo de 45 s e reload do PID1 por porta | `api_deadline`: 0 proxies dá 45 s, 10 dá 95 s, 300 dá 1545 s. O supervisor usa `enable --no-reload` só quando a unidade não está habilitada (`supervisor.py:69-73`). | **RESOLVIDO** (prazo proporcional; o tempo real com systemd só se mede na VM, T14) |
| A6: exceção na TUI mostrava senhas (`show_locals`) | TUI real (Pilot) contra a API real, export para `~usuario-que-nao-existe-qa/x.txt`: o status mostra "caminho inválido", a app continua rodando e não exibe senha. `_guarded` captura qualquer `Exception` com mensagem genérica, e `_fatal_error` substitui o traceback do Textual. | **RESOLVIDO** |
| A7: sem Idempotency-Key, retry após timeout duplicava o lote | Batch de 5 com timeout de cliente de 0,05 s (expira), depois retry com a mesma `Idempotency-Key`: 201, 5 criados no total (sem duplicar). CLI e TUI mandam uuid4 por invocação e o timeout escala 5 s por proxy. | **RESOLVIDO**, com a limitação documentada abaixo (Q2) |
| S1: SSRF para o loopback do host | 3proxy real com a config de `render_config()` e destino `[::1]` (servidor só em `::1`). Bloqueio ligado: HTTP 403, SOCKS5 recusado (curl rc 97), em HTTP e SOCKS5, com e sem senha. Bloqueio desligado (controle): 200 em todos. `measure_limits` com o padrão de produção: lote de 200 com `deny` no cfg e loopback recusado com 403. `localhost`/`ip6-localhost` resolvem só para 127.0.0.1 neste host, que é inalcançável com `-6` (502 nos dois modos); a resolução por nome em IPv6 não pôde ser testada aqui. | **RESOLVIDO para TCP** |
| N1: SSRF via SOCKS5 UDP ASSOCIATE (T22) | Não re-testado aqui: a correção é T23. Detalhes em `docs/reviews/T22-security.md`. | **ABERTO** (re-teste após T23) |

## Falhas / pendências

### Q1: `shellcheck install.sh` falha com shellcheck 0.9/0.10 (o CI usa o da imagem)
- Reprodução: `shellcheck install.sh` com 0.10.0 ou 0.9.0, rc=1. `SC2120`: `wait_api` usa `${1:-...}`, mas nenhuma chamada passa argumento (`install.sh:585`). `SC2119` nas chamadas (`install.sh:601,695,826,863`).
- Com 0.11.0 (`.tools/sc-venv`, o binário que `test_shellcheck_clean` usa) passa limpo. Por isso a suíte não acusa.
- Impacto: `.github/workflows/tests.yml` roda `shellcheck install.sh tests/e2e/vm_e2e.sh` com o shellcheck do runner (imagem ubuntu-24.04 do GitHub = 0.9.0-1, conferido no README do runner-images), e esse passo falha.
- Correção sugerida (produto/inst): remover o parâmetro não usado de `wait_api` ou passá-lo explicitamente; ou fixar a versão do shellcheck no CI.

### Q2 (informativo): replay idempotente de batch `--random` não devolve as senhas
- Um retry com a mesma chave devolve o resultado guardado, sem senhas, por desenho (`manager.py:155-163`; documentado em `docs/CLI.md:38`). Se a primeira resposta se perder por timeout, as senhas aleatórias desse lote ficam irrecuperáveis pela API; é preciso editar a senha de cada proxy. O timeout proporcional (5 s/proxy) torna isso raro. Não é regressão.

## Limites (resumo de `docs/evidence/limits.md`)
- RSS por 3proxy ≈ 4,7 MiB, com ou sem SSRF. 200 proxies ≈ 930 MiB. 4 fds por 3proxy ocioso.
- Lote de 200 pela API: 46 s com o bloqueio desligado e 48 s com o padrão de produção. Lote de 100 pela CLI: 27–39 s.
- Latência p50 pela API com 200 proxies vivos: create 417 ms, edit 283 ms, rotate 549 ms, delete 267 ms.

## Riscos restantes
- N1 aberto (gate de segurança não passa até T23).
- Q1 deve quebrar o passo de lint do CI no runner.
- Critério E (VM real, reboot, IPv6 roteado) não coberto localmente.
