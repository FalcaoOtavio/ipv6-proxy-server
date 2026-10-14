# ADR 0012: Gates web pulados por autorização do Otávio

- Data: 2026-10-14
- Status: aceito

## Contexto
finish recusava só por dpm e2e run e dpm perf check em URL de produção; produto CLI/TUI sem web. Otávio: 'Pode pular eles' (2026-10-14).

## Decisão
projects.has_ui=0 para este projeto (flag que ativa só os gates web). Nenhum teste foi registrado como aprovado sem execução.

## Alternativas
Registrar e2e/perf falsos (rejeitado)

## Consequências
Features F01–F08 seguem com status passed via pty; e2e browser e perf mobile não executados (não aplicáveis).
