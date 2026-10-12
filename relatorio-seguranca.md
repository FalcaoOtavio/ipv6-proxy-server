# T10 — Relatório de segurança (diff `bdfe9c7..767761c` + instalador)

Data: 2026-10-11 · Auditor: agent/sec · Ferramentas: `dpm security scan` (gitleaks, semgrep, trivy-fs, pip-audit),
shellcheck 0.11.0, 3proxy 0.9.4 real (`.tools/3proxy`), revisão manual.

## Resumo

| # | Sev. | Achado | Dono sugerido | Estado |
|---|------|--------|---------------|--------|
| S1 | **ALTO** | SSRF: qualquer proxy alcança serviços do próprio host (`[::1]`, ULA, link-local) | back (T18) | aberto; fix verificado; teste `tests/test_security_ssrf.py` (xfail strict) |
| S2 | MÉDIO | starlette 0.48.0 com 12 advisories (6 IDs) via `fastapi<0.117` | back (T18) | aberto; fix verificado (359 passed) |
| S3 | BAIXO | GitHub Actions por tag mutável (`@v4`, `@v5`) | qa/ci | aberto; SHAs abaixo |
| S4 | BAIXO | Instalador muda o modo de `/var/backups` (diretório do sistema) para 0700 | inst (T17) | aberto |
| S5 | BAIXO | Dependências Python por faixa (`>=,<`), sem lock/hashes | back (T18) | recomendação |
| F1 | funcional | `/usr/local/bin/ipv6-proxy-tui` instalado não inicia (`ModuleNotFoundError: proxy_tui`) | clients (T19) / inst (T17) | aberto |
| I1 | info | gitleaks: 1 falso positivo (`proxy_api/app.py:19`, commit `1be16bf1`, lista `CAPABILITIES`) | — | sem ação |
| I2 | info | shellcheck: `install.sh` limpo; `ipv6-proxy-server.sh` (legado, inalterado no diff) tem erros de parse do shellcheck, `bash -n` OK | — | sem ação |

Sem crítico. Um ALTO em aberto (preexistente desde a base `bdfe9c7`, não introduzido pelo diff).

---

## S1 — ALTO — SSRF para o loopback/rede interna do host

**Causa.** `proxy_api/backend.py:render_config` gera `auth none` + `allow *` (sem credencial) ou
`auth strong` + `allow <user>` (com credencial), sem nenhuma ACL de destino. O cliente do proxy escolhe o destino;
com `-6` e egress `-e<ipv6>` do host, destinos IPv6 internos são alcançáveis: `[::1]` (serviços só-loopback,
inclusive a própria API em `::1` se algum dia bindar dual-stack), `fc00::/7` (ex.: IMDS AWS `fd00:ec2::254`),
`fe80::/10`, e nomes que resolvem para esses endereços (`localhost`, `ip6-localhost`).
Mesmo proxies com senha são afetados: o cliente legítimo de um proxy é terceiro (revenda), não o operador.

**Reprodução (3proxy 0.9.4 real):**
```
python3 -m http.server 18081 --bind ::1 &
printf 'auth none\nallow *\nproxy -6 -n -a -p31001 -i127.0.0.1 -e::1\n' > a.cfg; 3proxy a.cfg &
curl -s -o /dev/null -w '%{http_code}\n' -x http://127.0.0.1:31001 'http://[::1]:18081/'       # 200
curl -s -o /dev/null -w '%{http_code}\n' -x http://127.0.0.1:31001 'http://ip6-localhost:18081/' # 200
```
SOCKS5 (`socks -6 -a`) também retorna o conteúdo de `[::1]`. IPv4 `127.0.0.1` e `::ffff:127.0.0.1` dão 502
(o `-6` não sai por IPv4), mas o deny cobre `::ffff:0:0/96` por defesa em profundidade.

**Fix verificado** (decisão do orquestrador, #155):
```
deny * * ::/128,::1/128,::ffff:0:0/96,fe80::/10,fc00::/7,ff00::/8,<subnet configurada>
auth iponly          # no modo sem credencial; "auth none" IGNORA ACLs (medido: deny + auth none => 200)
allow *              # ou: auth strong + allow <user>
```
Medições: `auth iponly`/`auth strong` + deny ⇒ 403 para `[::1]`, `[::]`, `::ffff:127.0.0.1`, `localhost`,
`ip6-localhost` (o deny é aplicado após a resolução de nome). Destinos públicos seguem permitidos.
A linha `deny` deve vir antes do `allow`. Incluir a subnet própria impede um proxy de alcançar outro proxy do host.
Ligado por padrão; `PROXY_BLOCK_PRIVATE_DESTINATIONS=false` só para testes em loopback (os testes reais usam
`::1` e `::ffff:127.0.0.0/120` como alvo/egress e precisam desse override).

**Impacto operacional.** A config renderizada muda ⇒ a reconciliação reinicia cada proxy uma vez na atualização
(T17/M3: instalador anuncia a contagem e exige `--yes`).

**Teste.** `tests/test_security_ssrf.py` sobe 3 proxies reais (http sem auth, http com auth, socks5 com auth)
egressando de `::1` e falha se algum entregar o conteúdo de um serviço em `[::1]`. Hoje: `xfail(strict)`.
Com o patch acima aplicado temporariamente o teste dá `XPASS(strict)` (ou seja, passa): quem implementar T18 remove
o marcador `xfail` e, se a flag de override existir, garante que o teste usa o padrão (bloqueio ligado).

## S2 — MÉDIO — starlette 0.48.0 vulnerável

`pip-audit -r requirements.txt`: PYSEC-2026-161, -248, -249, -1942, -2280, -2281 (Host/path reconstruindo
`request.url`, Range quadrático em `FileResponse`, `StaticFiles` UNC, `HTTPEndpoint` getattr, limites de form
urlencoded). **Alcance real hoje: baixo** — a API não usa `request.url` para autorização, `FileResponse`,
`StaticFiles`, `HTTPEndpoint` nem `request.form()`, e escuta só em `127.0.0.1`. Classificado MÉDIO porque o gate
`pip-audit` falha e a correção é barata.

**Fix verificado:** `fastapi==0.133.0` (primeira que aceita starlette ≥ 1.3.1; resolve starlette 1.7.0).
Suíte completa com 3proxy real nessa venv: `359 passed in 486s`. pip-audit resultante: só `pytest 8.4.2`
(PYSEC-2026-1845, dependência de dev, corrigido em 9.0.3).

## S3 — BAIXO — Actions por tag mutável

`.github/workflows/tests.yml:9-10`, `vm-e2e.yml:23,41`. Fixar por SHA (resolvidos hoje):
```
actions/checkout@11d5960a326750d5838078e36cf38b85af677262        # v4
actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065    # v5
actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4
```
Os workflows já usam `permissions:` restrito e `inputs.*` entra via `env:` (sem injeção em `run:`).

## S4 — BAIXO — instalador altera `/var/backups`

`install.sh:backup_installation`: `install -d -m 0700 "$backup_root" "$backup_dir"`. `install -d` aplica o modo
também a diretório existente (medido: 755 ⇒ 700), então `/var/backups` do Debian/Ubuntu vira 0700. Não expõe
nada, mas altera estado do sistema fora do escopo do projeto. Fix: `install -d -m 0700 "$backup_dir"`
(com `mkdir -p` do pai se ausente). Observação: backups contêm o `env` com o token (0600 dentro de dir 0700, ok) e
nunca são podados; documentar limpeza.

## S5 — BAIXO — pins por faixa

`requirements.txt` usa faixas (`fastapi>=0.115,<0.117`, `uvicorn>=0.30,<0.36`, `pydantic>=2.9,<3`): a instalação
na VPS resolve versões diferentes das testadas. Recomendação: pins exatos (ou `pip install --require-hashes` com
lock gerado). 3proxy está correto: tag + SHA conferido antes do build.

## F1 — funcional — TUI instalada não inicia

`ipv6-proxy-tui` usa `#!/usr/bin/env python3` (sem Textual) e `sys.path.insert(0, <dir do script>)`; instalado em
`/usr/local/bin`, o pacote fica em `/opt/ipv6-proxy-server/proxy_tui`. Reprodução: copiar o launcher para um
diretório fora do repo e executar ⇒ `ModuleNotFoundError: No module named 'proxy_tui'` (com o python do venv
também). Sem impacto de segurança (o path inserido seria `/usr/local/bin`, root:755). Fix: launcher com
`#!/opt/ipv6-proxy-server/.venv/bin/python` e `sys.path.insert(0, "/opt/ipv6-proxy-server")`, ou shell
`exec /opt/ipv6-proxy-server/.venv/bin/python -c 'from proxy_tui.app import main; main()'`. O passo da TUI no
`vm_e2e.sh` deveria pegar isso.

---

## Verificado sem achado

- **Token fora de argv/logs/ps:** instalador gera com `openssl rand` em `$(...)`, grava via heredoc (builtin) e
  usa `curl --config` em `mktemp -d` sob `umask 077`; o self-test grava a senha em arquivo do work_dir 0600 e usa
  `--data @file`. CLI lê `/etc/ipv6-proxy-api.env` com `O_NOFOLLOW`, exige root:0600; senha só por getpass ou
  `--password-stdin`; `Parser.error` não ecoa argumentos desconhecidos; corpos de erro HTTP nunca são exibidos
  (CLI e TUI). A API não loga senhas (logs só com porta).
- **Permissões:** env 0600; `data_dir` 0700 (chmod no startup); state/configs/`.addr` via `atomic_write` 0600;
  units com `UMask=0077`; exports CLI/TUI 0600, `O_EXCL|O_NOFOLLOW` (CLI `--force` via mkstemp + rename);
  backups 0700.
- **Injeção em config 3proxy:** username `^[A-Za-z0-9_.-]+$`, password `^[A-Za-z0-9_.!@%+=-]+$` (sem espaço,
  quebra de linha, `#`, `$`), IPv6/IPv4 tipados; teste `{"username": "a\nallow *"}` ⇒ 422. Interface por regex.
- **Unit names / comandos privilegiados:** `systemctl` só sobre `ipv6-proxy@<int validado>.service`; `ip addr del`
  só para endereços do journal `owned-addresses.json`; `instance_address.py` (ExecStartPre `+`) recusa endereço
  fora do journal e interface inválida. Instalador recusa 3proxy de terceiros e não mata nada fora dos seus units.
- **Exposição da API:** uvicorn `--host 127.0.0.1`; CLI/TUI só `http://` loopback, sem proxy de ambiente e sem
  seguir redirect. Hardening systemd (`NoNewPrivileges`, `ProtectSystem=strict`, `CapabilityBoundingSet`).
- **AuthZ:** `compare_digest` no token admin; tokens de cliente por SHA-256 vinculados a owner; outro owner ⇒ 404
  (sem IDOR); `X-Proxy-Owner` não amplia token de cliente (403); `If-Match` evita agir sobre herdeiro da porta.
- **trivy-fs:** sem achados.

## Como reproduzir os testes

```
TEST_3PROXY_BINARY=/data/projects/ipv6-proxy-server-independente/.tools/3proxy \
  .venv/bin/python -m pytest -q                                                  # base: 359 passed (antes do teste novo)
TEST_3PROXY_BINARY=/data/projects/ipv6-proxy-server-independente/.tools/3proxy \
  .venv/bin/python -m pytest -q tests/test_security_ssrf.py -rxX                  # 1 xfailed (SSRF aberto)
```
