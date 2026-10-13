# Estado do orquestrador — IPv6 Proxy Server Independente

> Memória durável do orquestrador (sobrevive a /compact e reinícios).

- Fase: PLANEJAMENTO (plano submetido 2026-10-11).
- Base: FalcaoOtavio/ipv6-proxy-server main = aa2968556f861f56c7e5d37c70a859797a8d8772 (verificado 2026-10-11).
- repo/ do DPM está vazio (bootstrap); T01 importa o fork. Entrega = branch `dpm/independent-proxies` no fork + PR.
- GitHub App do DPM tem push no fork (dry-run ok) e lê logs de Actions; gh CLI ausente (usar API REST com token do credential helper).
- Host sem root/docker/KVM: E2E systemd/instalador só no GitHub Actions do fork. Sem IPv6 roteado real (limitação declarada).
- Pergunta aberta: tratamento da branch ashford/isolated-mutations (não bloqueante; default = ignorar).
- 2026-10-11 [HERMES] ref IPV6-INDEPENDENTE-20261011: origem = ashford/isolated-mutations bdfe9c7 (ADR-0001), branch dedicada dpm/independent-proxies.
  Proibido sem autorização: push público, merge main, release, gastos, VPS, alterar Ashford Hub/credenciais. Não usar `dpm github create-repo`.
  ADR-0002 systemd template por proxy; ADR-0003 TUI Textual. #152 expirou: ADR provisória = sem push; E é gate pós-entrega (ISSUES).
- 2026-10-11 PLANO APROVADO. Correções Hermes aplicadas: state.json v2 fonte única; DAG T01–T15 registrado; T14 = GATE real (reboot/IPv6/VM),
  sem project finish antes dele; TUI viewports = pty 80x24/60x20; repo_url = FalcaoOtavio/ipv6-proxy-server (sem create-repo).
  Contexto comum dos agentes: tasks/CONTEXT.md; specs fonte: tasks/specs/.
- Sprint 1 = T01→T02→T03→T04 (sequencial, 1 dev "core" + tester "qa").
- Sprint 1: T01–T03 mergeados (cp-0003). ADR-0005 = endereço restaurado por instância via ExecStartPre=+ (instance_address.py). KillSignal=SIGINT.
- Em paralelo: core→T07 (instalador), qa→T04, cli→T05, tui→T06.
- Sprint 2 dev concluído: T04 (cp-0004), T05 (cp-0005), T07 (cp-0006), T13 (cp-0007), T06 (cp-0008). T16 (CI legado) com qa → depois T08.
- Revisão T09 (rev) e segurança T10 (sec) em paralelo, report-only. Depois: T08 QA, T11 validação TUI, T12 docs, T15 entrega local. T14 = GATE.
- T09 revisão = AJUSTES (docs/reviews/T09-review.md): 7 altos, 9 médios. Correções: T17 inst (instalador), T18 back (backend/deploy, inclui M7 parada
  de órfãos), T19 clients (CLI/TUI), T20 (testes, qa após T08). Depois: re-QA completa (nova rodada T08) e re-revisão rápida.
- T08 QA (cp-0010): full com 3proxy real 358p/1f, 359p, 359p — registrado como FALHOU (flaky F1 + M2) → T20 (qa). Limites em docs/evidence/limits.md.
- T10 sec achou SSRF ALTO preexistente (auth none/allow * sem ACL de destino; [::1]/fc00::/7 alcançáveis). Decisão: fix no T18 (back) com
  auth iponly + deny de faixas privadas + subnet própria, padrão ligado; T17 (inst) anuncia/--yes reinício um a um no update. sec só reporta.
- 2026-10-12: T17 (cp-0012), T19 (cp-0013), T18 (cp-0014) mergeados. SSRF corrigido (ADR 0006 = órfãos). Falta T20 (qa) → depois re-QA full,
  re-auditoria de segurança (sec rápido sobre S1/S2/A6), T11 validação TUI pty, T12 docs, T15 entrega local. T14 GATE.
- cp-0015: T20 mergeado. Agora: T21 re-QA (qa), T22 reauditoria (sec2), T11 validação TUI pty (val). Depois T12 docs, T15 entrega. T14 GATE.
- 2026-10-12: T12 docs (cp-0018), T11 validação TUI (cp-0019: F03–F08 passed; F01 emoji/markup, F02 rodapé → T24 tuifix), T23 N1+Q1 (cp-0020).
  Falta: T24 → revalidar F01/F02 (val) → re-QA curta + reauditoria curta (N1) → T15 entrega local. T14 GATE.
- 2026-10-12: TRABALHO LOCAL CONCLUÍDO. main 47f5d83 (cp-0025) + PR_PROPOSAL (750a8c5, cp-0026); sprint fechada (cp-0027). full PASS, security PASS,
  features F01–F08 passed. Branch local dpm/independent-proxies=47f5d83; dist/ bundle+patches+SHA256SUMS. Fork intocado (ls-remote conferido).
  ABERTO: T14 GATE + #152. Pergunta #158 (A push+CI / B VM com IPv6 / C ambos / D encerrar parcial). NÃO rodar `dpm project finish` antes da decisão:
  se D => finalizar com gates documentados como não satisfeitos; se A/B => executar T14 e corrigir.
  T29 (vm): E2E em VM QEMU TCG local — cobre systemd real + reboot real; IPv6 roteado continua só com B (VM/VPS do Otávio).
- #158 timeout → ADR-0008 (provisória): D + T29. Sem push/PR/VPS. Ao terminar T29: T14 fica done com nota "systemd/reboot real em VM local;
  IPv6 roteado NÃO satisfeito" e seguimos para project finish com limitações explícitas.
- E2E VM (T29) achou 2 bugs reais → T30 (READY_TIMEOUT, cp-0028) e T32 (timeout systemctl/fenced + /recover, cp-0029). T33 teste TUI f07 (cp-0030).
  Main = 55045144. vm reexecutando rodada obrigatória no SHA novo. Depois: QA curta final + T14 done + docs/PR_PROPOSAL/dist regenerados + finish.
- 2026-10-13: T29 E2E VM PASS (cp-0033), T36 release (cp-0034). Final = 139a726d1ad0f4ff84a7eb97fd910db6dd486e5b (branch local dpm/independent-proxies);
  dist/ regenerado e verificado. Fork intocado. Restante declarado: IPv6 roteado, Debian/Ubuntu 22.04/26.04 não executados, push/PR #152.
