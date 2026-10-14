# ADR 0010: Decisão provisória (#178): finish pendente de bypass dos gates web

- Data: 2026-10-13
- Status: aceito

## Contexto
#178 expirou. finish recusa só por dpm e2e run e dpm perf check em URL de produção; produto CLI/TUI sem deploy (ADR-0008); #168 liberou finish sem web, mas o bypass não foi aplicado no DevProMax.

## Decisão
Opção conservadora: não simular registros nem provisionar VPS (gasto/VPS proibidos sem autorização). Entrega técnica considerada concluída no SHA 6b1d3ab (branch local dpm/independent-proxies, dist/ verificado). Bypass dos 2 gates no DevProMax vira pendência pós-entrega (ISSUES/LEARNINGS). Orquestrador não altera o DevProMax (fora do escopo e da regra 1).

## Alternativas
VPS/URL de teste (rejeitado: gasto/VPS não autorizados); registrar testes manuais falsos (rejeitado: regra 2/3/9)

## Consequências
Projeto fica com finish pendente até o bypass ser aplicado; alertas PARADO seguirão sem ação possível.
