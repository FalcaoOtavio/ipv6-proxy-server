# API de proxies IPv6

Esta extensao usa FastAPI para controlar o mesmo motor 3proxy 0.9.4 do
repositorio original. O script Bash original continua disponivel como modo
legado; nao execute ambos sobre as mesmas portas ou enderecos.

## Contrato

- Cliente conecta ao IPv4 local configurado e a uma porta TCP.
- Cada porta tem exatamente um IPv6 de saida, exclusivo no pool gerenciado.
- IPv6 permanece fixo ate chamada explicita de rotacao.
- Rotacao conserva porta, protocolo e credenciais; troca somente IPv6 escolhido.
- HTTP/CONNECT e SOCKS5 usam `-6`, sem fallback de saida IPv4. Destinos precisam
  aceitar IPv6. Para SOCKS5, use resolucao DNS no proxy (`socks5h://`).
- Alteracoes reiniciam o processo 3proxy inteiro e interrompem conexoes abertas
  de todos os proxies. Enderecos dos demais proxies permanecem iguais.
- Estado persiste em disco; reinicio restaura portas, enderecos e credenciais.
- API serializa mutacoes, impede portas duplicadas e tenta rollback em falhas.
  Falha no rollback retorna 503 e bloqueia mutacoes ate recuperacao/reinicio.
- Nenhuma rotacao automatica ou por requisicao e adicionada.

## Instalacao: Debian/Ubuntu com systemd

Requer Python 3.9+, `iproute2`, compilador e subnet IPv6 inteira atribuida ao
servidor. O provedor deve rotear a subnet ou responder corretamente ao NDP para
os enderecos adicionados. A API adiciona IPv6 `/128` na interface, sem alterar
rota padrao, firewall, sysctl ou configuracao de ndppd. Enderecos aleatorios usam
`nodad`; a subnet precisa ser dedicada para evitar conflito com outros hosts.

Use este checkout customizado em `/opt/ipv6-proxy-server`, incluindo `proxy_api`
e `deploy`; clonar somente o upstream nao inclui esta extensao.

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip build-essential git iproute2 openssl
git clone --depth 1 --branch 0.9.4 https://github.com/3proxy/3proxy.git /tmp/3proxy-build
make -C /tmp/3proxy-build -f Makefile.Linux
sudo install -m 0755 /tmp/3proxy-build/bin/3proxy /usr/local/bin/3proxy
cd /opt/ipv6-proxy-server
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo install -m 0600 deploy/ipv6-proxy-api.env.example /etc/ipv6-proxy-api.env
openssl rand -hex 32
sudoedit /etc/ipv6-proxy-api.env
```

Substitua subnet, IPv4, interface e token pelos valores reais. IPv4 deve existir
na interface local; atras de NAT, clientes usam IPv4 publico com encaminhamento
das portas para esse IPv4 local. O token deve ter pelo menos 32 caracteres ASCII.

```bash
sudo install -m 0644 deploy/ipv6-proxy-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ipv6-proxy-api
sudo journalctl -u ipv6-proxy-api -f
```

O servico precisa de privilegios para adicionar/remover IPv6. Use systemd em
producao: `KillMode=control-group` encerra filhos 3proxy mesmo se API morrer.
Execute somente um worker, sem `--reload`, e mantenha um unico gerenciador por
interface/subnet. O lock impede dois gerenciadores com mesmo diretorio de dados.
Nao altere subnet, interface ou diretorio com proxies existentes: exclua todos
antes, mantendo configuracao original, e so depois altere configuracao.

Se houver instalacao legada, desative previamente suas entradas cron de
`proxy-startup.sh` e encerre seu processo 3proxy especifico. Nao execute o
instalador/desinstalador legado enquanto API estiver ativa: script legado pode
encerrar outros processos 3proxy. Nao existe importacao automatica do pool legado.

API escuta `127.0.0.1:8000`. Acesso remoto pode usar tunel SSH:

```bash
ssh -L 8000:127.0.0.1:8000 usuario@IPV4_DO_SERVIDOR
```

Para exposicao direta, configure HTTPS em proxy reverso e controle de acesso.
API exige Bearer token em todos os endpoints operacionais; Swagger em `/docs`.
Libere portas TCP de proxies no firewall do host e do provedor conforme clientes
autorizados. Sem `username`/`password`, proxy nao exige autenticacao; restrinja
acesso no firewall. Credenciais proxy sao independentes do token da API.

## Endpoints

| Metodo | Rota | Resultado |
|---|---|---|
| POST | `/proxies` | Cria proxy; 201 |
| GET | `/proxies` | Lista pool; 200 |
| POST | `/proxies/{port}/rotate` | Troca IPv6 da porta; 200 |
| DELETE | `/proxies/{port}` | Remove proxy e IPv6; 204 |

```bash
export API_TOKEN='TOKEN_CONFIGURADO_NO_SERVIDOR'
curl -sS http://127.0.0.1:8000/proxies \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"port":30000,"protocol":"http","username":"cliente","password":"senha-segura"}'

curl -sS http://127.0.0.1:8000/proxies -H "Authorization: Bearer $API_TOKEN"
curl -sS -X POST http://127.0.0.1:8000/proxies/30000/rotate \
  -H "Authorization: Bearer $API_TOKEN"
curl -i -X DELETE http://127.0.0.1:8000/proxies/30000 \
  -H "Authorization: Bearer $API_TOKEN"
```

Criacao aceita `{}`: HTTP, primeira porta livre na faixa configurada, sem auth.
Protocolo tambem aceita `socks5`. Username e password devem vir juntos; username
aceita letras ASCII, numeros, `_`, `.`, `-`; password aceita tambem `!@%+=`.
Caracteres de controle e sintaxe especial do 3proxy sao rejeitados. Mesmo
username em portas distintas precisa usar mesma senha, pois 3proxy compartilha
cadastro de usuarios. ACL restringe cada porta ao username correspondente.
Respostas nunca incluem senha.

Exemplo de resposta (IPv4/IPv6 de documentacao):

```json
{
  "port": 30000,
  "protocol": "http",
  "ipv4": "192.0.2.10",
  "ipv6": "2001:db8:1234:0:9ca1:af4b:2f0a:92bb",
  "username": "cliente"
}
```

Erros: 401 token invalido/ausente; 404 porta inexistente; 409 conflito ou pool
esgotado; 422 entrada invalida; 503 falha de aplicacao/recuperacao. Erro 503 exige
inspecao do journal e `/var/lib/ipv6-proxy-api/3proxy.log`.

Estado e configuracao ficam em diretorio `0700`, arquivos de estado/configuracao
`0600`, incluindo senhas necessarias ao 3proxy. Nao versione esses arquivos.
`owned-addresses.json` permite remover enderecos de operacoes interrompidas e
restaurar estado confirmado apos reinicio. Nao apague esse arquivo manualmente.
Parar API encerra listeners, preservando enderecos para reinicio. Para remover
enderecos, exclua proxies pela API antes de desativar servico.

## Validacao no servidor

Crie proxy e confirme IPv6 observado externamente:

```bash
curl --proxy http://IPV4_DO_SERVIDOR:30000 --proxy-user cliente:senha-segura https://api64.ipify.org
# Para SOCKS5:
curl --proxy socks5h://IPV4_DO_SERVIDOR:30000 --proxy-user cliente:senha-segura https://api64.ipify.org
```

Repita antes e depois de `/rotate`: IPv6 deve coincidir com resposta da API;
IPv4 e porta de conexao devem permanecer iguais. Apos DELETE, nova conexao deve
falhar. Destino somente IPv4 deve falhar, sem fallback IPv4. Teste depende do
roteamento/NDP e firewall reais do provedor; teste unitario nao comprova esses
requisitos. API verifica abertura de listeners, nao conectividade externa.

## Testes locais

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Testes padrao usam backend simulado, sem alterar interfaces locais.

Teste opcional com binario real, usando somente loopback IPv4/IPv6:

```bash
TEST_3PROXY_BINARY=/usr/local/bin/3proxy .venv/bin/python -m pytest -q
```

Esse teste confirma encaminhamento HTTP e SOCKS5 de cliente IPv4 para servidor
IPv6 e isolamento de credenciais entre portas. Nao altera interfaces nem valida
roteamento externo do provedor.
