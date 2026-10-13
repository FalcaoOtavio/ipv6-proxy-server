# Crítica visual da TUI (T40, reavaliada em T41) — pty 80x24, 120x40, 60x20

Produto sem web: a crítica é sobre `ipv6-proxy-tui` rodando num **pty real** (pexpect + pyte), com API
uvicorn real e 3proxy real (só `ip addr` simulado), capturada pelo harness `tests/tui_validation.py`.
Referência: `docs/DESIGN.md`.

```bash
TUI_EVIDENCE_DIR=docs/evidence/visual TUI_VIEWPORTS=80x24,120x40,60x20 \
TEST_3PROXY_BINARY=/data/projects/ipv6-proxy-server-independente/.tools/3proxy \
  .venv/bin/python tests/tui_validation.py
# 2026-10-13 (T41, após V-01/V-02/V-06): 111 ok, 0 falhas  (results.json)
```

29 telas × 3 tamanhos = 87 capturas (`<tela>-<COLSxLINHAS>.png` + `.txt` com o texto exato do pty).

## Telas cobertas

| Tela pedida | Capturas |
|---|---|
| Lista vazia | `F01-vazio` |
| Lista com proxies | `F01-lista`, `F01-ipv6-real`, `F08-lista-cheia`, `F01-status-atualizado` (✕ falhou) |
| Ajuda / rodapé | `F02-ajuda`, `F02-rodape` |
| Criar | `F03-senha-mascarada`, `F03-erro-porta`, `F03-erro-credenciais`, `F03-criado` |
| Lote | `F04-formulario`, `F04-validacao`, `F04-aleatorio`, `F04-resultado` |
| Editar | `F05-formulario`, `F05-conflito`, `F05-editado` |
| Confirmar exclusão / rotação | `F06-exclusao-errada`, `F06-excluido`, `F06-confirma-rotacao`, `F06-rotacionado`, `F06-corrida-em-andamento` |
| Exportar / senha | `F07-dialogo`, `F07-exportado`, `F07-mascarado`, `F07-revelado` |
| Erro | `F08-api-indisponivel`, `F08-token-invalido`, `F03-erro-*`, `F05-conflito` |

## Notas por item do checklist (DESIGN.md §12, 0–10)

| # | Item | 80x24 | 120x40 | 60x20 | Observação |
|---|---|---|---|---|---|
| A1 | Estado cor + símbolo + palavra | 10 | 10 | 10 | `● ativo`, `✕ falhou`, `✓/✕` no status; legível sem cor. |
| A2 | Contraste AA | 10 | 10 | 10 | Texto 13,9:1, status-erro 4,9:1, erro inline 5,6:1 (V-01 resolvido), status da linha ≥ 6,3:1. |
| A3 | Cabe em 60x20 | — | — | 9 | Tudo renderiza; IPv6 exige rolar (V-03); ajuda rola. |
| A4 | Régua de atalhos verdadeira | 10 | 10 | 10 | 2 linhas fixas, essenciais na última; nenhuma truncada. |
| A5 | Destrutivo confirmado | 10 | 10 | 10 | Exclusão exige digitar a porta; erro "nada foi alterado". |
| A6 | Erro com causa + próximo passo | 10 | 10 | 10 | `systemctl status …`, `PROXY_API_TOKEN em …`, `journalctl -u …`. |
| A7 | Vazio/carregando/erro/ocupado | 9 | 9 | 9 | Todos desenhados; carregando é só texto (aceitável). |
| A8 | Formulário legível | 10 | 10 | 10 | Campos `compact` com fundo `$panel`: vazios e sem foco ficam visíveis (V-02 resolvido, `F03-erro-porta-*`). |
| A9 | Copy PT-BR | 9 | 9 | 9 | Consistente; pequenos deslizes (V-05). |
| A10 | Nada decorativo | 10 | 10 | 10 | Sem header/banner/logo. |
| A11 | Segredos | 10 | 10 | 10 | `••••` padrão, `n/d`, revelar é explícito (`s`). |
| A12 | Teclado / Esc / q | 10 | 10 | 10 | `q` sai com 0 nos três tamanhos (results.json). |

Média ≈ 9,7 (T40: 9,2). 120x40 é idêntica a 80x24 com mais espaço vazio: a TUI não estica colunas nem o diálogo (64 col) —
coerente com o conceito "denso", não é defeito.

## Problemas

| ID | Sev. | Tela → elemento | Problema | Como deve ficar / estado |
|---|---|---|---|---|
| V-01 | média | Todos os diálogos → linha `✕` de erro (`Dialog .error`, `color: $error`) | `#B93C5B` sobre `$surface #1E1E1E` = **3,06:1**, abaixo de AA 4,5:1 (ver `F03-erro-porta-80x24.png`). | Usar `$text-error` (`#D17E92`, 5,6:1 sobre `#1E1E1E`) mantendo negrito, como já faz `#status.error`. **Resolvido em T41**: `Dialog .error { color: $text-error }`, medido 5,64:1. |
| V-02 | média | Criar/Lote/Editar → campos `Input` `compact` | Campo vazio sem foco é uma linha em branco igual ao fundo: "Usuário" e "Senha" parecem rótulos sem campo (`F03-erro-porta-80x24.png`). | Fundo sutil no input (`background: $boost` ou `$panel`) ou marcador `▁`; manter compacto. **Resolvido em T41**: `Input.-textual-compact { background: $panel }` (`#242F38` sobre `#1E1E1E`; texto 10,3:1); altura 1, 60x20 intacto. |
| V-03 | baixa | Lista 60x20 → coluna IPv6 | IPv6 global (39 col) não cabe; só aparece rolando `→`, e a única pista é a barra de rolagem de 1 célula (`F01-ipv6-real-60x20.png`). | Mencionar `← →` na ajuda (seção Atalhos) — custo zero de layout. |
| V-04 | baixa | Lista → status de exportação parcial | Exportação bem-sucedida com omissões usa estilo de erro `✕` vermelho (`F08-lista-cheia-*`). | Aceitável (chama atenção para senhas perdidas); se houver estilo de aviso, usar `!` + `$text-warning`. |
| V-05 | baixa | Status "ocupado" | "Aguarde: Rotacionando porta N em andamento" — rótulo da ação entra capitalizado no meio da frase (`F06-corrida-em-andamento-*`). | "Aguarde: rotação da porta N em andamento." |
| V-06 | baixa | Coluna Status | Cores do status usam ANSI nomeadas (`red`, `magenta`): o contraste depende da paleta do terminal do usuário; em paletas escuras, vermelho/magenta ANSI podem ficar < 3:1. Símbolo + palavra garantem a leitura (A1). | Opcional: usar `$text-error`/`$text-warning`/`$text-success` do tema para ter contraste previsível. **Resolvido em T41**: ativo/subindo/falhou/isolado em `$text-success`/`$text-warning`/`$text-error`/`$text-error` (6,3–12:1 sobre `#121212`). |

## Veredito

**Aprovado.** T41 resolveu os dois itens médios (V-01, V-02) e V-06; testes em `tests/test_tui.py::test_visual_contrast_inline_error_inputs_and_status`
(falham com o CSS antigo). Restam só baixas, sem tarefa obrigatória: V-03 (dica `← →` na ajuda), V-04 (export parcial em
estilo de erro), V-05 (copy de "Aguarde").
