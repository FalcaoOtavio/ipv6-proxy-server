# IPv6 Proxy Server API

API, CLI e TUI para criar, listar, editar, rotacionar e remover proxies HTTP e
SOCKS5, cada um com um IPv6 de saída fixo por porta IPv4. Extensão FastAPI do
projeto de [Temporalitas](https://github.com/Temporalitas/ipv6-proxy-server),
sob licença MIT.

> **Estado de validação:** testado localmente com 3proxy 0.9.4 real em
> loopback. **Ainda não executado em Linux limpo com systemd, nem com reboot
> real, nem com IPv6 roteado pelo provedor** (gate T14). Veja
> [Limitações e gates](#limitações-e-gates) antes de usar em produção.

## Arquitetura

```
cliente ──IPv4:porta──▶ ipv6-proxy@<porta>.service (3proxy próprio, cfg 0600)
                                   │ sai por um IPv6 /128 fixo da subnet
                                   ▼
                               destino IPv6

ipv6-proxy-api.service (FastAPI, 127.0.0.1:8000) ──systemctl──▶ só a unidade alvo
  ▲            ▲            ▲
 CLI       TUI        curl/Swagger      (mesmo token, mesmo estado, mesma fila)
```

- **Um processo 3proxy por proxy**, cada um na unidade systemd
  `ipv6-proxy@<porta>.service`, fora do cgroup da API
  ([ADR-0002](#decisões)). Logs, readiness e `Restart=on-failure` são por porta.
- A API serializa as mutações, grava o estado em
  `/var/lib/ipv6-proxy-api/state.json` (rename atômico) e só chama `systemctl`
  na instância alvo.
- No boot, cada unidade habilitada recoloca **somente o próprio IPv6** antes de
  subir (`ExecStartPre`), sem depender da API; endereço fora do journal
  `owned-addresses.json` é recusado (ADR-0005).
- Na partida, a API **adota** instâncias saudáveis sem reiniciá-las. Instâncias
  desconhecidas só são paradas se havia intenção registrada em
  `pending-ports.json`; as demais ficam servindo e são listadas como órfãs
  (ADR-0006).
- CLI (`ipv6-proxy`) e TUI (`ipv6-proxy-tui`) são clientes HTTP da mesma API,
  não gerenciadores paralelos.

## Sessões ativas por operação

Medido com 3proxy real e supervisor local (`tests/test_isolation_3proxy.py`,
`tests/test_supervisor_3proxy.py`, `tests/acceptance/test_criteria_ab.py`:
streams HTTP CONNECT e SOCKS5 vivos em outras portas, mesmo PID antes/depois).

| Operação | Porta afetada | Outras portas |
|---|---|---|
| `create` / `batch` | porta nova sobe | intactas |
| `edit` / `rotate` | sessões da porta caem (reinício do 3proxy dela) | intactas |
| `delete` | sessões da porta caem | intactas |
| Falha/morte do 3proxy de uma porta | fica `failed`/`fenced` | intactas |
| Reiniciar/atualizar a API sem mudar a config gerada | nenhuma | intactas |
| Atualização que muda a config gerada (ex.: nova ACL) | cada porta afetada reinicia **uma vez, uma por vez**, após confirmação | intactas |
| Migração da versão base (aa29685/bdfe9c7) | **todas caem uma única vez** | — |
| Reboot | todas (não validado em host real: T14) | — |

Não existe mais reinício coletivo em mutações: o aviso antigo ("toda mutação
reinicia todos os proxies") valia para a versão base com um 3proxy único.

## Instalação

VPS **Ubuntu 22.04/24.04/26.04 ou Debian 12/13** com systemd, sudo, IPv4 e uma
**subnet IPv6 dedicada roteada pelo provedor**. Fixe a revisão com `--ref`
(branch, tag ou SHA; o instalador grava o SHA resolvido em
`/opt/ipv6-proxy-server/VERSION`):

```bash
REF=<sha-ou-tag-aprovado>
curl -fsSLo install-ipv6-proxy.sh "https://raw.githubusercontent.com/FalcaoOtavio/ipv6-proxy-server/$REF/install.sh"
sudo bash install-ipv6-proxy.sh --ref "$REF" --self-test
```

Sem terminal: `--subnet 2001:db8:1234::/64 --interface eth0 --ipv4 192.0.2.10`.
O instalador compila o 3proxy 0.9.4 fixado, instala API, unidades, CLI e TUI,
gera o token em `/etc/ipv6-proxy-api.env` (root, 0600) e roda o healthcheck.
Detalhes: [docs/INSTALL.md](docs/INSTALL.md).

### Atualizar

```bash
curl -fsSLo install-ipv6-proxy.sh "https://raw.githubusercontent.com/FalcaoOtavio/ipv6-proxy-server/$REF/install.sh"
sudo bash install-ipv6-proxy.sh --update --ref "$REF" --self-test
```

- Backup em `/var/backups/ipv6-proxy/<data>-<pid>/` antes de mudar algo;
  rollback automático em qualquer falha.
- Reinicia **somente a API**; proxies saudáveis são adotados (mesmo PID).
- Se a nova revisão muda a configuração gerada de proxies, o instalador
  anuncia quantos serão reiniciados (um por vez) e exige digitar `REINICIAR`
  ou passar `--yes`. Sem confirmação, nada muda.
- Sem mudanças: imprime `Nada a atualizar` e não reinicia nada.

### Migração da versão base

Instalações `aa29685` (main antigo) ou `bdfe9c7` (`ashford/isolated-mutations`)
rodam os 3proxy dentro da API. O `--update` migra cada proxy para sua unidade,
preservando portas, IPv6, credenciais, owners e token, com **uma interrupção
única de todos os proxies**. Exige digitar `MIGRAR` ou `--yes`. Ver
[docs/MIGRATION.md](docs/MIGRATION.md).

## Uso

```bash
sudo ipv6-proxy list
sudo ipv6-proxy create --port 30000 --protocol http --username cliente   # senha sem eco
sudo ipv6-proxy rotate 30000
sudo ipv6-proxy delete 30000
sudo ipv6-proxy-tui
```

### 100 HTTP e 100 SOCKS5 com export

```bash
# 100 HTTP, senha comum (lida de arquivo, nunca em argv), lista em arquivo 0600
sudo ipv6-proxy batch -c 100 --start-port 30000 -t http -u equipe \
  --password-stdin --yes -f /root/http.list < /root/senha-comum.txt

# 100 SOCKS5, senha aleatória por proxy (usuário socks<porta>)
sudo ipv6-proxy batch -c 100 --start-port 31000 -t socks5 --random -u socks \
  --yes -f /root/socks5.list

# Formato: IPv4:porta:usuario:senha. Teste uma linha:
IFS=: read -r ip port user pass < /root/socks5.list
curl --proxy "socks5h://$ip:$port" --proxy-user "$user:$pass" https://api64.ipify.org

# Exportar o pool existente (sem senhas: a API nunca devolve senhas)
sudo ipv6-proxy export --format json
```

As senhas só aparecem na criação; guarde a saída do `batch` (`-f`). Com
`--random`, um replay idempotente após timeout não devolve as senhas: redefina
com `ipv6-proxy edit <porta> -u <usuario>`. Referência completa:
[docs/CLI.md](docs/CLI.md), [docs/TUI.md](docs/TUI.md), [docs/API.md](docs/API.md).

Swagger: `ssh -L 8000:127.0.0.1:8000 usuario@IP` e abra
<http://127.0.0.1:8000/docs> com o token de `sudo cat /etc/ipv6-proxy-api.env`.
Não exponha a porta 8000; libere no firewall só as portas dos proxies, para
clientes autorizados.

## Proteção anti-SSRF e UDP

`PROXY_BLOCK_PRIVATE_DESTINATIONS=true` (padrão, em `/etc/ipv6-proxy-api.env`)
nega destinos em loopback, `::ffff:0:0/96`, `fe80::/10`, `fc00::/7`,
`ff00::/8` e a própria subnet dos proxies: o cliente recebe HTTP 403 (HTTP) ou
recusa (SOCKS5). Mantenha ligado em produção.

Com o bloqueio ligado, **SOCKS5 UDP ASSOCIATE é negado** e `GET /capabilities`
**não** anuncia `socks5_udp_associate` (ADR-0007): a ACL do 3proxy só verifica o
endereço declarado no ASSOCIATE, e o relay poderia entregar datagramas ao
loopback do host. UDP só funciona com o bloqueio desligado, o que é inseguro.
Clientes que dependem de UDP devem consultar `/capabilities`.

> A implementação do bloqueio de UDP (T23) está em andamento; até ela entrar no
> main, a auditoria T22 (achado N1) segue **aberta**.

## Configuração IPv6 (rota, NDP, ndppd)

A API adiciona cada IPv6 como `/128` na interface (com `nodad`) e **não altera**
rota padrão, firewall, sysctl nem ndppd. O provedor precisa entregar a subnet:

- **Subnet roteada** para o IPv6 da VPS: nada a fazer além de informá-la.
- **Subnet "on-link"** (provedor faz NDP para cada endereço): os `/128`
  adicionados respondem por si; não use ndppd.
- **Provedor só faz NDP para o IPv6 principal**: é preciso um proxy NDP
  (ex.: `ndppd` com `rule <subnet> { static }` na interface externa) ou pedir
  ao provedor a subnet roteada. Configure fora deste projeto.

Diagnóstico rápido em [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#ipv6-de-saída-não-funciona).

## Limites medidos

Fonte: [docs/evidence/limits.md](docs/evidence/limits.md) (3proxy 0.9.4 real,
supervisor local, loopback v4-mapped, host compartilhado com load ~20: tempos
são limite superior, não benchmark; custo de `systemctl` não incluído).

| Medida | Valor |
|---|---|
| Lote 100 HTTP via CLI | 26,6 s |
| Lote 100 SOCKS5 via CLI (`--random`) | 39,5 s |
| Lote 200 via API (bloqueio SSRF ligado / desligado) | 48,4 s / 46,3 s |
| Remover 200 (DELETE sequencial) | 41,4 s |
| `GET /proxies` com 200 | 134 ms |
| RSS por 3proxy (mediana) | ≈ 4,7 MiB; 200 proxies ≈ 930 MiB |
| fds por 3proxy ocioso | 4 |
| p50 API com 200 vivos: create / edit / rotate / delete | 417 / 283 / 549 / 267 ms |

Memória e fds crescem linearmente com o número de proxies. Máximo por lote:
`PROXY_BATCH_MAX` (padrão 1000).

## Testes e evidências

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                                         # sem 3proxy
TEST_3PROXY_BINARY=/usr/local/bin/3proxy .venv/bin/python -m pytest -q # 3proxy real, loopback
.venv/bin/python -m pytest -q tests/acceptance                        # critérios A/B
sudo tests/e2e/vm_e2e.sh --dry-run --phase pre-reboot                 # plano do E2E em VM
```

- [docs/evidence/qa-final.md](docs/evidence/qa-final.md): re-QA final (409
  testes com 3proxy real, 3 execuções; aceite A/B 5x; pip-audit limpo).
- [docs/evidence/limits.md](docs/evidence/limits.md): limites medidos.
- [docs/reviews/T22-security.md](docs/reviews/T22-security.md): reauditoria de
  segurança (N1 UDP ASSOCIATE).

## Limitações e gates

Este projeto **não** está declarado 100% funcional. Pendências conhecidas:

- **Sem execução em Linux limpo com systemd**: instalador, unidades
  `ipv6-proxy@`, upgrade e rollback foram testados com stubs e supervisor
  local. O E2E em VM (`tests/e2e/vm_e2e.sh`) existe mas não rodou (T14).
- **Sem reboot real**: a recolocação do IPv6 por instância no boot não foi
  observada em host real (T14).
- **Sem IPv6 roteado**: toda a evidência usa loopback/`::1`/v4-mapped. NDP,
  rota do provedor e saída externa não foram comprovados (T14).
- **Push/PR não autorizados** (pergunta #152, ADR-0004): o código fica em
  branch local; o CI do GitHub não rodou. O passo de lint do CI usa
  shellcheck 0.9 e acusa `SC2119/SC2120` em `install.sh` (qa-final Q1).
- **UDP ASSOCIATE**: ver [Proteção anti-SSRF e UDP](#proteção-anti-ssrf-e-udp)
  (N1 aberto até T23).
- `PROXY_DATA_DIR` e `PROXY_3PROXY_BINARY` precisam ficar nos padrões; subnet e
  interface não mudam com proxies existentes.
- Sem rotação periódica automática nem por requisição (IPv6 fixo por contrato).
- Legado (`ipv6-proxy-server.sh`) e API não podem coexistir na mesma VPS.

## Decisões

ADRs 0001–0007 (fora do repositório, em `docs/adr/` do projeto): base de código
(0001), supervisão por unidade systemd (0002), TUI Textual (0003), sem push
público (0004), IPv6 por instância no boot (0005), órfãos só param com intenção
registrada (0006), UDP ASSOCIATE bloqueado com anti-SSRF (0007).

## Projeto original (modo legado)

As instruções abaixo são referência histórica do script original, **não são o
CLI da API**. Legado e API não podem coexistir nesta VPS, nem com portas
separadas ou API parada: o legado pode encerrar processos e alterar rede fora
do seu pool. Use `ipv6-proxy`, não `ipv6-proxy-server.sh`, para controlar a API.
Esta cópia do legado e os scripts de startup gerados por ela recusam execução
quando detectam a instalação API; cópias upstream e startups antigos não.
Antes de instalar a API, desative cron/startup legado, pare o processo legado e
retire a instalação antiga. Não execute o desinstalador legado depois.

## IPv6 Proxy Server [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Create your own IPv6 backconnect proxy server with only one script on any Linux distribution. Any number of random IPs on the subnet, ideal for parsing and traffic arbitrage (Google/Facebook/Youtube/Instagram and many others support IPv6).

Ask questions [in issues](https://github.com/Temporalitas/ipv6-proxy-server/issues), if you have some.

### Tutorial

Assuming you already have an entire IPv6 subnet (/48 or /64) routed to your server.

Just run:

```bash
#sudo su
wget https://raw.githubusercontent.com/Temporalitas/ipv6-proxy-server/master/ipv6-proxy-server.sh && chmod +x ipv6-proxy-server.sh
./ipv6-proxy-server.sh -s 64 -c 100 -u username -p password -t http -r 10
```

Uncomment first line or run all commands with `sudo` if you`re not under root.



If script already installed, you can just run one command to reconfigure parameters, for example:

```bash
./ipv6-proxy-server.sh -s 64 -c 20 -u username2 -p password2 -t socks5 -r 2
```

Old instance will be disabled and new starts without reinstallation very quickly.



If you want to uninstall proxy server, just run:

```bash
./ipv6-proxy-server.sh --uninstall
```

Proxy server will stopped, all configuration files, firewalls, shedulers and so on will be reset to initial state.



**Command line arguments:**

- `-s` or `--subnet` - IPv6 [subnet](https://docs.netgate.com/pfsense/en/latest/network/ipv6/subnets.html), fully allocated on your server. Any subnet divisible by 4 (for example, `48` or `56`), default `64`
- `-c` or `--proxy-count` - The total number of proxies you want to have (from 1 to 10000)
- `-t` or `--proxies-type` - Proxies type - `http` or `socks5`. Default `http`, if no value provided
- `-u` or `--username` - All proxies auth login
- `-p` or `--password` - All proxies auth password (if you specify neither username not password, proxy will run without authentication)
- `--rotate-every-request` - bool parameter without value, if used, for every request to each proxy you will get different gateway IP from your IPv6 subnet. Doesn't work on all VPS. If configuration isn't possible, it will exit with an error
- `--random` - bool parameter without value, if used, each backconnect proxy will have random username and password, that will be written in backconnect proxies file (`-f` argument)
- `--start-port` - backconnect IPv4 start port. If you create 1500 proxies and `start-port` is `20000`, and server external IPv4 is, e.g,`180.113.14.28` you can connect to proxies using `180.113.14.28:20000`, `180.113.14.28:20001` and so on until `180.113.14.28:21500`
- `-r` or `--rotating-interval` - rotation interval of entire proxy pool in minutes. At the end of each interval, output (external IPv6) addresses of all proxies are changed and proxy server is restarted, which breaks existing connections for a few seconds. From 0 to 59, default value - `0` (rotating disabled)
- `--allowed-hosts` - list of allowed hosts, to which user can connect via proxy (comma-separated, without spaces, for example - `"google.com,*.google.com,fb.com"`). All other hosts will be denied, if this parameter is provided
- `--denied-hosts` - list of denied hosts (comma-separated, without spaces, for example - `"google.com,*.google.com,fb.com"`). All others hosts will be allowed, if this parameter is provided
- `-l` or `--localhost` - bool parameter without value, if used, all backconnect proxy will be available only on localhost (`127.0.0.1:30000` instead of `180.113.14.28:30000`)
- `-b` or `--backconnect-ip` - server IPv4 backconnect address for proxies, use ONLY if script cannot parse IP correctly and your server has non-standard IP configuration
- `-f` or `--backconnect-proxies-file` - path to file, in which backconnect proxies list will be written when proxies start working (default `~/proxyserver/backconnect_proxies.list`). You can just copy all proxies from this file and use them in your soft as list of IPv6 proxies.
- `-m` or `--ipv6-mask` - first blocks on server subnet, unchanged part, without final colon(s) at the end, use ONLY if script cannot parse ipv6 mask automatically. For example, if the external ipv6 address on server is `2a03:6f01:5::1da6` and you want to use entire /64 subnet, script cannot parse ipv6 gateway because of address zero-field replacement with `::`. Real mask for /64 subnet is first four blocks - `2a03:6f01:5:0`.
  If you want to provide manually allocated subnet that isn't divisible by 16, you should provide FULL last block. For example, if you want all /56 subnet and you allocated `2a01:7e01:f403:a000` on your server, run command with `--ipv6-mask 2a01:7e01:f403:a000`, not `--ipv6-mask 2a01:7e01:f403:a0` - don't remove last numbers of block!
- `-i` or `--interface` - ethernet interface name, to which IPv6 subnet is allocated and where all proxies will be raised. Automatically parsed from system info by default, use ONLY if you have non-standard/additional interfaces on your server.
- `--uninstall` - uninstall proxy server, you don't need to provide any other parameters with it.
- `--info` - get info about running proxy server (proxy count, rotating, auth, etc.)



**Quick errors FAQ:**

- If your proxies simply don't work, do these few steps and write an issue **only** if none of this worked:
  - Check your logfile: `/var/tmp/ipv6-proxy-server-logs.log` for any error-like messages. If message is related to configuration/subnet allocation, ask your VPS provider. If error message is unclear, write an issue.
  - If there is no errors, run `./ipv6-proxy-server.sh --info`.
  - Check that backconnect addresses parsed correctly (the file name on your server in first step).
  - Check that outgoing IPv6 addresses generated correctly (in `proxyserver/3proxy/3proxy.cfg` file).
  - Check connection from proxy server: select one of outgoing IPv6 addresses from previous step and run command `curl --interface <ipv6-address> <website>`. If it works correctly, but proxy doesn't work - write an issue right now.
  - Check if the site you want to access has an AAAA record in DNS. If no, you cannot reach this site via any IPv6 proxies.
  - Verify that your VPS provider allocated a full IPv6 subnet to the server (DigitalOcean and many other providers don't do this).

### License

[MIT](https://opensource.org/licenses/MIT)
