# Solução de problemas

Cada proxy é a unidade `ipv6-proxy@<porta>.service`; a API é
`ipv6-proxy-api.service`. Diagnostique **por porta**: um proxy com problema não
afeta os outros, e nenhum comando abaixo reinicia o pool inteiro.

## Visão geral

```bash
sudo ipv6-proxy list                               # STATUS por porta; avisa portas fora do ar
systemctl list-units --all 'ipv6-proxy@*'          # estado de cada unidade
sudo systemctl status ipv6-proxy@30000             # uma porta
sudo journalctl -u ipv6-proxy@30000 -n 50          # log de uma porta (3proxy + ExecStartPre)
sudo journalctl -u ipv6-proxy-api -n 100           # log da API (reconciliação, órfãos, falhas)
cat /opt/ipv6-proxy-server/VERSION                 # revisão instalada
```

Status por porta (`list`, `GET /proxies?include=status`):

| Status | Significado | Ação |
|---|---|---|
| `running` | unidade ativa e servindo a config atual | — |
| `starting` | subindo ou aguardando `Restart=on-failure` (2 s) | aguarde; persistindo, veja o log da porta |
| `failed` | unidade parada/morta | [Porta `failed`](#porta-failed) |
| `fenced` | isolada após dupla falha (operação e compensação) | [Porta `fenced`](#porta-fenced) |

`GET /proxies` sem `?include=status` responde 503 se algum proxy visível não
está saudável; CLI, TUI e instalador usam `?include=status`, que sempre lista.

## Porta `failed`

1. Veja a causa: `sudo journalctl -u ipv6-proxy@<porta> -n 50`.
   - `ExecStartPre` falhou: o IPv6 da porta não está no journal
     `owned-addresses.json` ou `proxies/<porta>.addr` sumiu. Não edite esses
     arquivos; reinicie a API (abaixo) para ela regravar o `.addr`.
   - `bind`/`Address already in use`: outro processo ocupa a porta
     (`sudo ss -ltnp 'sport = :<porta>'`).
2. Unidade travada no limite de partidas (10 em 300 s):
   ```bash
   sudo systemctl reset-failed ipv6-proxy@<porta>
   sudo systemctl start ipv6-proxy@<porta>
   ```
   Só essa porta é afetada.

## Porta `fenced`

Quando uma mutação e a compensação dela falham, a API responde 503 e isola
somente aquela porta: ela sai do ar, fica `fenced` e recusa mutações (503).
O estado em disco fica no último commit; as demais portas seguem servindo e
mutando.

A porta também fica `fenced` quando o reconcile na partida da API não consegue
subi-la no prazo. `GET /proxies` (sem `?include=status`) responde 503 citando
a porta cercada.

Recuperação:

1. Corrija a causa no log (`journalctl -u ipv6-proxy-api` e
   `journalctl -u ipv6-proxy@<porta>`).
2. Recupere só essa porta, sem reiniciar a API (token admin):
   ```bash
   curl -sS -X POST -H "Authorization: Bearer $PROXY_API_TOKEN" \
     http://127.0.0.1:8000/proxies/<porta>/recover
   ```
   Se a instância já estiver no ar com a config gravada, é adotada (mesmo PID);
   senão é iniciada. Nenhuma outra porta é tocada. 503 = ainda cercada: volte
   ao passo 1.
3. Alternativa: reinicie a API:
   ```bash
   sudo systemctl restart ipv6-proxy-api
   sudo ipv6-proxy list
   ```
   Reiniciar a API **não derruba proxies**: na partida ela adota as instâncias
   saudáveis (mesmo PID) e recoloca só a porta cercada conforme o estado
   gravado. Se a porta continuar `fenced`/`failed`, repita o passo 1.
4. Se todas as mutações responderem 503 `Recovery required; restart service
   and inspect logs`, a API falhou ao regravar o estado em disco (processos
   continuam servindo): verifique disco/permissões de
   `/var/lib/ipv6-proxy-api` e reinicie a API.

## Proxy órfão

Instância `ipv6-proxy@<porta>` rodando que não está em `state.json` (ex.:
estado restaurado antigo). A API **não** a para: ela continua servindo, é
registrada em `/var/lib/ipv6-proxy-api/orphans.json` e o log da API traz um
WARNING com o comando de limpeza. Se a porta realmente não deve existir:

```bash
sudo systemctl disable --now ipv6-proxy@<porta>.service
sudo rm /var/lib/ipv6-proxy-api/proxies/<porta>.cfg /var/lib/ipv6-proxy-api/proxies/<porta>.addr
```

## Timeout ou resultado desconhecido no CLI (código 6)

Não repita às cegas. Repita **o mesmo comando** com a chave mostrada na
mensagem: `--idempotency-key K` (e `--if-match ID` em edit/rotate/delete). A
API devolve o resultado gravado sem duplicar. Senhas `--random` não voltam no
replay; redefina com `ipv6-proxy edit <porta> -u <usuario>`.

## IPv6 de saída não funciona

A API só adiciona `/128` na interface; rota e NDP são do provedor. Teste de
fora para dentro:

```bash
ip -6 addr show dev eth0 | grep -c '/128'        # endereços dos proxies presentes
ip -6 route get 2606:4700:4700::1111             # há rota IPv6 padrão?
curl -6 --interface <ipv6-da-porta> https://api64.ipify.org   # sai por esse IPv6?
sudo tcpdump -ni eth0 icmp6 and 'ip6[40] == 135'  # NS do roteador chegando para os /128?
```

- `curl --interface` falha e não há rota padrão: problema de rede da VPS, não
  do proxy.
- Neighbor Solicitations para os `/128` chegam sem resposta, ou só o IPv6
  principal funciona: o provedor não roteia a subnet. Peça a subnet roteada ou
  configure um proxy NDP (`ndppd`, regra `static` para a subnet na interface
  externa). O projeto não configura ndppd.
- `curl --interface` funciona mas o proxy não: veja o log da porta e o status.
- Destino sem AAAA falha por desenho (`-6`, sem fallback IPv4).

## HTTP 403 / SOCKS5 recusado para alguns destinos

Com `PROXY_BLOCK_PRIVATE_DESTINATIONS=true` (padrão), destinos em loopback,
`::ffff:0:0/96`, `fe80::/10`, `fc00::/7`, `ff00::/8` e a própria subnet são
negados. É proteção anti-SSRF; não desligue em produção.

## Liguei/desliguei o bloqueio e nada mudou

Aplique com `sudo bash install.sh --update --yes` (mesmo na revisão
instalada): ele reaplica a config dos proxies afetados, um por vez, e verifica
o resultado. Conferir à mão: `grep -c '^deny' /var/lib/ipv6-proxy-api/proxies/<porta>.cfg`
(2 com o bloqueio ligado, 0 desligado). Erros do instalador:

- `configuracao dos proxies nao corresponde ... (portas: ...)`: a API não
  regravou esses `.cfg`; veja `journalctl -u ipv6-proxy-api -n 100` (porta
  isolada/`fenced`?) e repita após corrigir.
- `ipv6-proxy@<porta>.service nao foi reiniciado (MainPID ...)`: o `.cfg` mudou
  mas o processo segue com a config antiga. Rollback já feito; repita o
  `--update` ou `sudo systemctl restart ipv6-proxy@<porta>` e confira.

## SOCKS5 UDP ASSOCIATE não responde

Esperado com o bloqueio anti-SSRF ligado: UDP ASSOCIATE é negado e
`GET /capabilities` não anuncia `socks5_udp_associate` (ADR-0007). Use TCP. UDP
só funciona com `PROXY_BLOCK_PRIVATE_DESTINATIONS=false`, que expõe o host a
SSRF via relay UDP.

## Atualização falhou / rollback

O `--update` faz backup em `/var/backups/ipv6-proxy/<data>-<pid>/` e, em
qualquer falha (erro, healthcheck, PID de proxy sem mudança trocado), restaura
sozinho e inicia a API anterior. Cada passo aparece como `ok` ou `FALHOU`.

- Todos `ok`: nada a fazer; corrija a causa e rode o `--update` de novo.
- Algum `FALHOU`: conclua com o rollback manual em
  [MIGRATION.md](MIGRATION.md#rollback-manual).
- `Sem terminal para confirmar`: a revisão reinicia proxies (ou é a migração);
  rode de novo com `--yes` após avaliar o anúncio de portas afetadas.
- Falha só no `--self-test` (após a atualização confirmada) é reportada sem
  rollback: verifique com `sudo ipv6-proxy list`.
- Instalação limpa interrompida com "Instalacao incompleta": remova os
  caminhos listados e repita ([INSTALL.md](INSTALL.md#instalacao-interrompida)).

## Host lento: proxy não fica pronto

Sintoma: criar/rotacionar falha com `3proxy for port N did not become ready in
30s (PROXY_READY_TIMEOUT ...)`, a porta fica `fenced` após reiniciar a API, ou o
instalador acusa "API nao respondeu" / falha no `--self-test`. Em VPS pequenas
ou VMs emuladas `systemctl start ipv6-proxy@N` pode levar 15–20 s e a API mais
de 2 min para subir.

- Aumente `PROXY_READY_TIMEOUT` (segundos por proxy, 1..300, padrão 30) em
  `/etc/ipv6-proxy-api.env` e rode `sudo systemctl restart ipv6-proxy-api`.
  O prazo vale por proxy, também no reconcile de muitos (não se soma).
- No instalador, aumente a espera pela API com `--api-timeout S` (padrão 180;
  soma 5 s por proxy). O `--max-time` das chamadas acompanha
  `PROXY_READY_TIMEOUT` + 30 s.
- Chamadas `systemctl` da API têm prazo `max(30, PROXY_READY_TIMEOUT + 15)` s.
  Se um `systemctl start` estoura esse prazo, a API não cerca a porta por isso:
  ela reconsulta a unidade (ativa + PID dono do socket) e adota se subiu. Porta
  que ainda assim ficou `fenced`: [recupere](#porta-fenced) com
  `POST /proxies/<porta>/recover`.

## Instalador: lock do apt ocupado

Sintoma: `Aguardando apt de outro processo (PID X)...` e depois
`Erro: apt de outro processo (PID X) ainda segura /var/lib/apt/lists/lock apos N s`
(ou, antes da correção, `E: Could not get lock /var/lib/apt/lists/lock`, rc=100).
Comum logo após o 1º boot: apt-daily/unattended-upgrades está rodando.

- Veja o processo: `ps -o pid,etime,cmd -p X`. O instalador nunca o encerra.
- Espere terminar (`systemctl status apt-daily.service unattended-upgrades.service`)
  e execute o mesmo comando de novo; nada foi instalado.
- Ou aumente a espera: `sudo bash install.sh --apt-lock-timeout 1800 ...` (0..7200 s).

## Ainda não validado

Comportamento com systemd real em Linux limpo, após reboot real e com IPv6
roteado pelo provedor não tem evidência ainda (T14). Ao relatar problema
nesses cenários, anexe `journalctl -u ipv6-proxy-api`, o log da porta e
`/opt/ipv6-proxy-server/VERSION`.
