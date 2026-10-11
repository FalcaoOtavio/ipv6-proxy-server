# Instalacao automatica

Execute na VPS Ubuntu 22.04/24.04/26.04 ou Debian 12/13, com systemd,
acesso sudo, IPv4 e subnet IPv6 dedicada roteada pelo provedor.
O instalador nao cria nem contrata uma VPS ou uma subnet.

```bash
curl -fsSLo install-ipv6-proxy.sh https://raw.githubusercontent.com/FalcaoOtavio/ipv6-proxy-server/main/install.sh
sudo bash install-ipv6-proxy.sh
```

Informe a subnet fornecida pelo provedor; interface e IPv4 local sao sugeridos.
Nao deduza uma subnet dedicada apenas a partir de um endereco IPv6 da VPS.
Para execucao sem perguntas, substitua os valores de exemplo:

```bash
sudo bash install-ipv6-proxy.sh --subnet 2001:db8:1234::/64 --interface eth0 --ipv4 192.0.2.10
```

O script instala dependencias via apt, compila a revisao fixa do 3proxy 0.9.4,
instala a API em `/opt/ipv6-proxy-server` e o CLI em `/usr/local/bin/ipv6-proxy`,
gera token aleatorio e habilita
`ipv6-proxy-api.service` no boot. Confirma resposta autenticada da API.
O token fica em `/etc/ipv6-proxy-api.env`, acessivel somente ao root.
Nao imprime o token nos logs de instalacao. Estado fica em `/var/lib/ipv6-proxy-api`.

O modo normal funciona com somente `install-ipv6-proxy.sh` baixado: ele clona
este repositorio para obter API, unidade systemd e CLI quando nao ha checkout
completo junto ao instalador. Isso e diferente de `--cli-only`, abaixo.

## Instalar ou atualizar somente o CLI

Para uma API ja instalada, obtenha um checkout completo e atualizado desta
versao (nao apenas `install.sh`, nem o repositorio upstream legado):

```bash
git clone https://github.com/FalcaoOtavio/ipv6-proxy-server.git ipv6-proxy-cli-source
cd ipv6-proxy-cli-source
sudo bash install.sh --cli-only
sudo ipv6-proxy --help
sudo ipv6-proxy list
```

`--cli-only` exige `install.sh` e `ipv6-proxy` juntos e reconhece a API pelos
arquivos/diretorio padrao. Adiciona ou substitui atomicamente apenas o cliente
root:root `0755`; nao instala dependencias, nao altera token, configuracao ou
estado, nao faz daemon-reload e nao reinicia API/3proxy. Nao aceita flags de
subnet/interface/IPv4. Recusa sobrescrever um CLI desconhecido, symlink ou com
permissoes inesperadas. O modo normal continua recusando instalacao existente.
Esse modo nao migra legado nem atualiza o servidor API.

## Acessar e usar

Na VPS, sem precisar copiar token:

```bash
sudo ipv6-proxy list
sudo ipv6-proxy create --port 30000 --protocol http --username cliente
sudo ipv6-proxy rotate 30000
sudo ipv6-proxy delete 30000
sudo ipv6-proxy list --output json
```

A senha e solicitada sem eco. Para automacao use `--password-stdin --yes` e
forneca uma linha pelo stdin; veja [contrato do CLI](API.md#cliente-cli).
**Toda mutacao (create/rotate/delete) pela API ou CLI reinicia todos os proxies
da API e desconecta todas as sessoes ativas**, inclusive de outras portas.
O CLI pede confirmacao, ou aceita `--yes`. CLI e API compartilham o mesmo
estado/serializacao, portanto podem ser usados juntos; nao inicie outro manager.

Para usar Swagger no navegador:

No seu computador, abra um tunel e mantenha o comando em execucao:

```bash
ssh -L 8000:127.0.0.1:8000 usuario@IP_PUBLICO_DA_VPS
```

Abra <http://127.0.0.1:8000/docs>. Consulte o token na VPS com
`sudo cat /etc/ipv6-proxy-api.env` e use **Authorize** no Swagger.
Crie um proxy via `POST /proxies`, informando usuario e senha:

```json
{"port":30000,"protocol":"http","username":"cliente","password":"troque-esta-senha"}
```

Libere a porta TCP 30000 no firewall da VPS/provedor para o IP do cliente.
Valide a saida IPv6 usando os exemplos em [API.md](API.md#validacao-no-servidor).
O teste de inicializacao nao comprova roteamento/NDP externo. A API nao altera
firewall, rota padrao, sysctl ou ndppd. Nao exponha a porta 8000 sem HTTPS e
controle de acesso. Omitir usuario/senha cria proxy sem autenticacao.

## Reinicio, diagnostico e instalacao existente

```bash
sudo systemctl status ipv6-proxy-api
sudo journalctl -u ipv6-proxy-api -n 50
sudo systemctl restart ipv6-proxy-api
```

O instalador recusa sobrescrever diretorios, binario e configuracoes existentes.
Reexecutar nao troca token nem apaga proxies. Ele tambem recusa um processo
3proxy ativo, diretorio legado `/root/proxyserver` ou cron `proxy-startup.sh`.
Legado e API nao podem coexistir, mesmo em portas diferentes ou API parada.
Antes de instalar, desative cron/startups antigos, pare o processo legado
especifico e retire sua instalacao. Nao execute o desinstalador legado depois.
A copia local de `ipv6-proxy-server.sh` e os novos startups que ela gera bloqueiam
execucao ao detectar marcadores da API, antes de efeitos colaterais. Isso nao
protege copias upstream ou startups antigos ja gerados: desative-os manualmente.
Nao existe importacao automatica do pool legado.

Se houver falha parcial, consulte o erro e complete a instalacao seguindo
[API.md](API.md#instalacao-debianubuntu-com-systemd). O script nao remove
automaticamente arquivos persistentes, mesmo em caso de falha. Para atualizar
manualmente, faca backup da configuracao e do estado, pare o servico, atualize
o codigo e as dependencias e reinicie. Nao altere subnet/interface com proxies
existentes; remova-os pela API antes de mudar esses parametros.
