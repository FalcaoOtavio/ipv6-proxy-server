# Proposta de PR — proxies IPv6 independentes (supervisão por proxy)

> Documento de entrega local (T15). **Nada foi enviado ao GitHub.** Push, PR e CI dependem do gate #152.

## Título proposto

`feat: independent per-proxy supervision, pinned-ref installer, CLI batch and TUI`

## Resumo

Cada proxy passa a rodar como unidade systemd própria (`ipv6-proxy@<porta>`), supervisionada e operada
individualmente: criar, editar, rotacionar ou remover um proxy não reinicia os outros, e uma falha num proxy não
derruba a API, a listagem nem o healthcheck. O instalador fica fixável por revisão (`--ref`), faz update com
backup e rollback transacional e migra uma única vez do 3proxy único para unidades por proxy. Inclui CLI com
lote/edição/exportação, TUI em Textual, bloqueio de SSRF por padrão (TCP e SOCKS5 UDP ASSOCIATE) e dependências
fixadas com hash.

## SHAs

| Referência | SHA |
|---|---|
| Base do PR (`main` do fork) | `aa2968556f861f56c7e5d37c70a859797a8d8772` |
| Origem (branch do fork `ashford/isolated-mutations`) | `bdfe9c7634691f51e893f6e53b6137f95792e4f5` |
| Final do código (main do DPM = cp-0025, branch local `dpm/independent-proxies`) | `47f5d83883e362d3116e7211045f2c3b52b7de82` |

- `bdfe9c7` é ancestral de `47f5d83` (verificado com `git merge-base --is-ancestor`), e `aa29685` é ancestral de `bdfe9c7`.
- Diff `bdfe9c7..47f5d83`: 237 arquivos, +17310/−735 (152 arquivos de produto/docs/CI, o resto testes e snapshots).
- O commit que adiciona este documento fica acima de `47f5d83`; o SHA dele é reportado no `dpm report` da T15.

## Mudanças por área

- **Supervisor/backend** (`proxy_api/`): unidade por proxy, operações por alvo, reconciliação no boot, parada de
  órfãos só com registro no journal, `enable/disable --no-reload`, `StartLimit` por unidade.
- **API**: `PATCH`, lote com `Idempotency-Key`, status por proxy (`include_status`), restauração do endereço da
  instância no boot, `If-Match` em rotate/delete, `/capabilities` sem UDP quando o bloqueio de SSRF está ligado.
- **Segurança**: destinos loopback/internos negados por padrão (`PROXY_BLOCK_PRIVATE_DESTINATIONS=true`), `deny UDPASSOC`
  no 3proxy, drop-in systemd `IPAddressDeny` condicional, dependências com hash (`requirements.lock`), Actions fixadas por SHA.
- **Instalador** (`install.sh`): `--ref` resolvido para SHA em `/opt/ipv6-proxy-server/VERSION`, `--update` com backup em
  `/var/backups/ipv6-proxy/`, rollback transacional, healthcheck por status, migração única para unidades por proxy.
- **CLI** (`ipv6-proxy`): `batch`, `edit`, `export` (arquivos 0600), mapa de opções upstream.
- **TUI** (`ipv6-proxy-tui`, `proxy_tui/`): operações com alvo fixo, exportação, dados da API renderizados literalmente.
- **Docs**: README reescrito, `docs/INSTALL.md`, `API.md`, `CLI.md`, `TUI.md`, `MIGRATION.md`, `TROUBLESHOOTING.md`.
- **CI**: `.github/workflows/tests.yml` alinhado; `.github/workflows/vm-e2e.yml` (manual, `workflow_dispatch`).

## Testes e evidências

- Suíte completa com 3proxy 0.9.4 real no main final `47f5d83` (T15, 2026-10-12):
  `TEST_3PROXY_BINARY=/data/projects/ipv6-proxy-server-independente/.tools/3proxy .venv/bin/python -m pytest -q`
  → `426 passed, 3 warnings in 309.12s (0:05:09)`.
- QA final: [docs/evidence/qa-final.md](evidence/qa-final.md) (3 execuções com 3proxy real, aceitação A/B, TUI, shellcheck, pip-audit).
- Segurança: [docs/reviews/T22-security.md](reviews/T22-security.md) (N1 e F01 fechados na verificação T27).
- Limites medidos: [docs/evidence/limits.md](evidence/limits.md). TUI: [docs/evidence/tui/](evidence/tui/).
- Aceitação com 3proxy real: `tests/acceptance/` (evidência em `tests/acceptance/evidence/`).

## Artefatos locais (fora do git)

Em `/data/projects/ipv6-proxy-server-independente/dist/`, conferidos por `SHA256SUMS` (`sha256sum -c SHA256SUMS`):

- `ipv6-proxy-server-independent.bundle` — `git bundle` de `bdfe9c7..dpm/independent-proxies`
  (sha256 `98660ea32f93b2a920f560570a8799ee74b275a2cfa312743ab8e7405720002c`). `git bundle verify` OK num clone do
  fork; o fetch do bundle produz `47f5d83`.
- `patches/` — 40 patches: `0000` (arquivos de bootstrap do DPM sobre `bdfe9c7`) + `git format-patch --no-merges
  7f5c71e..dpm/independent-proxies`. Aplicados com `git am` sobre `bdfe9c7`, geram árvore idêntica a `47f5d83`.
  O `0000` existe porque o histórico do DPM começa num commit raiz próprio (`50aa13a`) e importa o fork por merge
  (`7f5c71e`); um `format-patch bdfe9c7..` puro não aplica sobre `bdfe9c7`.
- `ipv6-proxy-server-independent.squash.diff` — diff único `bdfe9c7..47f5d83` (sha256
  `5b9571b8765af0688576ed485130bff742e6ab4dd9f1d2f1811c8c3c1b0d68de`); `git apply` sobre `bdfe9c7` gera a mesma árvore.

## Riscos

- Sem VM real: systemd é simulado nos testes de instalador/backend; reboot real, IPv6 roteado e `IPAddressDeny` via
  cgroup/BPF só foram verificados em dry-run (`tests/e2e/vm_e2e.sh --dry-run`).
- Bloqueio de SSRF ligado por padrão muda comportamento: destinos privados/loopback e SOCKS5 UDP ASSOCIATE passam a ser
  negados. Quem depende disso precisa de `PROXY_BLOCK_PRIVATE_DESTINATIONS=false` explícito.
- Migração única de 3proxy único para unidades por proxy no `--update` (com backup e rollback; ver `docs/MIGRATION.md`).
- O histórico inclui arquivos do DevProMax (`CLAUDE.md`, entradas no `.gitignore`/`README.md` do commit `50aa13a`).
  Decidir antes do push se ficam no PR ou se saem num commit de limpeza.
- Replay idempotente de `batch --random` não devolve senhas (por desenho; `docs/CLI.md`).

## Gates pendentes

- **#152** — push da branch para o fork, abertura do PR e execução do CI (`tests.yml`, `vm-e2e.yml`). Requer aprovação do Otávio.
- **T14** — Linux limpo com systemd real, reboot real e tráfego por IPv6 roteado (`tests/e2e/vm_e2e.sh` fases
  `pre-reboot`/`post-reboot`).

## Comandos propostos (NÃO executados)

O remote `fork` está com push desabilitado (`DISABLED`) neste repo. Após aprovação do #152, num clone com permissão:

```bash
# 1. Push da branch (a partir do repo do DPM, ou de um clone do fork + bundle)
git fetch /data/projects/ipv6-proxy-server-independente/dist/ipv6-proxy-server-independent.bundle \
  dpm/independent-proxies:dpm/independent-proxies   # só se partir de um clone do fork
git push git@github.com:FalcaoOtavio/ipv6-proxy-server.git dpm/independent-proxies:dpm/independent-proxies

# 2. PR para main
gh pr create --repo FalcaoOtavio/ipv6-proxy-server --base main --head dpm/independent-proxies \
  --title "feat: independent per-proxy supervision, pinned-ref installer, CLI batch and TUI" \
  --body-file docs/PR_PROPOSAL.md

# 3. Workflow vm-e2e (manual) na branch
gh workflow run vm-e2e.yml --repo FalcaoOtavio/ipv6-proxy-server --ref dpm/independent-proxies
gh run watch --repo FalcaoOtavio/ipv6-proxy-server "$(gh run list --repo FalcaoOtavio/ipv6-proxy-server \
  --workflow vm-e2e.yml --branch dpm/independent-proxies --limit 1 --json databaseId -q '.[0].databaseId')"
```

## Instalação com o SHA final

Só funciona **depois do push** (o `raw.githubusercontent.com` e o `--ref` resolvem o SHA no fork):

```bash
REF=47f5d83883e362d3116e7211045f2c3b52b7de82
curl -fsSLo install-ipv6-proxy.sh "https://raw.githubusercontent.com/FalcaoOtavio/ipv6-proxy-server/$REF/install.sh"
sudo bash install-ipv6-proxy.sh --ref "$REF" --self-test
```

Se o commit deste documento for incluído no push, o SHA dele também serve como `--ref` (mesmo código, mais este arquivo).
