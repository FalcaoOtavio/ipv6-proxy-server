# T22 — Reauditoria de segurança pós-correções (main `5fd8149`, diff `0d4535b..HEAD`)

Data: 2026-10-12 · Auditor: agent/sec2 · Ferramentas: `dpm security scan` (gitleaks, semgrep, trivy-fs, pip-audit),
`pip-audit -r requirements.lock --require-hashes`, 3proxy 0.9.4 real (`.tools/3proxy`), sondas SOCKS5 UDP manuais, revisão manual.

## Veredito por item

| # | Sev. original | Veredito | Prova |
|---|---------------|----------|-------|
| S1 TCP (http/socks, com e sem credencial) | ALTO | **FECHADO** | `tests/test_security_ssrf.py` (3 proxies reais, sem xfail): 403/recusa para `[::1]`; override abre (controle) |
| S1 SOCKS5 UDP ASSOCIATE | ALTO | **ABERTO — ALTO (N1)** | `tests/test_security_udp.py` (xfail strict); detalhes abaixo |
| S2 starlette/pip-audit | MÉDIO | FECHADO | `pip-audit -r requirements.lock --require-hashes`: *No known vulnerabilities found*; scan dpm: pip-audit OK |
| S3 Actions por SHA | BAIXO | FECHADO | `tests.yml`/`vm-e2e.yml`: os 3 SHAs do T10 com comentário de tag |
| S4 `/var/backups` | BAIXO | FECHADO | `install.sh:468-472`: só `var/backups/ipv6-proxy` recebe 0700; pai criado 0755 só se ausente; poda `keep_backups` |
| S5 pins/hashes | BAIXO | FECHADO | `requirements.txt` com `==`; `requirements.lock` (pip-compile, 21 pacotes, 177 hashes) igual ao txt; instalador usa `--require-hashes` (`install.sh:434,751`) |
| A6 senha em traceback da TUI | — | FECHADO | `ProxyTui._fatal_error` substitui o do Textual 8.2.8 (único caminho com `show_locals=True`; `WorkerFailed` sem `__rich__` cai nele); worker usa `GENERIC_ERROR` para não-`ApiError`; testes `test_a6_*` |
| A7/M1 Idempotency-Key/If-Match nos clientes | — | FECHADO | CLI: `mutate()` envia uuid4 (ou `--idempotency-key`) em toda mutação e `If-Match` em edit/rotate/delete (`pinned_id`); TUI: uuid4 + 1 retentativa só em falha de transporte, If-Match em edit/rotate/delete; API valida a chave (422) e recusa reuso com outra request |
| F1 TUI instalada | funcional | FECHADO | launcher resolve `/opt/ipv6-proxy-server` e re-exec no venv, guarda contra loop |

Sem crítico. **Um ALTO em aberto (N1)** ⇒ gate de segurança **não passa**.

## N1 — ALTO — SSRF via SOCKS5 UDP ASSOCIATE (S1 incompleto)

**Causa.** No 3proxy 0.9.4 a ACL (`deny * * <redes>`) do `socks` é avaliada para UDP ASSOCIATE contra o
endereço/porta que o **cliente declara** no pedido ASSOCIATE (o "DST.ADDR" do RFC 1928, que é o endereço de
origem esperado do cliente), não contra o destino de cada datagrama. Depois de aceito, o relay encaminha cada
datagrama para o destino escrito no cabeçalho SOCKS-UDP, sem nova checagem de ACL.

**Reprodução** (sonda Python, proxy `socks5` com credencial, config gerada por `render_config` com o padrão
`block_private_destinations=True`, egress `::1`, eco UDP em `[::1]`):

| ASSOCIATE declara | bloqueio ligado | resultado |
|---|---|---|
| `0.0.0.0:0` (padrão dos clientes) | sim | sem resposta (pedido pendurado) |
| `127.0.0.1:<porta do cliente>` | sim | sem resposta |
| `203.0.113.5` (IPv4 público) | sim | sem resposta |
| `2606:4700::1111` (IPv6 público) | sim | **datagrama entregue a `[::1]` e resposta `secret:ping` recebida** |
| qualquer | não (override) | entregue (controle positivo) |

Mesmo resultado sem credencial. Destino IPv4 (`127.0.0.1`) não é alcançado (egress `-6`), então o alcance é
IPv6: loopback do host, ULA (ex.: IMDS `fd00:ec2::254` é TCP, mas DNS/serviços UDP internos em `fc00::/7`),
link-local e os outros proxies da própria subnet (estes são TCP, logo menos relevantes). Severidade **ALTO**,
igual a S1: qualquer cliente de proxy SOCKS5 (terceiro/revenda) envia UDP para serviços só-loopback do host
(ex.: DNS resolver local em `[::1]:53`, SNMP, statsd, memcached UDP).

**Efeito colateral funcional (N1b).** Como `0.0.0.0` é `::ffff:0.0.0.0`, coberto pelo deny `::ffff:0:0/96`, o
ASSOCIATE de clientes padrão (curl, Python `socks`, a própria `test_socks5_udp_associate_egresses_from_the_proxy_ipv6`
roda com o override) fica **sem resposta**: o recurso anunciado em `/capabilities` (`socks5_udp_associate`) e em
`docs/API.md:43` não funciona com o padrão de produção. Sem a faixa `::ffff:0:0/96` todos os ASSOCIATE funcionam
— e todos vazam para `[::1]`. Testado também `deny * * * * UDPASSOC` como regra adicional: bloqueia tudo
(inclusive o caso público), ou seja, desliga UDP.

**Recomendação (back, decisão do orquestrador):**
1. Curto prazo, seguro: não oferecer UDP ASSOCIATE com bloqueio ligado — `deny * * * * UDPASSOC` antes do
   deny de redes quando `block_private_destinations` (verificado: fecha N1) e remover/condicionar
   `socks5_udp_associate` em `/capabilities` e na doc. Muda a config renderizada ⇒ um restart por proxy SOCKS5
   na atualização (fluxo `config_changes` do instalador já cobre).
2. Alternativa que mantém UDP: filtro de destino no nível do kernel para o tráfego dos proxies
   (`ip6tables`/nftables `owner`/cgroup do `ipv6-proxy@.service`: `-d ::1,fc00::/7,fe80::/10,<subnet>` REJECT,
   ou `IPAddressDeny=` do systemd na unit dos proxies — `IPAddressDeny=::1/128 fc00::/7 fe80::/10 ...` com
   `IPAddressAllow=` para o bind IPv4 do cliente). Isso vale para TCP e UDP e não depende da semântica da ACL do 3proxy.
   O `IPAddressDeny=` é a opção mais barata e nativa (exige cgroup v2/BPF; validar em VM).
3. Ao corrigir: remover os `xfail` de `tests/test_security_udp.py`; o segundo teste exige que ASSOCIATE padrão
   responda — se a opção 1 for escolhida, inverta-o para afirmar recusa explícita (REP≠0) em vez de pendurar.

## Varredura do diff `0d4535b..HEAD` (regressões)

- **Injeção em `render_config` via subnet:** `settings.subnet` é `IPv6Network` (pydantic) e sai como
  `str(network)` — só hex, `:` e `/len`. Sem quebra de linha/espaço possível. `installer config_changes` também
  converte com `IPv6Network(...)`. OK.
- **Ordem ACL:** `auth iponly|strong` → `deny` → `allow`; `auth none` removido (ignorava ACL). OK.
- **Flag de override:** padrão `True` em `Settings`, no `env.example` e no instalador (qualquer valor diferente
  de falso explícito mantém bloqueio). Só testes usam `False`. OK.
- **Permissões novas:** `pending-ports.json`/`orphans.json` via `atomic_write` (`O_CREAT` 0600, tmp+rename+fsync)
  em `data_dir` (0700, `StateDirectory`, `UMask=0077`). `requirements.lock`/`.txt` instalados 0644 (público, sem
  segredo). Backups 0700. OK.
- **`pending-ports.json` como entrada:** só é lido do `data_dir` root:0700; conteúdo usado apenas como conjunto de
  portas comparado a `list_managed()` (unit names inteiros validados no supervisor). Arquivo corrompido derruba
  a API no start (`json.loads` sem try) — falha fechada, não vetor. Info.
- **Órfãos:** `_orphan_addresses` só protege da liberação endereços lidos de `.addr` próprio; não amplia o que é
  removido. Mudança reduz ação coletiva (não para órfãos). OK.
- **Segredos em argv/logs:** CLI mantém senha só por prompt/`--password-stdin`; `--idempotency-key` e `--if-match`
  no argv não são segredos (uuid/id público). Hint de retentativa mostra chave e id — não sensíveis. Instalador
  segue passando o token por `curl --config` em `work_dir`. Novos `logger.*` em backend/manager logam só portas.
  `supervisor._systemctl` inclui `stderr` do systemctl em `RuntimeError` (preexistente; nunca contém senha). OK.
- **Corpo de erro HTTP na TUI:** só lê até 512 bytes do 422 de `/proxies/batch` e extrai um inteiro por regex
  (corpo pode refletir a senha enviada). OK.
- **Timeout de batch** (`60 + 5*count`, `count` ≤ `PROXY_BATCH_MAX` ≤ 10000): no máximo ~14 h de espera em uma
  conexão local; sem impacto de segurança. Info.
- **Unit files:** `StartLimit*` apenas; hardening intacto. OK.

## Scanners

- gitleaks: 1 achado = falso positivo I1 já triado (`proxy_api/app.py:19`, commit `1be16bf1`, lista `CAPABILITIES`).
- semgrep: OK. trivy-fs: OK. pip-audit (`requirements.txt`/lock): OK.
- `pip-audit -r requirements-dev.txt`: `pytest 8.4.2` PYSEC-2026-1845 (fix 9.0.3) — dependência só de dev, não
  instalada em produção. BAIXO, aberto (T01 dono de requirements).

## Como reproduzir

```unknown
TEST_3PROXY_BINARY=/data/projects/ipv6-proxy-server-independente/.tools/3proxy \
  .venv/bin/python -m pytest -q tests/test_security_ssrf.py tests/test_security_udp.py -rxX   # 2 passed, 2 xfailed
.venv/bin/pip-audit -r requirements.lock --require-hashes --disable-pip                       # no known vulnerabilities
```
