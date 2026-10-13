# ADR 0005: Endereço IPv6 por instância no boot e parada rápida do 3proxy

- Data: 2026-10-11
- Status: aceito

## Contexto
Após reboot, os /128 adicionados pela API somem e as unidades ipv6-proxy@N (enabled) sobem sem a API: o 3proxy escutaria e sairia por endereço inexistente. Além disso, o 3proxy 0.9.4 leva ~5s para sair com SIGTERM (laço interno com usleep), e cada edit/rotate/delete pagava esse tempo.

## Decisão
(1) Cada instância tem ExecStartPre=+ que chama proxy_api/instance_address.py %i: lê proxies/<porta>.addr (ipv6+interface, escrito pela API, 0600) e só adiciona o endereço se ele estiver em owned-addresses.json; já presente => nada (idempotente); ausente do journal => falha e a unidade não sobe. Não há ExecStopPost: stop, boot e shutdown não removem endereço; só o delete pela API libera. (2) KillSignal=SIGINT e TimeoutStopSec=3 por instância (SIGKILL de fallback); LocalProcessSupervisor usa o mesmo sinal, com fallback em 2s. Medido com 3proxy real: SIGTERM 5051ms, SIGINT 2ms, SIGQUIT 549ms. Mediana por operação: rotate 5780->1014ms, edit 5525->491ms, edit de porta 5779->699ms, delete 5330->261ms.

## Alternativas
Drop-in por instância com o IPv6 (ExecStartPre=ip addr add): mais arquivos em /etc e não consulta o journal de propriedade. Unit única de restauração no boot: coletiva, viola a independência. Esperar a API recolocar os endereços: proxy sairia por IP inexistente até lá. Manter SIGTERM: ~5s por mutação.

## Consequências
Boot independente da API, por proxy, nunca adiciona endereço alheio. Ao parar, a sessão cai na hora (antes caía após ~5s, sem shutdown gracioso real no 3proxy). Depende de /opt/ipv6-proxy-server/.venv/bin/python, a ser instalado por T07, que também precisa instalar ipv6-proxy@.service. Instalações de T02 ganham o .addr na reconciliação da 1ª partida.
