# ADR 0001: Base de código: branch ashford/isolated-mutations (bdfe9c7) em branch dedicada

- Data: 2026-10-11
- Status: aceito

## Contexto
Hermes reportou que ashford/isolated-mutations (bdfe9c7634691f51e893f6e53b6137f95792e4f5, 2026-10-05) está 4 commits à frente e 0 atrás de main (aa2968556f861f56c7e5d37c70a859797a8d8772). Ela já tem 3proxy por porta (Popen filho da API), rollback restrito com fencing por porta, identidade imutável (id + If-Match), Idempotency-Key atômica, owner scoping (PROXY_CLIENT_TOKENS_FILE), /capabilities e state.json v2 com migração de proxies.json. CI dela falha: 202 passed, 1 error (tests/test_hermes_failure_isolation.py — ValidationError de Settings no fixture). Lacuna: processos 3proxy ficam no cgroup da API (KillMode=control-group), logo restart/upgrade da API derruba todos os proxies; logs compartilhados em 3proxy.log; sem edição, lote, TUI, instalador de update.

## Decisão
Origem = bdfe9c7. Criar branch dedicada dpm/independent-proxies a partir dela, sem reset/overwrite da branch original nem de main. Preservar contratos (id imutável, If-Match, Idempotency-Key, owner scoping, capabilities, state.json v2). Corrigir o teste quebrado, mover cada proxy para unidade systemd template ipv6-proxy@<porta> (fora do cgroup da API, logs/readiness/restart por proxy) e acrescentar edição, lote, TUI e instalador. Base registrada: main=aa29685…, origem=bdfe9c7…, final=a definir na entrega.

## Alternativas
Partir de main aa29685 (regride contratos já feitos, descartado); estender a própria branch ashford (proibido alterar a original).

## Consequências
PR alvo main conterá também os 4 commits ashford; Ashford Hub não é alterado. Contratos ashford viram regressão obrigatória nos testes.
