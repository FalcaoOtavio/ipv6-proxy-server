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

## Release final (T36)

Data: 2026-10-13. Main `1402bdc05f519a8a09a2119972156215f1bb572b` (merge agent/vm; inclui T29–T35 e o E2E em VM).
Host compartilhado sem root/systemd, 8 CPUs. As suítes locais continuam em loopback com systemctl simulado; o que exige
systemd real foi coberto pela VM (abaixo).

### Suítes locais

| Suíte | Comando | Linha final |
|---|---|---|
| Sem 3proxy | `env -u TEST_3PROXY_BINARY .venv/bin/python -m pytest -q` | `430 passed, 30 skipped, 2 warnings in 71.12s` (skips: falta de `TEST_3PROXY_BINARY`) |
| 3proxy real, execução 1 | `TEST_3PROXY_BINARY=.../.tools/3proxy .venv/bin/python -m pytest -q` | `460 passed, 3 warnings in 155.78s` |
| 3proxy real, execução 2 | idem | `460 passed, 3 warnings in 155.18s` |
| TUI 3x | `.venv/bin/python -m pytest -q tests/test_tui.py` | `28 passed, 1 warning` em 29.10 / 28.70 / 29.02 s |
| Validação da TUI | `TEST_3PROXY_BINARY=... .venv/bin/python tests/tui_validation.py` | `74 ok, 0 falhas` (rc=0; os snapshots regravados não foram commitados, só portas/tempos mudam) |
| shellcheck 0.9.0 | `.tools/sc09-venv/bin/shellcheck install.sh tests/e2e/*.sh` | rc=0, limpo |
| shellcheck 0.11.0 | `.tools/sc11-venv/bin/shellcheck install.sh tests/e2e/*.sh` | rc=0, limpo |
| bash -n | `install.sh ipv6-proxy-server.sh` | rc=0 |
| pip-audit 2.10.1 | `pip-audit -r requirements.lock --require-hashes` | `No known vulnerabilities found` (rc=0) |
| pip-audit 2.10.1 | `pip-audit -r requirements-dev.txt` | `No known vulnerabilities found` (rc=0) |

Nenhuma falha intermitente nas execuções acima.

### Validado em VM real Ubuntu 24.04 (T29–T35)

Detalhes e logs: [e2e/ubuntu-24.04/README.md](e2e/ubuntu-24.04/README.md). QEMU **TCG** (sem KVM), systemd real.

- Upgrade a partir de `aa29685` e de `bdfe9c7`: token/state preservados, state v2 com o mesmo IPv6, unidades por proxy,
  harness A/B/C sem queda dos streams B/C nem troca de PID.
- 2ª execução do `--update` idempotente (hashes e PIDs iguais).
- Reboot REAL (boot_id mudou): API sobe sozinha, mesmos proxies/IPv6/id, cada `/128` restaurado pela própria instância.
- Toggle de `PROXY_BLOCK_PRIVATE_DESTINATIONS` via `--update`: cfg re-renderizada, reinício um a um só dos afetados,
  drop-in `IPAddressDeny` presente/ausente; bloqueio comprovado no cgroup da unidade e no ACL do 3proxy (403), também
  após reboot real.
- Instalação limpa logo após o 1º boot (`--self-test --apt-lock-timeout 3600`), sem intervenção, e reboot real depois.

### Não validado

- Tráfego por IPv6 **roteado** real (sem subnet de provedor; a VM usou ULA em `lo`).
- Debian 12/13 e Ubuntu 22.04/26.04 não executados.
- Tempos da VM não são representativos (TCG, host com load 18–33).
- `--apt-lock-timeout`: com o padrão (600 s) sob TCG o apt do 1º boot não terminou a tempo e o instalador abortou sem
  instalar nada; a espera pelo lock só aparece numa execução contaminada (dois instaladores em paralelo), não numa
  válida.
- `docs/evidence/limits.md` não foi re-medido.

### Novas envs/flags desde a T26

`PROXY_READY_TIMEOUT` (1..300 s, padrão 30), `install.sh --api-timeout S` (10..3600, padrão 180),
`install.sh --apt-lock-timeout S` (0..7200, padrão 600), `POST /proxies/{port}/recover` (admin).

### Bugs achados pelo E2E e corrigidos

| Tarefa | Bug | Status |
|---|---|---|
| T30 | Prontidão fixa de 5 s: self-test/POST falhavam em host lento | corrigido (`PROXY_READY_TIMEOUT`, `--api-timeout`) |
| T32 | `systemctl start` estourava 30 s na migração; porta `fenced` (503) com instalador reportando sucesso | corrigido (prazo derivado, adoção, `/recover`) |
| T34 | Toggle do bloqueio via `--update` não re-renderizava cfg nem reiniciava: falso "protegido" | corrigido e verificado em VM |
| T35 | Instalação limpa pós-1º boot falhava no lock do apt | corrigido (`--apt-lock-timeout`) |

### Publicação

Push e PR **não executados**: aguardam autorização (#152). Comandos propostos em [PR_PROPOSAL.md](../PR_PROPOSAL.md);
artefatos em `dist/` (fora do git) gerados a partir do commit desta seção.

O projeto **não** é declarado 100% funcional: ver pendências acima.
