# ADR 0011: Publicação autorizada: branch + PR no fork

- Data: 2026-10-14
- Status: aceito

## Contexto
Otávio autorizou em 2026-10-14 ('Autorizo! Siga até o final') o push/PR pendente desde #152.

## Decisão
Push só de refs/heads/dpm/independent-proxies (6b1d3ab) por URL explícita; PR #1 para main com corpo = docs/PR_PROPOSAL.md. NÃO usar 'dpm git push' (empurra main + tags -f ao origin). Sem merge do PR, sem release.

## Alternativas
dpm git push (rejeitado: arriscaria main do fork)

## Consequências
CI 'Tests and installer' roda no push/PR. vm-e2e (workflow_dispatch) exige permissão actions:write que o GitHub App não tem (403) — disparo manual pelo Otávio.
