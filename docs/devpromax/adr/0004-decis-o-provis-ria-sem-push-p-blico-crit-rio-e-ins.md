# ADR 0004: Decisão provisória: sem push público; critério E (instalador/systemd) fica como gate

- Data: 2026-10-11
- Status: aceito

## Contexto
Pergunta #152 (autorizar push da branch dpm/independent-proxies no fork público para rodar CI E2E) expirou sem resposta. Hermes exigiu autorização específica para push/publicação.

## Decisão
Opção conservadora: NÃO fazer push. Todo trabalho fica local na branch dedicada. Instalador e E2E systemd são escritos e testados localmente no que for possível (unit, fragmentos, shellcheck, 3proxy real em loopback); a execução em VM/runner com systemd fica como gate explícito pós-entrega, junto com IPv6 roteado e reboot real. Entrega = branch local + bundle/patch no zip + comando de push/PR proposto.

## Alternativas
Push da branch para CI (efeito público sem autorização); push + PR.

## Consequências
Critério E sem evidência em VM real até autorização; não alegar instalador validado ponta a ponta. Revogável: se o Hermes autorizar, T08 roda no Actions.
