# QA final (T26), main cp-0021

Data: 2026-10-12. Main `6d9bd37` (merge agent/tuifix, inclui T23 UDP/SSRF, drop-in `IPAddressDeny`, F01/F02 da TUI). Host compartilhado, 8 CPUs, load ~15.

**Escopo de toda a evidência local:** loopback (`127.0.0.1`, `::1`, v4-mapped), **sem systemd real** (systemctl é simulado nos testes de instalador e backend) e **sem IPv6 roteado**. O critério E (VM real, reboot, IPv6 roteado) só foi verificado em dry-run.

## Suítes

| Suíte | Comando | Resultado |
|---|---|---|
| Sem 3proxy | `.venv/bin/python -m pytest -q -rs` | 397 passed, 29 skipped (todos por falta de `TEST_3PROXY_BINARY`), 205 s |
| Com 3proxy real, execução 1 | `TEST_3PROXY_BINARY=.../.tools/3proxy .venv/bin/python -m pytest -q` | 426 passed, 390 s |
| Com 3proxy real, execução 2 | idem | 426 passed, 352 s |
| Com 3proxy real, execução 3 | idem | 426 passed, 288 s |
| Aceite 5x | `TEST_3PROXY_BINARY=... pytest -q tests/acceptance` | 5/5, 12 passed cada (65–69 s) |
| TUI 5x | `pytest -q tests/test_tui.py` | 5/5, 28 passed cada (66–79 s) |
| shellcheck 0.9.0 (`.tools/sc09-venv`) | `shellcheck install.sh tests/e2e/vm_e2e.sh` | rc=0, limpo |
| shellcheck 0.11.0 (`.tools/sc11-venv` e `.tools/sc-venv`) | idem | rc=0, limpo |
| bash -n | `install.sh`, `ipv6-proxy-server.sh`, `tests/e2e/vm_e2e.sh` | OK |
| vm_e2e dry-run | `tests/e2e/vm_e2e.sh --dry-run --phase pre-reboot\|post-reboot\|no-reboot` | rc=0 nas 3 fases: 9, 2 e 8 passos; `--phase bogus` dá rc=2 com mensagem |
| pip-audit 2.10.1 | `pip-audit -r requirements.lock --require-hashes` | No known vulnerabilities found (rc=0) |
| pip-audit 2.10.1 | `pip-audit -r requirements-dev.txt` | No known vulnerabilities found (rc=0) |

Avisos não bloqueantes: `StarletteDeprecationWarning` (httpx em `starlette.testclient`) e `DeprecationWarning` de `pty.fork` multi-thread em `test_tui_pty.py`. Nenhum afeta o produto.

## Tabela final de achados

| Item | Repro curto | Resultado |
|---|---|---|
| A1: proxy caído derrubava `GET /proxies`, o healthcheck e a CLI | `test_include_status_reports_each_proxy_without_503`, `test_unrelated_fenced_proxy_does_not_roll_back_update`, `test_get_one_reports_fenced` passam com 3proxy real (3x) | **RESOLVIDO** |
| A2: `die` na transação não fazia rollback | `test_die_inside_transaction_rolls_back`, `test_rollback_never_runs_in_a_subshell` | **RESOLVIDO** (systemctl simulado) |
| A3: falha no meio do rollback deixava a API parada | `test_rollback_keeps_going_and_always_starts_old_api` | **RESOLVIDO** (systemctl simulado) |
| A4: unidade inativa reconciliada virava falso "reiniciado" | `test_restarted_unchanged_flags_only_same_config_new_pid` | **RESOLVIDO** |
| A5: prazo fixo de 45 s e reload do PID1 por porta | `test_api_deadline_grows_with_proxy_count`; `test_backend.py` confere `enable/disable --no-reload` | **RESOLVIDO** (tempo real com systemd só na VM) |
| A6: exceção na TUI exibia senhas (`show_locals`) | `test_a6_export_to_unknown_user_home_shows_generic_error`, `test_a6_unexpected_exception_in_action_is_generic`, `test_a6_fatal_error_never_renders_locals` (5x) | **RESOLVIDO** |
| A7: retry sem Idempotency-Key duplicava o lote | `test_batch_retry_with_same_key_does_not_duplicate`, `test_timeout_message_carries_the_key_and_retry_reuses_it`, `test_a7_mutation_gives_up_after_one_retry`, `test_crash_after_*` | **RESOLVIDO** (limitação Q2 mantida, abaixo) |
| S1: SSRF TCP para o loopback do host | `test_proxies_refuse_host_loopback` (HTTP e SOCKS5, com 3proxy real) | **RESOLVIDO para TCP** (resolução de nome para `::1` não testável neste host; backstop `IPAddressDeny` exige systemd real) |
| N1: SSRF via SOCKS5 UDP ASSOCIATE | Controle manual com 3proxy real e `render_config()`: bloqueio **desligado** dá REP `05 00` e o datagrama chega ao eco em `[::1]` (`[b'ping']`); bloqueio **ligado** (padrão) não dá REP e nada chega (`[]`). `test_security_udp.py` (3 testes) passa 3x | **RESOLVIDO** (ACL `deny * * * * UDPASSOC`; drop-in de kernel só na VM) |
| Q1: `shellcheck install.sh` falhava com 0.9/0.10 | `.tools/sc09-venv/bin/shellcheck install.sh tests/e2e/vm_e2e.sh` dá rc=0 | **RESOLVIDO** (0.10 não re-testado: não há binário local; 0.9 e 0.11 limpos) |
| F01: dados da API interpretados como markup/emoji na TUI | `test_f01_data_is_never_parsed_as_markup_or_emoji` (ex.: `falhou [bold]x[/bold] [@click=app.quit]y[/]` exibido literal) 5/5 | **RESOLVIDO** |
| F02: rodapé cortava atalhos | `test_f02_footer_shows_essential_keys` em 60x20 e 80x24, `test_f02_help_footer_and_escape` 5/5 | **RESOLVIDO** |
| Flakes | 3 execuções completas com 3proxy real, 5 de aceite e 5 de TUI: nenhuma falha intermitente | **NÃO há flakes** nesta rodada |

## Pendências informativas

### Q2: replay idempotente de batch `--random` não devolve as senhas
Por desenho (`docs/CLI.md`): retry com a mesma chave devolve o resultado guardado, sem senhas. Se a primeira resposta se perder, é preciso editar a senha de cada proxy. Não é regressão.

## Riscos restantes
- Critério E (VM real com systemd, reboot, IPv6 roteado, `IPAddressDeny` via cgroup/BPF) não coberto localmente; só dry-run do `vm_e2e.sh`.
- Limites em `docs/evidence/limits.md` não foram re-medidos nesta rodada.
