# ADR 0009: Gates web do finish inaplicáveis (CLI/TUI sem produção)

- Data: 2026-10-13
- Status: aceito

## Contexto
Pós-CR-1 o finish exige dpm e2e run e dpm perf check contra URL de produção. Projeto é CLI/TUI/systemd, entrega local sem deploy (ADR-0008); não há URL.

## Decisão
Provisória: DESIGN.md + crítica visual feitos sobre a TUI em pty (T40). e2e browser e perf mobile NÃO serão simulados nem registrados falsamente; aguardam #168 (liberação do finish pelo DevProMax ou VPS/URL). Equivalentes reais já existentes: F01–F08 validadas em pty, E2E em VM Ubuntu 24.04.

## Alternativas
Servir TUI via textual-serve só para satisfazer o gate (rejeitado: não é o produto); registrar manual (rejeitado: falso)

## Consequências
Projeto fica aberto nos gates e2e/perf até decisão do Hermes/Otávio.
