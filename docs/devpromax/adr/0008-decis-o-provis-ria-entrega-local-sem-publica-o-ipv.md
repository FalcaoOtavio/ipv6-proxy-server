# ADR 0008: Decisão provisória: entrega local sem publicação; IPv6 roteado como critério NÃO satisfeito documentado

- Data: 2026-10-12
- Status: aceito

## Contexto
Pergunta #158 (caminho para o gate T14) expirou sem resposta. Hermes havia determinado: push/PR/CI só com autorização específica (#152 pendente); IPv6 roteado e reboot real não podem ser substituídos por simulação; nada de VPS/gastos. Encontrado ambiente descartável sem custo: VM QEMU TCG local (T29) cobre systemd real, instalação limpa, upgrades, 2ª execução e reboot REAL.

## Decisão
Opção mais conservadora: D + T29. Sem push, PR, CI remoto, VPS ou gastos. T29 fornece a evidência de systemd/reboot real em VM local. Tráfego por IPv6 ROTEADO real fica registrado como critério NÃO satisfeito (não 'pendência resolvida'), com procedimento pronto (tests/e2e/vm_e2e.sh com E2E_ROUTED_IPV6_TARGET) para execução na VPS do Otávio. A entrega final declara explicitamente: não 100% validado em rede real; publicação aguarda autorização (#152).

## Alternativas
A: push público sem autorização (violaria instrução do Hermes); B/C: dependem de recurso que o Otávio não forneceu; esperar indefinidamente (proibido pela regra de autonomia).

## Consequências
Projeto finaliza como entrega local verificada com limitações explícitas em README, PR_PROPOSAL, ISSUES (ISS-001) e relatório final. Revogável: com autorização, executar comandos de push/PR propostos e vm_e2e.sh com IPv6 roteado.
