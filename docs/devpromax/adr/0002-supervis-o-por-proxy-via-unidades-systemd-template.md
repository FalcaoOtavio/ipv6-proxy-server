# ADR 0002: Supervisão por proxy via unidades systemd template

- Data: 2026-10-11
- Status: aceito

## Contexto
Brief exige lifecycle, logs, readiness e recuperação por proxy, e que restart/upgrade da API ou falha de um proxy não interrompa os outros.

## Decisão
Cada proxy = ipv6-proxy@<porta>.service (3proxy foreground, config 0600 própria, Restart=on-failure, journal por unidade, hardening). API chama systemctl start/stop/restart apenas da instância alvo; readiness = porta aceitando conexão E MainPID da unidade alvo dono do socket. Reconciliação na partida adota unidades saudáveis sem reiniciá-las. Interface Supervisor com implementação LocalProcessSupervisor (Popen com start_new_session) para testes sem root/systemd.

## Alternativas
Popen filho da API (ashford: morre junto com a API); um 3proxy único com reload (desconecta todos); supervisord/s6 (nova dependência).

## Consequências
Migração da versão base exige uma interrupção inicial explícita (processos antigos no cgroup da API). Testes systemd reais só em VM/runner com systemd.
