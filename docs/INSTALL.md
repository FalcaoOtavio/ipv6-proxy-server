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
instala a API em `/opt/ipv6-proxy-server`, gera token aleatorio e habilita
`ipv6-proxy-api.service` no boot. Confirma resposta autenticada da API.
O token fica em `/etc/ipv6-proxy-api.env`, acessivel somente ao root.
Nao imprime o token nos logs de instalacao. Estado fica em `/var/lib/ipv6-proxy-api`.

## Acessar e usar

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
3proxy ativo. Desative cron e processo do gerenciador legado antes de instalar.

Se houver falha parcial, consulte o erro e complete a instalacao seguindo
[API.md](API.md#instalacao-debianubuntu-com-systemd). O script nao remove
automaticamente arquivos persistentes, mesmo em caso de falha. Para atualizar
manualmente, faca backup da configuracao e do estado, pare o servico, atualize
o codigo e as dependencias e reinicie. Nao altere subnet/interface com proxies
existentes; remova-os pela API antes de mudar esses parametros.
