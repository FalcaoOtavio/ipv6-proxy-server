# TUI `ipv6-proxy-tui`

Interface de terminal (Textual, versão fixada em `requirements.txt`) para gerir
os proxies pela mesma API local que o CLI `ipv6-proxy`. Não executa o script
legado nem fala com o 3proxy: toda operação é uma chamada HTTP à API.

## Executar

```bash
sudo ipv6-proxy-tui
```

Lê `PROXY_API_TOKEN` de `/etc/ipv6-proxy-api.env` (regular, root, `0600`, as
mesmas regras do CLI). Variáveis opcionais:

| Variável | Padrão | Uso |
|---|---|---|
| `PROXY_TUI_API_URL` | `http://127.0.0.1:8000` | Só `http://` em loopback (o token trafega sem TLS). |
| `PROXY_API_TOKEN` | — | Token direto, dispensa ler o arquivo (útil em testes). |
| `PROXY_TUI_REFRESH` | `5` | Segundos entre atualizações de status; `0` desliga. |

Requer o Python do venv da instalação (onde o Textual está instalado).

## Atalhos

| Tecla | Ação |
|---|---|
| `↑` `↓` | mover seleção |
| `c` | criar proxy (porta, protocolo, usuário, senha mascarada) |
| `l` | criar lote (quantidade até `PROXY_BATCH_MAX`, porta inicial, protocolo, credenciais comum/aleatória/sem, opção "Parcial") |
| `e` | editar porta, protocolo e credenciais do selecionado |
| `r` | rotacionar o IPv6 do selecionado (confirmação) |
| `d` | excluir (exige digitar a porta) |
| `x` | exportar `IPv4:porta:usuario:senha` (sem auth: `IPv4:porta`) |
| `s` | mostrar/ocultar a senha do selecionado |
| `a` | recarregar (bloqueado durante uma mutação; a lista recarrega ao final dela) |
| `?` | ajuda |
| `Esc` | fechar diálogo sem efeito |
| `q` | sair |

Status em texto e símbolo (não só cor): `● ativo`, `◐ subindo`, `✕ falhou`,
`■ isolado` (dupla falha; reinicie a API). A lista vem de
`GET /proxies?include=status`, então um proxy fora do ar aparece como tal em vez
de esconder a lista inteira.

## Senhas

A API nunca devolve senhas. A TUI guarda **somente em memória** as senhas que
ela mesma definiu na sessão (criar, editar, lote comum ou aleatório). Coluna
Senha: `••••` oculta, `n/d` desconhecida, `—` sem autenticação. Revelar só com `s`.

Exportar grava um arquivo novo com permissão `0600` (nunca sobrescreve).
Proxies com autenticação e senha desconhecida são omitidos com o aviso
`N omitido(s): senha desconhecida (a API não devolve senhas).` Senhas aleatórias
de lote existem só na resposta da criação: exporte antes de sair.

## Segurança das operações

- Uma mutação por vez, em segundo plano: a interface continua respondendo.
- O alvo (id + porta) é fixado quando a ação é aberta e enviado como `If-Match`.
  Mudar a seleção, navegar ou cancelar durante a requisição nunca afeta outro
  proxy; se outro cliente recriou a porta nesse meio-tempo, a API recusa (412)
  e nada muda.
- Cada mutação reinicia só o 3proxy da porta afetada.
- Cada mutação envia um `Idempotency-Key` (uuid4). Se a conexão cair ou der
  timeout, a TUI repete **uma vez com a mesma chave**: a API devolve o resultado
  já gravado em vez de repetir, então um lote nunca duplica. Senhas aleatórias
  perdidas nessa resposta não voltam no replay (a API não guarda senhas): a
  coluna mostra `n/d` e o resumo pede para definir novas com `e`.
- Timeout do lote proporcional à quantidade (60 s + 5 s por proxy).
- Limite do lote: padrão 1000 (`PROXY_BATCH_MAX`); se a API recusar com outro
  limite (422), a TUI passa a usar o valor informado.
- Lote "Parcial" marcado: mantém os criados se alguma porta falhar e lista as
  falhas; desmarcado: tudo ou nada.

## Erros

Sem traceback; a linha de status mostra a causa e o próximo passo. Qualquer
exceção inesperada numa ação vira a mensagem genérica "Erro inesperado na
operação…" (o texto da exceção nunca é exibido), e a tela de erro fatal do
Textual (que imprime variáveis locais, ou seja, senhas) foi substituída por uma
linha fixa.

- API fora do ar: `sudo systemctl status ipv6-proxy-api`.
- Token inválido (401): conferir `PROXY_API_TOKEN` em `/etc/ipv6-proxy-api.env`.
- Arquivo de token ilegível: rodar com `sudo`.
- Falha de rede numa mutação (após a repetição automática): o resultado pode ter
  sido aplicado; recarregue (`a`) antes de repetir.
- Exportação para caminho inválido (ex.: `~usuarioinexistente/x`): mensagem de
  caminho inválido. O arquivo exportado recebe `fsync` antes de confirmar.

Testado em 80x24 e 60x20 (em 60 colunas o IPv6 é a última coluna e aparece truncado).

## Testes

```bash
.venv/bin/python -m pytest -q tests/test_tui.py tests/test_tui_client.py tests/test_tui_api.py tests/test_tui_pty.py
```

`test_tui.py` roda os fluxos F01–F08 com Textual Pilot contra a API real
(uvicorn em processo, backend simulado) e grava capturas SVG em
`tests/tui_snapshots/` (80x24 e 60x20). `test_tui_pty.py` abre o entrypoint em
pseudo-terminal real (pexpect), navega e sai.
