# Proposta de PR — proxies IPv6 independentes (supervisão por proxy)

> Documento de entrega local (T15, atualizado na release T36). **Nada foi enviado ao GitHub.** Push, PR e CI dependem do gate #152.

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
| Final do código (main do DPM com T29–T35 e E2E em VM) | `1402bdc05f519a8a09a2119972156215f1bb572b` |

- `bdfe9c7` é ancestral de `1402bdc` (verificado com `git merge-base --is-ancestor`), e `aa29685` é ancestral de `bdfe9c7`.
- Diff `bdfe9c7..1402bdc`: 291 arquivos, +19098/−736.
- O commit de docs da release (T36: este arquivo, README e qa-final) fica logo acima de `1402bdc`, sem mudar código.
  Um commit não consegue conter o próprio SHA: ele aparece no `dpm report` da T36, no `dist/` (bundle e patches) e
  é o SHA da branch local `dpm/independent-proxies`.

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
- **Host lento (T30/T32)**: `PROXY_READY_TIMEOUT` (prontidão por proxy, 1..300 s, padrão 30), `install.sh --api-timeout S`
  (10..3600, padrão 180), `POST /proxies/{port}/recover` (admin) para porta `fenced`.
- **Instalador (T34/T35)**: `--update` re-renderiza as cfg e reinicia um a um os proxies afetados quando muda config
  vinda do env (ex.: toggle de `PROXY_BLOCK_PRIVATE_DESTINATIONS`); espera o lock do apt até `--apt-lock-timeout S`
  (0..7200, padrão 600) sem matar processo alheio.

### Bugs achados pelo E2E em VM e corrigidos

| Tarefa | Bug | Correção |
|---|---|---|
| T30 | Prontidão fixa de 5 s: self-test e POST falhavam em host lento (bind ~21 s) | `PROXY_READY_TIMEOUT`, `--api-timeout`; probe antes do prazo no reconcile |
| T32 | Na migração, `systemctl start` estourava 30 s fixos e a porta ficava `fenced` (503) com o instalador reportando sucesso | prazo `max(30, PROXY_READY_TIMEOUT+15)`, adoção do proxy que subiu, `POST /proxies/{port}/recover` |
| T34 | Toggle do bloqueio via `--update` no mesmo SHA instalava o drop-in sem re-renderizar cfg nem reiniciar: falso "protegido" | re-render + reinício um a um + verificação |
| T35 | Instalação limpa logo após o 1º boot falhava no lock do apt | espera limitada por `--apt-lock-timeout` |

## Testes e evidências

- QA de release no main `1402bdc` (T36, 2026-10-13): [docs/evidence/qa-final.md](evidence/qa-final.md#release-final-t36)
  (suíte sem 3proxy, 2x com 3proxy 0.9.4 real, TUI 3x, `tui_validation.py`, shellcheck 0.9/0.11, pip-audit).
- E2E em VM real Ubuntu 24.04: [docs/evidence/e2e/ubuntu-24.04/](evidence/e2e/ubuntu-24.04/README.md).
- Segurança: [docs/reviews/T22-security.md](reviews/T22-security.md) (N1 e F01 fechados na verificação T27).
- Limites medidos: [docs/evidence/limits.md](evidence/limits.md). TUI: [docs/evidence/tui/](evidence/tui/).
- Aceitação com 3proxy real: `tests/acceptance/` (evidência em `tests/acceptance/evidence/`).

## Artefatos locais (fora do git)

Em `/data/projects/ipv6-proxy-server-independente/dist/`, regenerados na T36 a partir do HEAD do commit de docs
(SHA no `dpm report` da T36) e conferidos por `SHA256SUMS` (`sha256sum -c SHA256SUMS`; os hashes não ficam aqui porque
dependem desse commit):

- `ipv6-proxy-server-independent.bundle` — `git bundle` de `bdfe9c7..dpm/independent-proxies`; `git bundle verify`
  num clone do fork e o fetch produz o SHA final.
- `patches/` — `0000` (arquivos de bootstrap do DPM sobre `bdfe9c7`) + `git format-patch --no-merges
  7f5c71e..<SHA final>`. Aplicados com `git am` sobre `bdfe9c7`, geram árvore idêntica ao SHA final.
  O `0000` existe porque o histórico do DPM começa num commit raiz próprio (`50aa13a`) e importa o fork por merge
  (`7f5c71e`); um `format-patch bdfe9c7..` puro não aplica sobre `bdfe9c7`.
- `ipv6-proxy-server-independent.squash.diff` — diff único `bdfe9c7..<SHA final>`; `git apply` sobre `bdfe9c7` gera a
  mesma árvore.

## Riscos

- **IPv6 roteado real não testado**: a VM usou ULA em `lo`; NDP, rota do provedor e saída externa não foram comprovados.
- **Distros não executadas**: só Ubuntu 24.04 rodou em VM; Debian 12/13 e Ubuntu 22.04/26.04 não.
- **VM em TCG** (sem KVM, host carregado): tempos não representativos. Sob TCG o apt do 1º boot passou dos 600 s do
  `--apt-lock-timeout` padrão (o instalador abortou sem instalar nada); com 3600 s concluiu. A espera pelo lock não foi
  observada numa execução válida.
- Bloqueio de SSRF ligado por padrão muda comportamento: destinos privados/loopback e SOCKS5 UDP ASSOCIATE passam a ser
  negados. Quem depende disso precisa de `PROXY_BLOCK_PRIVATE_DESTINATIONS=false` explícito.
- Migração única de 3proxy único para unidades por proxy no `--update` (com backup e rollback; ver `docs/MIGRATION.md`;
  validada em VM a partir de `aa29685` e `bdfe9c7`).
- O histórico inclui arquivos do DevProMax (`CLAUDE.md`, entradas no `.gitignore`/`README.md` do commit `50aa13a`).
  Decidir antes do push se ficam no PR ou se saem num commit de limpeza.
- Replay idempotente de `batch --random` não devolve senhas (por desenho; `docs/CLI.md`).

## Gates pendentes

- **#152** — push da branch para o fork, abertura do PR e execução do CI (`tests.yml`, `vm-e2e.yml`). Requer aprovação
  do Otávio. **Push e PR NÃO foram executados.**
- Tráfego por IPv6 roteado real (`E2E_ROUTED_IPV6_TARGET` no `tests/e2e/vm_e2e.sh`) numa VPS com subnet de provedor.
- (Opcional) E2E em Debian 12/13 e Ubuntu 22.04/26.04.

## Comandos propostos (NÃO executados)

O remote `fork` está com push desabilitado (`DISABLED`) neste repo. Após aprovação do #152, num clone com permissão
(`<SHA final>` = SHA reportado na T36, igual ao da branch local `dpm/independent-proxies`):

```bash
# 1. Push da branch (a partir do repo do DPM, ou de um clone do fork + bundle)
git fetch /data/projects/ipv6-proxy-server-independente/dist/ipv6-proxy-server-independent.bundle \
  dpm/independent-proxies:dpm/independent-proxies   # só se partir de um clone do fork
test "$(git rev-parse dpm/independent-proxies)" = "<SHA final>"
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

Só funciona **depois do push** (o `raw.githubusercontent.com` e o `--ref` resolvem o SHA no fork). Tanto `1402bdc`
(código) quanto o SHA final (código + docs da release) servem como `--ref`:

```bash
REF=<SHA final>   # ou 1402bdc05f519a8a09a2119972156215f1bb572b
curl -fsSLo install-ipv6-proxy.sh "https://raw.githubusercontent.com/FalcaoOtavio/ipv6-proxy-server/$REF/install.sh"
sudo bash install-ipv6-proxy.sh --ref "$REF" --self-test   # host lento: --api-timeout 600 --apt-lock-timeout 1800
```
