# Mapa do projeto — IPv6 Proxy Server Independente

> Arquitetura real em `main` 47f5d83 (base `bdfe9c7`). Revisão T09 (2026-10-11) + atualização final 2026-10-12 (T17–T28).

## Visão geral
```
 ipv6-proxy (CLI stdlib) ─┐                 ┌─ ipv6-proxy@30000.service ─ 3proxy  proxies/30000.cfg
 ipv6-proxy-tui (Textual) ─┼─ HTTP 127.0.0.1:8000 ─ ipv6-proxy-api.service ─┼─ ipv6-proxy@30001.service ─ 3proxy  proxies/30001.cfg
 Ashford (Go, Bearer)  ────┘   (FastAPI, 1 worker)   systemctl <verbo> <1 unidade> └─ ...
```
- Um processo 3proxy **por porta**, cada um em sua unidade systemd template, fora do cgroup da API. Sem `PartOf`/`BindsTo`: reiniciar a API não derruba proxies.
- A API é o único gerenciador (lock `flock` em `manager.lock` + `RLock`). CLI e TUI são clientes HTTP locais com o token de `/etc/ipv6-proxy-api.env`.
- Toda mutação toca só a porta-alvo: config, instância e endereço `/128` próprios.

## Módulos
| Caminho | Papel |
|---|---|
| `proxy_api/config.py` | `Settings` via env `PROXY_*` (subnet, bind_ipv4, interface, token, data_dir, binary, faixa de portas, client tokens, `PROXY_SUPERVISOR` systemd/local, `PROXY_BATCH_MAX`) |
| `proxy_api/models.py` | `ProxyCreate`, `ProxyUpdate` (PATCH), `BatchCreate`, `ProxyRecord` (`id` imutável hex32), views `ProxyView`/`ProxyStatusView` |
| `proxy_api/manager.py` | `Manager`: estado `state.json` v2 (proxies + idempotência num rename atômico), planos `plan_create/update/rotate/delete/batch`, `_commit` por porta alterada (novos/editados primeiro, remoções por último), `_undo` por alvo, fencing por porta (`broken_ports`) ou global (`broken`, só se o disco falhar) |
| `proxy_api/backend.py` | `LinuxBackend`: `provision/replace/deprovision` de um alvo; `_claim/_release` de endereço só via journal `owned-addresses.json`; readiness = porta aceita conexão **e** o MainPID do alvo é dono do socket LISTEN (`/proc/net/tcp*` + `/proc/<pid>/fd`); `reconcile` na partida (adota, reinicia só divergentes, para órfãos); `statuses` |
| `proxy_api/supervisor.py` | `SystemdSupervisor` (`enable --now`/`disable --now`/`restart` de `ipv6-proxy@<porta>`, `list-units` só leitura) e `LocalProcessSupervisor` (dev/testes: processo destacado, pidfile com starttime anti-reuso de PID, parada por SIGINT e depois SIGKILL) |
| `proxy_api/instance_address.py` | `ExecStartPre=+` da instância: recoloca o próprio `/128` no boot se ele estiver no journal; recusa endereço alheio |
| `proxy_api/app.py` | Rotas, auth Bearer (admin com `compare_digest`; client tokens por hash → owner), `Idempotency-Key`, `If-Match` |
| `ipv6-proxy` | CLI: `list`, `create`, `rotate`, `delete`, `batch`, `edit`, `export`; `Parser` sem abreviação nem eco de argumentos; senha por prompt sem eco ou `--password-stdin`; `OutputFile` 0600 (`O_EXCL`, ou `mkstemp` + `replace` com `--force`) |
| `proxy_tui/` | `client.py` (urllib, sem proxy nem redirect), `app.py` (`ProxyTui`; atalhos `? c l e r d x s a q`), `screens.py` (diálogos; excluir exige digitar a porta). Mutações fixadas por `id`+porta com `If-Match` |
| `deploy/ipv6-proxy@.service` | Template: 3proxy em foreground, `KillSignal=SIGINT`, `TimeoutStopSec=3`, `Restart=on-failure`, hardening, `CapabilityBoundingSet=` vazio |
| `deploy/ipv6-proxy-api.service` | API uvicorn em 127.0.0.1:8000, `CAP_NET_ADMIN` (para `ip addr`) |
| `install.sh` | Modos: instalação limpa, `--update` (migração de aa29685/bdfe9c7 com backup + rollback), `--cli-only`, `--tui-only`, `--ref`, `--self-test`, `--yes`. Grava `VERSION` (SHA) |
| `tests/` | unit (backend mockado/fake), 3proxy real em loopback (`TEST_3PROXY_BINARY`), `acceptance/` (critérios A/B com evidências JSON), `e2e/vm_e2e.sh` (só em VM/runner; não executado) |

## Rotas (contrato `ashford.proxy/1`)
| Método | Rota | Notas |
|---|---|---|
| GET | `/capabilities` | `isolated_mutations, owner_scoping, idempotency_keys, conditional_mutations, socks5_udp_associate, edit, batch, per_proxy_status` |
| GET | `/proxies` | Legado: 503 se algum proxy visível não estiver saudável |
| GET | `/proxies?include=status` | Status por item: `running`, `starting`, `failed` ou `fenced` (nunca 503 por proxy) |
| GET | `/proxies/{port}` | Um proxy com `status` |
| POST | `/proxies` | 201; `Idempotency-Key` |
| POST | `/proxies/batch` | `count`, `start_port`, `protocol`, `credentials` (none/common/random), `partial`. Retorna 201; 207 com `partial`; 503 tudo-ou-nada. Senhas aleatórias só na 1ª resposta |
| PATCH | `/proxies/{port}` | Credenciais/protocolo/porta; preserva `id`, `owner` e `ipv6`; `If-Match` + `Idempotency-Key` |
| POST | `/proxies/{port}/rotate` | Troca só o IPv6; `If-Match` |
| DELETE | `/proxies/{port}` | 204; chave pinada se não houver `If-Match` |

## Fluxos
- **Mutação**: `Manager.mutate`, depois `plan_*` (sem efeito), `_commit` (para cada porta alterada, `backend.provision/replace/deprovision`) e `_write(state.json)` (ponto de commit).
  - Em caso de falha: `_undo` só das portas feitas; dupla falha → fence da porta.
  - Mudança de porta: a nova instância sobe antes de a antiga parar; o IPv6 é compartilhado e liberado só no fim.
- **Partida da API**: `Manager.__init__` executa `backend.reconcile(records)`.
  - Unidades saudáveis com config igual são adotadas sem reinício; as que divergem são reiniciadas; as que faltam sobem; unidades `ipv6-proxy@*` fora do estado são paradas.
  - Uma porta que falha fica `fenced`.
- **Boot**: as instâncias `enabled` sobem sozinhas. O `ExecStartPre` recoloca o `/128` próprio; a API reconcilia depois.
- **Update** (`install.sh --update`): backup, `apply_release` e restart **só** da API, seguidos de `healthcheck`. Se uma porta com config igual teve o PID trocado, o instalador faz rollback.
  - Migração da base: stop/start da API é a única interrupção coletiva, explícita e confirmada.

## Dados (`/var/lib/ipv6-proxy-api`, 0700)
`state.json` (v2, fonte de verdade) · `owned-addresses.json` · `proxies/<porta>.cfg` e `.addr` (0600) · `manager.lock` · `proxies.json.v1-backup` (migração) · backups do instalador em `/var/backups/ipv6-proxy-*`.

## Limitações conhecidas (ver ISSUES/T09)
IPv6 roteado, reboot real e instalador em VM ainda não executados (gate T14, ISS-001). Achados da revisão T09 estão no report do DPM.

## Atualização final (2026-10-12, main 47f5d83)
- Anti-SSRF (ADR-0007, T18/T23): `render_config` com `auth iponly|strong` + `deny` de `::/128 ::1/128 ::ffff:0:0/96 fe80::/10 fc00::/7 ff00::/8 <subnet>` e
  `deny UDPASSOC` quando `PROXY_BLOCK_PRIVATE_DESTINATIONS=true` (padrão); `/capabilities` só anuncia `socks5_udp_associate` com bloqueio off.
  Drop-in `ipv6-proxy@.service.d/50-block-private.conf` (`IPAddressDeny` IPv6) instalado só com bloqueio on.
- Órfãos (ADR-0006): `pending-ports.json` (intenção durável); partida da API nunca para instância desconhecida sem intenção; `orphans.json` para limpeza manual.
- Supervisor: `enable --no-reload` só se necessário; reconcile linear (200 proxies: 42 s frio / 8 s adoção, loopback).
- Instalador: transação com rollback por trap EXIT/ERR, healthcheck por `?include=status`, prazo 45 s + 5 s/proxy, reinício um a um com `REINICIAR/--yes`
  quando a config renderizada muda, backups em `/var/backups/ipv6-proxy/` (`--keep-backups`), `pip --require-hashes -r requirements.lock`.
- Clientes: `GET /proxies?include=status`, `Idempotency-Key` uuid4 em toda mutação, `If-Match` em edit/rotate/delete; TUI com Text literal (sem markup) e rodapé próprio.
- Entrega: branch local `dpm/independent-proxies`=47f5d83; artefatos em `../dist/`; `docs/PR_PROPOSAL.md`.
