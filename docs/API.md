# API de proxies IPv6

Esta extensao usa FastAPI para controlar o mesmo motor 3proxy 0.9.4 do
repositorio original. O script Bash original continua disponivel como modo
legado; ele nao pode coexistir com a API nesta VPS, mesmo em portas diferentes
ou com o servico API parado. O CLI `ipv6-proxy` e um cliente da API, nao o legado.

## Contrato

- Cliente conecta ao IPv4 local configurado e a uma porta TCP.
- Cada porta tem exatamente um IPv6 de saida, exclusivo no pool gerenciado.
- IPv6 permanece fixo ate chamada explicita de rotacao.
- Rotacao conserva porta, protocolo e credenciais; troca somente IPv6 escolhido.
- HTTP/CONNECT e SOCKS5 usam `-6`, sem fallback de saida IPv4. Destinos precisam
  aceitar IPv6. Para SOCKS5, use resolucao DNS no proxy (`socks5h://`).
- Cada porta roda em processo 3proxy proprio, supervisionado como unidade
  `ipv6-proxy@<porta>.service`, fora do cgroup da API. Criar, editar,
  rotacionar ou excluir uma porta reinicia somente o processo dela; conexoes das
  demais portas continuam (testado com 3proxy real em
  `tests/test_isolation_3proxy.py` e `tests/test_supervisor_3proxy.py`). Falha
  ou morte do 3proxy de uma porta nao derruba as outras. Reiniciar ou atualizar
  a API nao derruba proxies: na partida ela adota as instancias saudaveis sem
  reinicia-las.
- `GET /capabilities` devolve `{"spec":"ashford.proxy/1","capabilities":[...]}`
  com `isolated_mutations`, `owner_scoping`, `idempotency_keys`,
  `conditional_mutations`, `socks5_udp_associate`, `edit`, `batch` e
  `per_proxy_status`, cada um coberto por teste.
- Identidade: cada proxy tem `id` (32 hex) imutavel, gerado no create, mantido
  no rotate/PATCH e nunca reaproveitado quando a porta e reutilizada. `If-Match: <id>`
  em rotate/PATCH/DELETE e precondicao: id diferente responde 412 sem efeito
  (compare-and-delete). Proxy migrado do 0.1.0 recebe id no primeiro start.
- Posse: `PROXY_CLIENT_TOKENS_FILE` aponta para JSON `{"owner":"sha256-hex do
  token"}`. Token de cliente so lista/muda proxies do proprio owner (de outro
  owner responde 404, como inexistente) e nao pode agir por outro owner (403).
  O token admin ve tudo; `X-Proxy-Owner` apenas o restringe. Proxies sem owner
  (legados) so sao visiveis ao admin.
- `Idempotency-Key` (`[A-Za-z0-9._:-]{1,128}`) em POST/batch/rotate/PATCH/DELETE: repeticao
  devolve o resultado gravado sem repetir a mutacao; mesma chave com outro
  pedido gera 422. Chaves valem 24h por owner e sao gravadas no mesmo
  `state.json` (rename atomico) que o pool. Excecao: DELETE com chave e sem
  `If-Match` fixa a chave para sempre (um retry tardio nao pode apagar o
  herdeiro da porta); acima de 100k chaves fixas responde 428 (use If-Match).
- SOCKS5 aceita UDP ASSOCIATE; o relay UDP sai pelo mesmo IPv6 da porta.
- Estado persiste em disco; reinicio restaura portas, enderecos e credenciais.
- API serializa mutacoes, impede portas/IPv6/ids duplicados e compensa falhas
  so nos alvos da mutacao. Dupla falha (operacao e compensacao falham) retorna
  503 e isola so a porta afetada: ela sai do ar, fica com `status: fenced` e
  recusa mutacao (503) ate reinicio; o estado em disco fica no ultimo commit e
  as demais portas seguem servindo e mutando. `GET /proxies` responde 503 so para quem
  enxerga uma porta morta (o owner dela e o admin). Falha ao regravar o estado
  em disco bloqueia todas as mutacoes, sem derrubar processos.
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
sudo bash install.sh --cli-only  # checkout completo, sem reiniciar API
sudo journalctl -u ipv6-proxy-api -f
```

O servico precisa de privilegios para adicionar/remover IPv6. Use systemd em
producao: cada proxy e a instancia `ipv6-proxy@<porta>.service` (instale tambem
`deploy/ipv6-proxy@.service`). Reiniciar a API nao para proxies. No boot, cada
instancia habilitada recoloca so o proprio IPv6 (`ExecStartPre`, a partir do
journal de enderecos da API) antes de subir o 3proxy; endereco que nao esta no
journal e recusado e a instancia falha em vez de sair por IP alheio.
Execute somente um worker, sem `--reload`, e mantenha um unico gerenciador por
interface/subnet. O lock impede dois gerenciadores com mesmo diretorio de dados.
Nao altere subnet, interface ou diretorio com proxies existentes: exclua todos
antes, mantendo configuracao original, e so depois altere configuracao.

Se houver instalacao legada, desative previamente suas entradas cron de
`proxy-startup.sh` e encerre seu processo 3proxy especifico. Nao execute o
instalador/desinstalador legado depois de instalar a API, mesmo parada: ele pode
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

## Cliente CLI

Instalado pelo autoinstalador em `/usr/local/bin/ipv6-proxy`. Requer Python 3
padrao (sem dependencias FastAPI no cliente) e sudo nos comandos operacionais;
`ipv6-proxy --help` funciona sem root. Para API existente, veja
[`--cli-only`](INSTALL.md#instalar-ou-atualizar-somente-o-cli).

```bash
sudo ipv6-proxy list
sudo ipv6-proxy create --port 30000 --protocol socks5 --username cliente
sudo ipv6-proxy rotate 30000
sudo ipv6-proxy delete 30000
sudo ipv6-proxy list --output json
```

`create` sem `--port` usa a primeira livre na faixa configurada (padrao
30000–39999); protocolo padrao `http`. Sem `--username` cria proxy sem auth.
Com username, solicita senha sem eco em terminal; nao existe argumento de senha.
Em automacao, forneca exatamente uma linha terminada em newline pelo stdin,
com `--password-stdin --yes`. Exemplo com arquivo secreto protegido, nao coloque
senha em historico nem em argumentos de processos:

```bash
sudo -v
sudo ipv6-proxy create --username cliente --password-stdin --yes --output json < /caminho/seguro/senha.txt
sudo ipv6-proxy rotate 30000 --yes --output json
sudo ipv6-proxy delete 30000 --yes --output json
```

**Cada mutacao reinicia somente o 3proxy da porta afetada e desconecta as
sessoes dessa porta**; o aviso aparece em stderr e o CLI pede confirmacao. `--yes` dispensa
somente a pergunta, nao o aviso. CLI e HTTP/Swagger podem operar simultaneamente
no mesmo pool: um unico manager na API serializa operacoes e rejeita conflitos.
Isso garante consistencia de estado, nao continuidade de conexoes.

O CLI conecta somente a `http://127.0.0.1:8000`, sem proxy de ambiente, redirects
ou retries. Le token de `/etc/ipv6-proxy-api.env` (arquivo regular root, sem
acesso de grupo/outros; use `0600`), sem `source`/eval. Nao controla 3proxy ou
rede diretamente. Para remoto, execute o CLI na VPS via SSH, por exemplo
`ssh -t usuario@IP_PUBLICO 'sudo ipv6-proxy create --username cliente'`.

`--output json` funciona nos quatro comandos: list retorna array; create/rotate
retornam objeto como API; delete retorna `{"deleted":true,"port":30000}`.
Stdout contem somente o resultado; avisos/erros vao para stderr. Senhas e token
nao sao retornados. Corpos de erro HTTP nao sao exibidos pois podem refletir
segredos enviados ao servidor.

| Codigo de saida | Significado |
|---|---|
| 0 | Sucesso |
| 2 | Argumento/credencial invalida ou confirmacao cancelada/ausente |
| 3 | Privilegio/configuracao/token invalido (inclui HTTP 401) |
| 4 | Erro HTTP 4xx, exceto 401 (ex.: 404, 409, 422) |
| 5 | Erro do servidor, redirect ou resposta invalida |
| 6 | Falha de transporte; resultado da mutacao pode ser desconhecido |
| 130 | Interrompido pelo usuario |

Apos timeout ou interrupcao, consulte `list` antes de repetir uma mutacao: o
servidor pode ter aplicado a alteracao apesar de o cliente nao receber resposta.
O token e estado da API continuam administrados somente pelo servico/instalacao.

## Endpoints

| Metodo | Rota | Resultado |
|---|---|---|
| POST | `/proxies` | Cria proxy; 201 |
| GET | `/proxies` | Lista pool; 200 |
| GET | `/proxies/{port}` | Um proxy com `status`; 200 |
| PATCH | `/proxies/{port}` | Edita credenciais/protocolo/porta; 200 |
| POST | `/proxies/batch` | Cria lote; 201 (207 com `partial`) |
| POST | `/proxies/{port}/rotate` | Troca IPv6 da porta; 200 |
| DELETE | `/proxies/{port}` | Remove proxy e IPv6; 204 |
| GET | `/capabilities` | Contrato `ashford.proxy/1`; 200 |

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
Caracteres de controle e sintaxe especial do 3proxy sao rejeitados. Cada
porta tem cadastro de usuario proprio; ACL restringe a porta ao seu username.
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

### GET /proxies/{port}

Mesma view do list, mais `status`: `running` (instancia ativa e servindo esta
configuracao), `starting` (subindo ou aguardando `Restart=on-failure`),
`failed` (instancia parada/morta) ou `fenced` (isolada apos dupla falha; exige
reinicio da API). Responde mesmo com o proxy fora do ar. `GET /proxies`
continua sem `status` e responde 503 se algum proxy visivel nao esta saudavel.

### PATCH /proxies/{port}

Corpo com qualquer combinacao de `protocol`, `port` e o par
`username`+`password` (os dois juntos; `{"username":null,"password":null}`
remove autenticacao). `id`, `owner` e `ipv6` nao mudam e nao sao aceitos.
Aceita `If-Match` e `Idempotency-Key`. Porta ocupada: 409 sem efeito; fora da
faixa: 422. Mudanca de porta: a instancia na porta nova sobe antes de a antiga
parar; as sessoes deste proxy caem na troca, as dos demais seguem intactas.
Usernames iguais com senhas diferentes em portas distintas sao permitidos
(cada porta tem processo e cadastro proprios).

```bash
curl -sS -X PATCH http://127.0.0.1:8000/proxies/30000 \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -H 'If-Match: 3f2a...id' -d '{"port":30010,"protocol":"socks5"}'
```

### POST /proxies/batch

```json
{"count": 10, "start_port": 30100, "protocol": "socks5",
 "credentials": "random", "username": "cli", "partial": false}
```

- `count` de 1 ate `PROXY_BATCH_MAX` (padrao 1000); acima: 422.
- Portas: as proximas `count` livres em ordem crescente a partir de
  `start_port` (padrao: inicio da faixa), pulando ocupadas; nao precisam ser
  contiguas. Faltando portas livres na faixa: 409 sem efeito.
- `credentials`: `none` (sem auth), `common` (exige `username`+`password`,
  iguais em todos), `random` (senha aleatoria por proxy; usuario
  `<username ou u><porta>`). Senhas aleatorias aparecem SO na primeira resposta;
  repeticao com a mesma `Idempotency-Key` devolve a lista sem senhas.
- Tudo e validado antes de subir qualquer processo (portas e IPv6 unicos).
- Padrao tudo-ou-nada: qualquer falha desfaz so os novos e responde 503.
  `partial: true` mantem os criados e responde 207 com `failed`.

```json
{"created": [{"id": "...", "port": 30100, "protocol": "socks5", "ipv4": "192.0.2.10",
              "ipv6": "2001:db8:1234::1", "username": "cli30100", "owner": null,
              "password": "so-nesta-resposta"}],
 "failed": [{"port": 30101, "error": "proxy did not start; inspect its log"}]}
```

Erros: 401 token invalido/ausente; 403 owner alheio; 404 porta inexistente (ou
de outro owner); 409 conflito ou pool esgotado; 412 `If-Match` divergente; 422
entrada invalida; 428 limite de DELETE idempotente sem `If-Match`; 503 falha de
aplicacao/recuperacao. Erro 503 exige inspecao do journal da API e da instancia
(`journalctl -u ipv6-proxy@<porta>`).

Estado e configuracao ficam em diretorio `0700`, arquivos de estado/configuracao
`0600`, incluindo senhas necessarias ao 3proxy. Nao versione esses arquivos.
`owned-addresses.json` permite remover enderecos de operacoes interrompidas e
restaurar estado confirmado apos reinicio. Nao apague esse arquivo manualmente.
Parar a API nao encerra listeners (cada proxy e sua propria unidade). Para
remover proxies e enderecos, exclua-os pela API.

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

Testes padrao usam backend simulado, sem alterar interfaces locais. Incluem
CLI e HTTP concorrentes contra a mesma API em loopback, conflito de porta,
validacao de configuracao/respostas e protecao contra proxy/redirect.

Teste opcional com binario real, usando somente loopback IPv4/IPv6:

```bash
TEST_3PROXY_BINARY=/usr/local/bin/3proxy .venv/bin/python -m pytest -q
```

Esse teste confirma encaminhamento HTTP e SOCKS5 de cliente IPv4 para servidor
IPv6 e isolamento de credenciais entre portas. Nao altera interfaces nem valida
roteamento externo do provedor.
