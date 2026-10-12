# CLI `ipv6-proxy`

Cliente da API local (`http://127.0.0.1:8000`), script unico em Python 3 padrao,
instalado em `/usr/local/bin/ipv6-proxy`. Usa o mesmo token
(`/etc/ipv6-proxy-api.env`, root `0600`) e o mesmo gerenciador/estado da API e
da TUI: nao executa o script legado nem mantem pool paralelo. Comandos
operacionais exigem `sudo`; `--help` funciona sem root.

## Comandos

| Comando | Endpoint | Efeito em sessoes ativas |
|---|---|---|
| `list [--output table\|json]` | `GET /proxies?include=status` | nenhum |
| `create [--port N] [--protocol http\|socks5] [--username U [--password-stdin]]` | `POST /proxies` | nenhum (porta nova) |
| `batch ...` | `POST /proxies/batch` | nenhum (so portas novas) |
| `edit <porta> ...` | `GET` + `PATCH /proxies/{porta}` com `If-Match` | reinicia so o 3proxy da porta |
| `rotate <porta>` | `GET` + `POST /proxies/{porta}/rotate` com `If-Match` | reinicia so o 3proxy da porta |
| `delete <porta>` | `GET` + `DELETE /proxies/{porta}` com `If-Match` | encerra so a porta |
| `export [--format F] [-f ARQ] [--force]` | `GET /proxies?include=status` | nenhum |

Cada proxy roda em processo 3proxy proprio (ver `docs/API.md`); `edit`,
`rotate` e `delete` avisam em stderr que somente as sessoes daquela porta caem.
Nenhuma mutacao reinicia os demais proxies.

`list` e `export` usam `?include=status`: um proxy caido nao esconde a lista
(a rota legada `GET /proxies` responde 503 nesse caso). `list` mostra a coluna
`STATUS` (`running`, `starting`, `failed`, `fenced`) e avisa em stderr quais
portas estao fora do ar.

### Idempotencia e alvo fixo (todas as mutacoes)

- Cada invocacao de `create`, `batch`, `edit`, `rotate` e `delete` envia um
  `Idempotency-Key` (uuid4 novo). `--idempotency-key K` reusa uma chave.
- Em falha de transporte/timeout (codigo 6) o resultado e desconhecido; a
  mensagem traz a chave e, se houver, o id. Repita **o mesmo comando** com
  `--idempotency-key K` (e `--if-match ID`): a API devolve o resultado gravado
  em vez de repetir, entao o lote nao duplica (validade da chave: 24 h na API).
- **Senhas aleatorias nao sao recuperaveis no replay**: a API nunca guarda
  senhas e so as devolve na primeira resposta. Se ela se perdeu, o replay lista
  os proxies sem senha e avisa; defina novas com
  `ipv6-proxy edit <porta> -u <usuario>`.
- `edit`, `rotate` e `delete` leem o `id` (`GET /proxies/{porta}`), mostram
  `Alvo: porta P, id ID` em stderr e enviam `If-Match: ID`; se a porta foi
  excluida e recriada nesse intervalo, a API responde 412 e o herdeiro nao e
  tocado. `--if-match ID` pula a leitura.
- O timeout do `batch` cresce com a quantidade (60 s + 5 s por proxy).

### `batch`

| Opcao | Descricao |
|---|---|
| `-c`, `--proxy-count N` | Quantidade, de 1 ate `PROXY_BATCH_MAX` lido de `/etc/ipv6-proxy-api.env` (padrao 1000) |
| `--start-port P` | Primeira porta candidata (padrao: inicio da faixa). Portas ocupadas sao puladas; as portas nao precisam ser contiguas |
| `-t`, `--protocol http\|socks5` | Padrao `http` |
| `-u`, `--username U` | Usuario comum a todos (senha sem eco ou `--password-stdin`); com `--random` e o prefixo (usuario `<prefixo><porta>`, padrao `u<porta>`). Maximo 58 caracteres |
| `--password-stdin` | Senha comum em uma linha do stdin (exige `--yes`) |
| `--random` | Senha aleatoria por proxy, gerada pela API |
| `--no-auth` | Sem autenticacao (restrinja no firewall) |
| `--partial` | Mantem os criados se algum falhar (HTTP 207); sem ela e tudo-ou-nada |
| `--format ipv4:port:user:pass\|text\|json` | Formato da lista (padrao `IPv4:porta:usuario:senha`; sem auth = `IPv4:porta`) |
| `-f`, `--output-file ARQ` | Grava a lista em arquivo `0600` em vez do stdout |
| `--force` | Permite substituir `ARQ` (troca atomica; um symlink e substituido, nunca seguido) |
| `--yes` | Modo nao interativo: nao pergunta nada |
| `--idempotency-key K` | Reexecuta com seguranca apos timeout (ver Idempotencia) |

Sem `--yes` e em terminal, o CLI pergunta em PT-BR somente o que faltou
(quantidade, porta inicial, protocolo, credenciais, arquivo), mostra o resumo e
pede confirmacao. Sem terminal e sem `--yes`, falha com codigo 2.

Validacao local antes de chamar a API: quantidade, portas, combinacao de
credenciais, formato de usuario/senha, arquivo de saida (criado antes da
chamada, com `O_EXCL|O_NOFOLLOW`; se ja existir sem `--force`, nada e criado).
Se a API falhar, o arquivo e removido.

Stdout recebe so a lista (quando sem `-f`); stderr recebe o resumo
`Criados: N; falhas: M.` e as portas com falha.

### `edit <porta>`

`--new-port P`, `-t/--protocol`, `-u/--username U` (nova senha sem eco ou
`--password-stdin`) ou `--no-auth` (remove autenticacao). Le o `id` com
`GET /proxies/{porta}` e envia `PATCH` com `If-Match: <id>`: se a porta foi
excluida e recriada nesse intervalo, a API responde 412 e nada muda. IPv6, id e
campos nao informados sao preservados.

### `export`

Exporta os proxies existentes no mesmo formato do `batch`. **A API nunca
devolve senhas de proxies existentes**, entao o export traz `IPv4:porta:usuario`
(sem senha) e avisa em stderr. Senhas so ficam disponiveis no momento da
criacao: grave a saida do `batch` com `-f` (arquivo `0600`). Nao ha endpoint de
revelacao de senha.

## Exemplos

```bash
# 100 HTTP, senha aleatoria por proxy, lista IPv4:porta:usuario:senha em arquivo 0600
sudo ipv6-proxy batch -c 100 --start-port 30000 -t http --random -u cli \
  -f /root/http.list --yes

# 100 SOCKS5, usuario/senha comuns (senha de arquivo protegido, nunca em argv)
sudo ipv6-proxy batch -c 100 -t socks5 -u equipe --password-stdin --yes \
  -f /root/socks5.list < /root/senha-comum.txt

# Interativo
sudo ipv6-proxy batch

# Editar e exportar
sudo ipv6-proxy edit 30000 --new-port 30500 -t socks5 --yes
sudo ipv6-proxy export --format json
```

## Codigos de saida

| Codigo | Significado |
|---|---|
| 0 | Sucesso |
| 2 | Argumento/credencial/arquivo invalido ou confirmacao cancelada/ausente |
| 3 | Privilegio/configuracao/token invalido (inclui HTTP 401) |
| 4 | Erro HTTP 4xx, exceto 401 (404, 409 portas insuficientes, 412 proxy mudou, 422) |
| 5 | Erro do servidor (inclui 503 do lote tudo-ou-nada), redirect ou resposta invalida |
| 6 | Falha de transporte; o resultado pode ser desconhecido (consulte `list` ou repita com a `--idempotency-key` da mensagem) |
| 7 | Lote parcial (`--partial`): parte criada, portas com falha em stderr |
| 130 | Interrompido |

## Seguranca

- Nenhuma opcao aceita senha em argumento; abreviacoes de opcoes estao
  desativadas e argumentos nao reconhecidos nao sao ecoados.
- Senhas nunca vao para stderr; corpos de erro da API nao sao exibidos.
- Arquivos de lista: `0600`, sem seguir symlink, sem sobrescrever sem `--force`.

## Mapa de equivalencia com Temporalitas/ipv6-proxy-server (master)

| Original | Aqui | Situacao |
|---|---|---|
| `-c`, `--proxy-count` | `batch -c/--proxy-count` | equivalente |
| `--start-port` | `batch --start-port` | equivalente (faixa definida por `PROXY_PORT_START/END`, nao "> 5000"; portas ocupadas sao puladas) |
| `-t`, `--proxies-type` | `batch -t/--protocol` | equivalente (`http`/`socks5`) |
| `-u`, `--username` | `batch -u/--username` | equivalente |
| `-p`, `--password` | `--password-stdin` ou prompt sem eco | equivalente com outra forma: senha em argv fica visivel em `ps`/historico |
| `--random` | `batch --random` | parcial: senha aleatoria por proxy; usuario e `<prefixo><porta>`, nao aleatorio |
| `-f`, `--backconnect-proxies-file` | `batch -f/--output-file`, `export -f` | equivalente (arquivo `0600`, sem sobrescrever sem `--force`) |
| formato `ip:porta:usuario:senha` | `--format ipv4:port:user:pass` (padrao) | equivalente |
| `-s`, `--subnet` | `PROXY_IPV6_SUBNET` no instalador/API | nao aplicavel a CLI (configuracao do servidor) |
| `-i`, `--interface` | `PROXY_INTERFACE` | nao aplicavel a CLI (configuracao do servidor) |
| `-b`, `--backconnect-ip` | `PROXY_BIND_IPV4` | nao aplicavel a CLI (configuracao do servidor) |
| `-l`, `--localhost` | `PROXY_BIND_IPV4=127.0.0.1` | nao aplicavel a CLI (configuracao do servidor) |
| `-m`, `--ipv6-mask` | — | nao implementado (enderecos sorteados dentro da subnet inteira) |
| `-r`, `--rotating-interval` | `rotate <porta>` manual | nao implementado: rotacao periodica automatica (IPv6 fica fixo ate rotacao explicita, por contrato) |
| `--rotate-every-request` | — | nao implementado (contrato: 1 IPv6 fixo por porta) |
| `--allowed-hosts`, `--denied-hosts` | — | nao implementado |
| `--info` | `list`, `export`, `GET /proxies/{porta}` (status) | parcial |
| `--uninstall` | instalador (`install.sh`) | nao aplicavel a CLI |
| — | `edit`, `rotate`, `delete`, `--partial`, `--format json` | sem equivalente no original |
