# Relatório — Sprints 1–2 (2026-10-11 → 2026-10-12)

## Feito
- Base: fork em `bdfe9c7` (ashford/isolated-mutations), contratos id/If-Match/Idempotency-Key/owner/capabilities preservados.
- Independência: 1 unidade systemd `ipv6-proxy@<porta>` por proxy (fora do cgroup da API), readiness pelo PID dono do socket, endereço restaurado por
  instância no boot, mutações só no alvo, rollback/fencing por porta, reconciliação que adota sem reiniciar, órfãos nunca parados sem intenção.
- Contratos novos: PATCH, POST /proxies/batch, GET /proxies/{port}, ?include=status.
- CLI: batch/edit/export + mapa de equivalência Temporalitas. TUI Textual PT-BR (F01–F08 validadas em pty 80x24/60x20).
- Instalador: limpo/--update/migração/rollback/idempotente/--ref SHA/--cli-only/--tui-only/lock com hashes.
- Segurança: SSRF TCP e UDP corrigidos (ADR-0007), deps sem CVE, pins+hashes, actions por SHA.
- Docs: README, API, CLI, TUI, INSTALL, MIGRATION, TROUBLESHOOTING, limites medidos, PR_PROPOSAL.

## Testes (resultado real)
- QA final (main 6d9bd37/47f5d83): 3proxy real 3x 426 passed; sem 3proxy 397p/29s; acceptance 5x; TUI 5x; tui_validation 74 ok; pip-audit limpo;
  shellcheck 0.9/0.11 limpos. Release: 426 passed no main final 47f5d83.
- Falhas ao longo do caminho e correções: T08 (flaky F1/M2 → T20), revisão T09 (7 altos → T17/T18/T19), segurança T10/T22 (SSRF S1, N1 → T18/T23),
  T21 (Q1 shellcheck → T23), validação T11 (F01 emoji, F02 rodapé → T24), cosméticos (T28).

## Riscos / gates (NÃO satisfeitos)
- T14: execução em Linux limpo com systemd (instalação, upgrade aa29685/bdfe9c7 → final, 2ª execução), reboot REAL, tráfego por IPv6 roteado real,
  IPAddressDeny do drop-in. Tudo testado só em loopback com supervisor local.
- #152: push da branch/PR/CI não autorizados. Artefatos locais prontos (bundle, patches, PR_PROPOSAL).

## Próximo
Aguardar autorização/ambiente para T14 e push. Projeto NÃO será finalizado antes disso.
