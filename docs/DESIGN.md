# DESIGN.md — ipv6-proxy-tui

> O produto não tem web. A única "UI" é a TUI em terminal (`ipv6-proxy-tui`, Textual 8.x,
> `proxy_tui/app.py` + `proxy_tui/screens.py`). Este documento descreve a direção de arte **como ela é**
> (T40); mudanças de código viram tarefa. Crítica visual: `docs/evidence/visual/README.md`.

## 1. Conceito

**"Painel de operador de rede: denso, literal e calmo."** Quem usa é o admin do VPS, via SSH, muitas vezes num
terminal pequeno. A tela é uma tabela de inventário com uma linha de status e uma régua de atalhos — nada além.

Adjetivos: **operacional**, **honesta**, **econômica**.

Referências reais do segmento e o que se tira de cada uma:

| Referência | O que tiramos |
|---|---|
| `htop` / `btop` | Régua de atalhos fixa no rodapé com tecla em negrito + rótulo; tabela ocupa tudo. |
| `k9s` | Status por linha com símbolo + palavra; ação destrutiva pede confirmação explícita. |
| `nmtui` | Diálogos modais centralizados, formulário vertical rótulo-sobre-campo, Esc cancela. |
| `systemctl status` | Mensagens de erro que dizem o próximo comando a rodar (`sudo journalctl -u …`). |

## 2. Cor (tema `textual-dark`, sem tema próprio)

A TUI usa o tema padrão do Textual; não há paleta custom. Papéis efetivos (valores do tema, medidos):

| Papel | Token Textual | Valor | Onde |
|---|---|---|---|
| Fundo | `$background` | `#121212` | tela |
| Superfície | `$surface` | `#1E1E1E` | corpo dos diálogos |
| Painel | `$panel` | `#242F38` | régua de atalhos (`#keys`) |
| Faixa de status | `$boost` | `#FFFFFF0A` | `#status` |
| Texto | `$foreground` | `#E0E0E0` | tudo |
| Ação / foco / seleção | `$primary` | `#0178D4` | borda de diálogo, botão primário, linha selecionada |
| Sucesso (texto) | `$text-success` | `#8AD4A1` | linha de status `✓` |
| Erro (status) | `$text-error` sobre `$error-muted` | `#D17E92` / `#441E27` | linha de status `✕` |
| Erro (diálogo) | `$error` | `#B93C5B` | linha `✕` inline do formulário |
| Status da linha | ANSI `green`/`yellow`/`red`/`magenta` | do terminal | coluna Status |

Contraste (WCAG, medido): texto/fundo 13,9:1 · régua 10,1:1 · sucesso 9,8:1 · erro na faixa de status 4,9:1 ·
seleção (branco/`#0178D4`) 4,5:1 · **erro inline de diálogo 3,1:1 (abaixo de AA — ver V-01)**.

Regra: **cor nunca é o único sinal.** Todo estado tem símbolo + palavra:

| Estado | Render | Estilo |
|---|---|---|
| running | `● ativo` | green |
| starting | `◐ subindo` | yellow |
| failed | `✕ falhou` | bold red |
| fenced | `■ isolado` | bold magenta |
| desconhecido | `? —` | dim |
| mensagem ok / erro | `✓ …` / `✕ …` | success / error + fundo |

Em terminal monocromático ou com daltonismo a leitura continua completa.

## 3. Tipografia

Monoespaçada do terminal do usuário (não controlamos a fonte). Hierarquia só com o que o terminal garante:

- **Negrito**: título do diálogo, cabeçalho da tabela, tecla na régua de atalhos, seções da ajuda, erros.
- **Peso normal**: rótulos, valores, rótulos de atalho.
- **Dim**: só para estado desconhecido.
- Sem itálico, sublinhado ou maiúsculas decorativas (itálico some em muitos terminais via SSH).
- Glifos usados: `● ◐ ✕ ■ ✓ •••• — ↑ ↓ ▼ ▐X▌` e bordas arredondadas `╭╮╰╯`. Todos BMP, presentes em DejaVu/Liberation Mono.

## 4. Layout e densidade

Grade vertical fixa, de cima para baixo:

```
┌ DataTable (#table, 1fr)  ou  vazio (#empty, centralizado) ┐
│ Porta │ Status │ Proto │ Usuário │ Senha │ IPv6           │  colunas com largura = maior célula
│ …                                                          │  zebra + cursor de linha
├ #status (auto, máx. 3 linhas) ✓/✕ mensagem ────────────────┤
└ #keys (2 linhas fixas) régua de atalhos ───────────────────┘
```

- Sem Header (título não ajuda quem já abriu a ferramenta) e sem Footer do Textual: o Footer truncava à
  direita em 80 colunas; a régua tem 2 linhas fixas, cada uma ≤ 58 colunas, essenciais (`? c a s q`) na última.
- Padding horizontal de 1 célula; tabela sem margem. Uma linha por proxy: densidade máxima.
- Colunas na ordem de importância; **IPv6 por último** porque é a mais larga e a primeira a ser cortada.
  Em 60 colunas o IPv6 completo exige rolagem horizontal (`→`).
- Diálogo: 64 colunas, `max-width: 100%`, `max-height: 100%`, rolável; centralizado; borda `round $primary`.

Tamanhos suportados: **60x20 (mínimo)**, 80x24 (referência), 120x40 (folgado — o espaço extra fica vazio;
a TUI não estica colunas).

## 5. Forma

Um único raio: borda `round` nos diálogos. Sem sombras (terminal não tem). Separação por fundo
(`$surface`, `$panel`, `$boost`) em vez de linhas.

## 6. Componentes-chave

| Componente | Comportamento |
|---|---|
| Tabela | cursor de linha, zebra, `↑ ↓` movem; seleção = alvo das ações, fixado (id+porta) ao abrir a ação. |
| Linha de status | uma mensagem por vez, prefixo `✓`/`✕`; erros dizem o próximo passo (comando ou tecla). |
| Régua de atalhos | `tecla Rótulo` com tecla em negrito; sempre visível fora de diálogos. |
| Diálogo de formulário | título → rótulo/campo em pares → linha de erro `✕` → `Confirmar` / `Cancelar`. Enter envia, Esc cancela. |
| Confirmação destrutiva | excluir exige **digitar a porta**; rotacionar só confirma (foco já no botão). |
| Mensagem / Ajuda | só texto + `Fechar (Esc)`; ajuda rola com setas. |
| Senha | `••••` oculta, `n/d` desconhecida (API não devolve), `—` sem autenticação; `s` alterna. |

## 7. Estados

| Estado | Como aparece |
|---|---|
| Carregando | `#status` = `Carregando…`; mutação = `Criando proxy…`, `Rotacionando porta N…`. Sem spinner. |
| Vazio | texto centralizado: "Nenhum proxy cadastrado." + ação (`c` / `l`) + `?`. |
| Erro de carga | vazio troca para "Não foi possível carregar a lista." + `✕` no status com causa e comando. |
| Sem API / token inválido | idem, mensagem específica (`systemctl status ipv6-proxy-api`, `PROXY_API_TOKEN`). |
| Erro de validação | linha `✕` dentro do diálogo; diálogo não fecha; "nada foi alterado" quando relevante. |
| Em andamento | segunda ação → `✕ Aguarde: <ação> em andamento.` (uma mutação por vez). |
| Erro inesperado | mensagem genérica fixa; nunca traceback (pode conter senha). |

## 8. Motion

Nenhuma animação além das do Textual (abertura de modal). Atualização periódica (5 s, `PROXY_TUI_REFRESH`)
redesenha in place mantendo a seleção. Sem piscar, sem spinner.

## 9. Teclado e acessibilidade

- 100% teclado; mouse opcional. Um atalho = uma letra, sem modificadores (funciona em qualquer terminal/SSH).
- Tab / Shift+Tab entre campos, Enter envia, Esc fecha qualquer diálogo, `q` sai com código 0.
- Estado nunca só por cor (§2). Texto em PT-BR, frases completas, leitores de tela de terminal leem linha a linha.
- Paleta de comandos do Textual desligada (`ENABLE_COMMAND_PALETTE = False`) para não roubar `Ctrl+P`.

## 10. Tom de voz (PT-BR)

Direto, imperativo, técnico. Diz o que aconteceu, com o valor real, e o próximo passo.

- Sim: "Proxy criado na porta 30001 com IPv6 2001:db8::1." · "Digite exatamente 30001; nada foi alterado."
- Sim: "Token inválido. Confira PROXY_API_TOKEN em /etc/ipv6-proxy-api.env e rode com sudo."
- Não: "Ops! Algo deu errado 😕" · "Sucesso!" sem dado · jargão de marketing.
- Plural com `(s)` é aceito (economia de colunas).

## 11. Orçamento de performance

Não há página web, então LCP/CLS não se aplicam. Equivalentes para terminal:

- Primeira tela (lista) < 1 s após abrir com API local.
- Toda chamada de API fora da thread de UI (`asyncio.to_thread`); a UI nunca congela durante mutação.
- Sem dependências além de `textual` (+ `rich`); funciona por SSH a 9600 bps sem truecolor (degrada para 256/16 cores).

## 12. Checklist anti-slop (TUI)

| # | Item | Regra |
|---|---|---|
| A1 | Estado por cor + símbolo + palavra | nunca só cor |
| A2 | Contraste AA (≥ 4,5:1) para todo texto | inclusive erros |
| A3 | Cabe em 60x20 sem quebrar, sem crash, sem perder status/atalhos essenciais | — |
| A4 | Régua de atalhos visível e verdadeira (toda tecla listada funciona) | — |
| A5 | Toda ação destrutiva confirmada; excluir exige digitar | — |
| A6 | Erros com causa + próximo passo, nunca traceback | — |
| A7 | Estados vazio / carregando / erro / ocupado desenhados | — |
| A8 | Formulário: rótulo visível, campo distinguível, erro inline específico | — |
| A9 | Copy PT-BR consistente, sem emoji, sem "Ops!", dados reais na mensagem | — |
| A10 | Nada decorativo: sem banner ASCII, sem logo, sem Header, sem cor gratuita | — |
| A11 | Segredos: senha mascarada por padrão; nenhum segredo em mensagem de erro | — |
| A12 | Teclado completo; Esc sempre volta; q sempre sai | — |
