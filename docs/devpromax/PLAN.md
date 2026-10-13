# Plano — IPv6 Proxy Server Independente

## Resumo
Evoluir o fork `FalcaoOtavio/ipv6-proxy-server` para proxies IPv6 **independentes por porta** (um processo 3proxy por proxy, sob unidade systemd template), edição real,
criação em lote pela CLI com exportação, TUI PT-BR e autoinstalador com atualização/migração. Entrega = branch
`dpm/independent-proxies` no MESMO repo + proposta de PR para `main` + SHA final + zip. Sem deploy SaaS.

### SHAs (ADR-0001)
- main (base histórica): `aa2968556f861f56c7e5d37c70a859797a8d8772` (2026-09-18)
- **origem**: `ashford/isolated-mutations` = `bdfe9c7634691f51e893f6e53b6137f95792e4f5` (2026-10-05; 4 à frente / 0 atrás de main)
- final: a registrar na entrega. Branch original e main nunca são reescritas; Ashford Hub não é alterado.

### Publicação (gate)
Sem push público/merge em main/release/novos gastos/VPS sem autorização específica. Push da branch dedicada no fork (público)
é necessário para o CI E2E (systemd/instalador) — pedido de autorização enviado; até lá, trabalho só local.
`dpm github create-repo` NÃO será usado (setaria remote e faria push do main do DPM no fork).

## Escopo
Dentro: MVP 1–6 do BRIEF (independência, edição, lotes CLI, TUI, instalador, docs), testes A–F.
Fora: contratos Ashford alterados (id/If-Match/Idempotency-Key/owner/capabilities são preservados e testados), dashboard web, pagamentos, multi-tenant, Coolify/Railway, túneis, WhatsApp, rotação automática por intervalo
(`-r` do upstream) e `--rotate-every-request` (documentados como "não aplicáveis/não implementados" no mapa de equivalência),
qualquer escrita na branch `ashford/isolated-mutations`.

## Ambiente e restrições verificadas
- Host DPM: sem root, sem docker daemon, sem KVM/userns. Agentes rodam unit tests + 3proxy real em loopback (`-e ::1`).
- E2E com systemd/`ip -6 addr`/instalador: GitHub Actions do fork (runners Ubuntu 22.04/24.04 = VM limpa com systemd e sudo).
  Debian 12/13: tentativa via container systemd privilegiado no runner; se inviável, declarar "não testado".
- IPv6 roteado real: **indisponível** (runners sem IPv6 externo; nenhuma VPS fornecida). Testes usam subnet ULA `fd00:1234::/64`
  em `lo` com origem IPv6 real por proxy contra servidor local. Limitação declarada; nunca alegar teste em rede real.
- Reboot real e IPv6 roteado: **critérios NÃO satisfeitos** até execução em ambiente real (T14). Reboot simulado e ULA em loopback
  são testes complementares rotulados como tal, nunca substitutos. Busca de ambiente descartável sem custo feita em 2026-10-11:
  host sem docker daemon/KVM/userns/sudo; nenhum worker host alternativo no DPM; Actions exige push (gate #152). Projeto NÃO
  será declarado 100% funcional nem concluído enquanto T14 não passar.

## Arquitetura e stack (preservada: Python 3 / FastAPI / 3proxy 0.9.4 / systemd)
- **Supervisor por proxy (ADR-0002)**: unidade template `ipv6-proxy@<porta>.service` (3proxy foreground, config própria
  `/var/lib/ipv6-proxy-api/proxies/<porta>.cfg` 0600, `Restart=on-failure`, logs por unidade no journal, `PartOf` nada coletivo).
  A API só cria/para/reinicia a instância alvo. Reiniciar/atualizar a API **não** derruba proxies (processos fora do cgroup da API).
  Boot: instâncias habilitadas + reconciliação na partida da API (adota unidades vivas, sobe as faltantes, nunca reinicia as saudáveis).
- Interface `Supervisor` (systemd em produção; `LocalProcessSupervisor` com `start_new_session` para testes sem root).
- **Endereços**: journal de propriedade por proxy; `ip -6 addr add <ip>/128 nodad` só de endereços próprios; remoção só dos próprios;
  recusa endereço já presente de terceiro. Nada de flush/killall/pkill.
- **Herdado de bdfe9c7**: rollback com fencing por porta, `id` imutável + `If-Match`, `Idempotency-Key` atômica em `state.json` v2,
  owner scoping (`PROXY_CLIENT_TOKENS_FILE`), `/capabilities`. Lacunas que corrigimos: processos 3proxy no cgroup da API
  (restart/upgrade da API derruba todos), log único `3proxy.log`, readiness só por porta aberta, teste quebrado no CI.
- **Manager**: transação por alvo (create/edit/rotate/delete/lote) com rollback restrito aos alvos; estado `broken` por proxy, não global.
  Persistência atômica (`proxies.json`) + lock de arquivo + RLock; API é o único gerenciador (CLI/TUI são clientes HTTP locais).
- **API**: endpoints e headers existentes mantidos (main + ashford); novos: `PATCH /proxies/{port}` (credenciais/protocolo/porta,
  com If-Match/Idempotency-Key), `POST /proxies/batch`, `GET /proxies/{port}`, `status` por proxy na view (campo aditivo) e
  capability nova anunciada em `/capabilities`.
- **CLI** `ipv6-proxy` (stdlib, script único): novos `batch`/`create --count`, `edit`, `export`; mapa de equivalência com upstream.
- **TUI** `ipv6-proxy-tui` (ADR-0003: Textual fixado, roda no venv da instalação), PT-BR, cliente da mesma API.
- **Instalador** `install.sh`: instalação limpa, `--update` (migração da versão base, backup + rollback), idempotente,
  `--ref` (checkout de uma revisão; SHA gravado em `/opt/ipv6-proxy-server/VERSION`), `--cli-only`/`--tui-only` sem tocar proxies.

## Módulos
`proxy_api/{config,models,backend,supervisor,manager,app}.py` · `ipv6-proxy` (CLI) · `proxy_tui/` · `deploy/*.service` ·
`install.sh` · `tests/` · `.github/workflows/tests.yml` · `docs/{API,INSTALL,CLI,TUI,TROUBLESHOOTING,MIGRATION}.md`.

## Modelo de dados (herdado de bdfe9c7 — fonte de verdade)
- `state.json` **v2** `{version: 2, proxies: [...], idempotency: {...}}` é a única fonte de verdade. `proxies.json` (schema main)
  é lido SOMENTE como fonte legada na migração (renomeado para `proxies.json.v1-backup`); nunca voltar a gravá-lo.
- ProxyRecord efetivo: `{id (imutável, hex 32), owner?, port, protocol http|socks5, username?, password?, ipv6}`; edição preserva `id`
  e `owner`; mudança de porta preserva `id`. Campos novos só aditivos e com default (compatíveis com v2).
- Idempotência e ownership mantidos para todas as mutações novas (PATCH, batch).
- Por porta: config `proxies/<porta>.cfg` 0600, endereço próprio no journal `owned-addresses.json`.
- Settings via env real existente (`PROXY_IPV6_SUBNET, PROXY_BIND_IPV4, PROXY_INTERFACE, PROXY_API_TOKEN, PROXY_DATA_DIR,
  PROXY_3PROXY_BINARY, PROXY_PORT_START, PROXY_PORT_END, PROXY_CLIENT_TOKENS_FILE`); novos nomes só se necessários e documentados em
  `deploy/ipv6-proxy-api.env.example`.
- Lote: requisição {count, start_port?, protocol, credentials: common|random|none, username?, password?} → resultado
  {created[], failed[], rolled_back}; atomicidade definida em T03 (tudo-ou-nada por padrão, só os novos são tocados).

## Tarefas registradas (DAG real: `dpm task list`)
| ID | Título | Papel | Pts | Depende | Write set exclusivo |
|---|---|---|---|---|---|
| T01 | Baseline: importar fork (todas as refs) em bdfe9c7, corrigir teste quebrado, reservar requirements (textual, pexpect pinados), 3proxy local | dev | 3 | — | requirements*.txt, tests/conftest, .gitignore |
| T02 | Supervisor por proxy (systemd template + local), config/log por porta, readiness = PID alvo dono do socket, endereços próprios, reconciliação/boot | dev | 13 | T01 | proxy_api/backend.py, proxy_api/supervisor.py, deploy/ |
| T03 | Contratos: PATCH, POST /proxies/batch, GET /{port}, status, compensação de dupla falha, idempotência/ownership nas novas mutações | dev | 8 | T02 | proxy_api/manager.py, app.py, models.py, config.py, docs/API.md |
| T04 | Harness de aceite A/B real (3proxy real, streams HTTP CONNECT/SOCKS5, PIDs, falhas injetadas, concorrência, reuso de porta) | tester | 8 | T03 | tests/acceptance/ |
| T05 | CLI: batch/export/random/interativo/edit + mapa Temporalitas | dev | 8 | T03 | ipv6-proxy, tests/test_cli*.py, docs/CLI.md |
| T06 | TUI Textual PT-BR + testes Pilot/pty | dev | 13 | T03 | proxy_tui/, ipv6-proxy-tui, tests/test_tui*.py, docs/TUI.md |
| T07 | Instalador final (limpo/update/migração main e bdfe9c7/rollback/idempotente/--ref SHA/--cli-only/--tui-only) | dev | 13 | T03 | install.sh, tests/test_install*.py, docs/INSTALL.md, docs/MIGRATION.md |
| T08 | Rodada QA completa da sprint 2 + registro de falhas | tester | 5 | T04,T05,T06,T07 | tests/ (só novos testes de QA) |
| T09 | Revisão independente de código (diff bdfe9c7..main) | reviewer | 5 | T04,T05,T06,T07 | — (só report) |
| T10 | Auditoria de segurança | security | 5 | T04,T05,T06,T07 | — (só report) |
| T11 | Validação da TUI em pty, feature por feature (80x24 e 60x20) | validator | 5 | T08 | evidências em docs/evidence/tui/ |
| T12 | Docs: README, TROUBLESHOOTING, limites medidos (100 HTTP + 100 SOCKS5) | dev | 5 | T08 | README.md, docs/TROUBLESHOOTING.md |
| T13 | Preparar E2E de VM: script `tests/e2e/vm_e2e.sh` + workflow Actions (não executado sem autorização) | tester | 5 | T07 | tests/e2e/, .github/workflows/ |
| T14 | **GATE**: executar E2E em Linux limpo com systemd (instalação, upgrade, 2ª execução, reboot REAL, IPv6 roteado) | tester | 8 | T13 | — |
| T15 | Entrega local: branch dpm/independent-proxies sobre bdfe9c7, bundle, SHA final, proposta de PR; push/PR = **GATE** #152 | dev | 2 | T09,T10,T11,T12 | — |
Correções de QA/revisão/segurança viram tarefas novas (com pontos) e reabrem os critérios afetados; xfail/skip nunca contam como aprovação.
Arquivos compartilhados: requirements*.txt só T01 (pedidos posteriores passam pelo orquestrador); deploy/ só T02; docs/ por arquivo conforme tabela.

## Matriz de aceite (BRIEF A–F)
| Critério | Evidência exigida | Onde | Tarefa |
|---|---|---|---|
| A independência | ≥3 proxies; streams HTTP CONNECT e SOCKS5 ativos em B/C durante create/edit A/rotate A/delete A; PID, bytes, IPv6 de origem e config de B/C inalterados; nenhum stop/reload coletivo (auditoria de chamadas) | local 3proxy real + runner systemd | T04, T08 |
| B falhas/concorrência | falha injetada em bind, start, persistência, rollback e dupla falha; B/C sobrevivem; API/CLI/TUI concorrentes sem porta/IPv6 duplicado; DELETE com If-Match após reuso de porta | local + runner | T03, T04 |
| C lotes | 100 HTTP + 100 SOCKS5, credenciais comuns/aleatórias/sem, faixa correta, export IPv4:porta:user:senha 0600 usável por curl, visível em API/TUI, falha parcial documentada | local + runner | T05, T08 |
| D TUI | todos os fluxos F01–F08 em pty real, capturas | local | T06, validação |
| E instalador | VM Linux limpa com systemd: instalação, upgrade de aa29685 e de bdfe9c7, 2ª execução idempotente, **reboot REAL**, dados/token preservados, CLI/TUI/API, tráfego HTTP/SOCKS5 por **IPv6 roteado real** | VM/runner (gate) | T07, T13, T14 |
| F regressões/entrega | suíte antiga + ashford verde, avisos obsoletos removidos com evidência, branch, SHA final, docs, zip, proposta de PR | local + runner | T09, entrega |

## Sprints
1. **Núcleo independente** (T01–T04): pronto quando A/B passam localmente com 3proxy real (processos por porta, PIDs de B/C intactos).
2. **Interfaces e instalador** (T05–T07): pronto quando lote/edição pela CLI e TUI funcionam contra a API e o instalador passa testes unitários.
3. **E2E e entrega** (T08, T09, QA, segurança, validação TUI, PR): pronto quando CI E2E verde no fork, auditoria ok, features TUI validadas, PR aberto.

## Estratégia de testes
Unit (pytest, backend mockado) · integração com 3proxy real em loopback (PIDs, conexões persistentes, falhas injetadas) ·
E2E no GitHub Actions com systemd (instalação limpa, upgrade de `aa29685`, segunda execução, reboot simulado, CLI/TUI/API,
tráfego HTTP e SOCKS5 com origem IPv6 ULA) · TUI via Textual Pilot + pty real (pexpect) com capturas SVG/texto.
Testes antigos que pressupõem restart global são atualizados; avisos obsoletos (`MUTATION_WARNING`) removidos só após evidência.

## Segurança
Ameaças: vazamento de token/senhas (logs, argv, `ps`, arquivos), injeção em config 3proxy, matar processos/endereços de terceiros,
API exposta. Controles: bind 127.0.0.1, Bearer com compare_digest, senha via stdin/prompt sem eco, arquivos 0600 (env, configs,
exportações), validação estrita de usuário/senha/porta, unidades por proxy com hardening, só `ip addr` de endereços do journal,
nada de pkill/killall. Varreduras: auditoria `security` (OWASP/segredos/deps), shellcheck, pip-audit.

## UX (TUI)
Validação em pty com dimensões 80x24 e 60x20 (não browser). Respostas assíncronas: a operação é ligada ao `id`+porta selecionados
no momento da ação (If-Match), navegação/cancelamento durante a requisição nunca redirecionam a operação a outro proxy.
Textual, PT-BR, teclado (atalhos visíveis + ajuda `?`), tabela porta/protocolo/IPv6/status (texto + símbolo, não só cor),
diálogos criar/lote/editar/rotacionar/excluir (confirmação digitando a porta), exportar, senhas mascaradas, estados vazio/erro/carregando,
funciona em 80x24 e degrada em terminal pequeno. Sem identidade visual nova.

## Deploy
Auto-hospedado via instalador. Nada de Coolify/Railway. Entrega: branch `dpm/independent-proxies` no fork + PR para `main`.

## Riscos
- Sem IPv6 roteado nem VPS: validação real fica como pendência pós-entrega documentada.
- Push da branch (público) não autorizado ainda: sem ele, E (instalador/systemd) fica sem evidência e vira gate.
- Contratos Ashford: qualquer mudança quebra o cliente Go do Ashford; regressão obrigatória.
- Debian em container systemd no runner pode falhar: declarar não testado.
- Carga do host DPM alta: limitar a ≤3 agentes simultâneos.

## Paralelismo
Sprint 1: 1 dev (T01→T02→T03) + tester (T04). Sprint 2: 3 devs (T05, T06, T07). Sprint 3: tester (T08) + dev docs + security + validator.
