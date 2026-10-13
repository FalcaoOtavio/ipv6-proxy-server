# ADR 0006: Parada de proxies órfãos só com intenção registrada (pending-ports.json)

- Data: 2026-10-12
- Status: aceito

## Contexto
M7 (T09): na partida, reconcile parava toda ipv6-proxy@* ausente de state.json; state.json restaurado antigo/vazio causava parada coletiva.

## Decisão
provision/deprovision gravam a porta em data_dir/pending-ports.json (fsync) antes de agir; o Manager limpa a porta após o commit de state.json (portas cercadas ficam pendentes). Na partida, só instâncias desconhecidas COM intenção são paradas; as demais são órfãs: continuam servindo, mantêm cfg/addr/IPv6, são logadas (WARNING com o comando de limpeza) e listadas em data_dir/orphans.json. Limpeza manual: systemctl disable --now ipv6-proxy@<porta>.service; rm proxies/<porta>.cfg proxies/<porta>.addr; o IPv6 é liberado no próximo start da API.

## Alternativas
Limite numérico/flag para parar >1 órfão (ainda para tenants por engano); nunca parar órfãos (deixa lixo de criação interrompida).

## Consequências
Nenhum state.json velho derruba proxies; órfãos exigem ação do operador; arquivo extra pending-ports.json no data_dir.
