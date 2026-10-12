# Instalacao

Execute na VPS Ubuntu 22.04/24.04/26.04 ou Debian 12/13, com systemd, acesso
sudo, IPv4 e subnet IPv6 dedicada roteada pelo provedor. O instalador nao cria
VPS nem contrata subnet. Fonte unica: `FalcaoOtavio/ipv6-proxy-server` (nunca o
upstream).

## Instalacao limpa

Fixe a revisao com `--ref` (branch, tag ou SHA). O instalador resolve o ref para
UM SHA, faz checkout desse SHA e grava `sha`, `ref` e data em
`/opt/ipv6-proxy-server/VERSION`.

```bash
REF=main   # ou uma tag, ou o SHA de 40 caracteres da versao aprovada
curl -fsSLo install-ipv6-proxy.sh "https://raw.githubusercontent.com/FalcaoOtavio/ipv6-proxy-server/$REF/install.sh"
sudo bash install-ipv6-proxy.sh --ref "$REF"
```

Sem terminal (automacao), informe a rede:

```bash
sudo bash install-ipv6-proxy.sh --ref "$REF" --subnet 2001:db8:1234::/64 --interface eth0 --ipv4 192.0.2.10 --self-test
```

Com terminal, o instalador pergunta subnet, interface e IPv4, sugerindo os
locais. Nao deduza a subnet dedicada a partir de um IPv6 da VPS.

O instalador:

1. valida sistema, systemd, subnet (prefixo <= 126, roteavel), interface, IPv4
   presente na interface e porta 127.0.0.1:8000 livre;
2. recusa 3proxy de outro gerenciador (lista os PIDs, nao encerra nenhum) e
   instalacao legada por cron (`proxy-startup.sh`, `/root/proxyserver`);
3. instala dependencias via apt e compila o 3proxy 0.9.4 fixado
   (SHA `346880eb2e73836385e5f7648f738efcece580ac`) em `/usr/local/bin/3proxy`;
4. instala a API e o venv em `/opt/ipv6-proxy-server` (o venv tambem roda o
   `ExecStartPre` de cada proxy), as unidades `ipv6-proxy-api.service` e
   `ipv6-proxy@.service`, o CLI `/usr/local/bin/ipv6-proxy` e, se presente na
   revisao, a TUI `/usr/local/bin/ipv6-proxy-tui`;
5. gera o token (`openssl rand -hex 32`) em `/etc/ipv6-proxy-api.env` (0600,
   root) e habilita a API no boot;
6. healthcheck: API autenticada responde, cada proxy persistido esta ativo como
   `ipv6-proxy@<porta>` e nenhum 3proxy ficou no cgroup da API. Com
   `--self-test`, cria um proxy de teste com senha aleatoria, confere que fica
   `running` e o remove (com `If-Match`), sem tocar proxies existentes;
7. imprime a revisao instalada e o comando para reinstalar a mesma revisao.

O token nunca aparece em argumentos de processo nem nos logs: o instalador o
passa ao curl por arquivo de configuracao temporario.

## Arquitetura instalada

Cada proxy e uma unidade systemd propria, `ipv6-proxy@<porta>.service`, fora
do cgroup da API. Reiniciar, atualizar ou parar a API nao derruba proxies; um
proxy que falha nao afeta os outros. No boot, cada unidade habilitada recoloca
so o proprio IPv6 antes de subir (endereco fora do journal da API e recusado).

```bash
systemctl list-units 'ipv6-proxy@*'
sudo journalctl -u ipv6-proxy@30000 -n 50     # log de um proxy
sudo journalctl -u ipv6-proxy-api -n 50       # log da API
```

## Atualizar

```bash
REF=<sha-ou-tag-aprovado>
curl -fsSLo install-ipv6-proxy.sh "https://raw.githubusercontent.com/FalcaoOtavio/ipv6-proxy-server/$REF/install.sh"
sudo bash install-ipv6-proxy.sh --update --ref "$REF" --self-test
```

- Faz backup de app, env, estado, unidades, CLI/TUI e binario 3proxy em
  `/var/backups/ipv6-proxy-<data>-<pid>/` (0700) antes de mudar qualquer coisa.
- Reinicia **somente a API**. A API reconcilia na partida: adota os proxies
  saudaveis (mesmo PID) e reinicia apenas um proxy cuja configuracao gerada
  mudou. O instalador confere os PIDs antes e depois e faz rollback se um proxy
  sem mudanca de configuracao for reiniciado.
- Se `ipv6-proxy@.service` mudar, nenhum proxy e reiniciado; cada um adota a
  nova unidade no proximo reinicio dele (edicao, rotacao ou reboot).
- Falha (erro, healthcheck ou PID trocado): restaura o backup automaticamente e
  reinicia a API anterior.
- Reexecutar sem mudancas nao altera nada (token, env, estado, unidades, PIDs)
  e nao reinicia servicos: imprime `Nada a atualizar`.
- `--subnet/--interface/--ipv4` nao sao aceitos em instalacao existente; o
  token nunca e regenerado.

Migracao da versao base (aa29685/main ou bdfe9c7/ashford): veja
[MIGRATION.md](MIGRATION.md). E a unica atualizacao que interrompe proxies.

## Somente CLI ou TUI

Para uma API ja instalada, a partir de um checkout completo do fork:

```bash
git clone https://github.com/FalcaoOtavio/ipv6-proxy-server.git ipv6-proxy-src
cd ipv6-proxy-src && git checkout --detach "$REF"
sudo bash install.sh --cli-only
sudo bash install.sh --tui-only
```

Esses modos nunca chamam `systemctl`: nao reiniciam API nem proxies e nao
alteram token, env, estado ou unidades. `--cli-only` substitui atomicamente
apenas `/usr/local/bin/ipv6-proxy` (root:root 0755). `--tui-only` instala o
pin de `textual` do `requirements.txt` no venv da API, `proxy_tui/` em
`/opt/ipv6-proxy-server` e `/usr/local/bin/ipv6-proxy-tui`. Ambos recusam
sobrescrever arquivo desconhecido, symlink ou com permissoes inesperadas.

## Acessar e usar

```bash
sudo ipv6-proxy list
sudo ipv6-proxy create --port 30000 --protocol http --username cliente
```

Cada mutacao reinicia somente o proxy afetado; as sessoes das outras portas
continuam. Swagger: abra um tunel `ssh -L 8000:127.0.0.1:8000 usuario@IP` e use
<http://127.0.0.1:8000/docs> com o token de `sudo cat /etc/ipv6-proxy-api.env`.
Libere no firewall so as portas dos proxies, para clientes autorizados. A API
nao altera firewall, rota padrao, sysctl nem ndppd; nao exponha a porta 8000
sem HTTPS e controle de acesso. Contrato completo em [API.md](API.md).

## Limitacoes

- `PROXY_DATA_DIR` e `PROXY_3PROXY_BINARY` devem ficar nos padroes
  (`/var/lib/ipv6-proxy-api`, `/usr/local/bin/3proxy`): as unidades por proxy
  usam esses caminhos; o instalador recusa outros valores.
- Nao altere subnet ou interface com proxies existentes; remova-os pela API
  antes. O teste de instalacao nao comprova roteamento/NDP externo.
- Legado (`ipv6-proxy-server.sh`) e API nao podem coexistir. Desative o cron
  legado e pare o processo legado especifico antes de instalar; nao existe
  importacao automatica do pool legado.
