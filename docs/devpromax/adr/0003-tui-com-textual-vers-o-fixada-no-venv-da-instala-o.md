# ADR 0003: TUI com Textual (versão fixada) no venv da instalação

- Data: 2026-10-11
- Status: aceito

## Contexto
TUI PT-BR com teclado, tabelas, diálogos, terminal pequeno, testável em pseudo-terminal.

## Decisão
Textual (pin exato em requirements) como cliente HTTP da API local, mesmo token/env do CLI; testes com Textual Pilot (capturas SVG) e pty real (pexpect, só dev).

## Alternativas
curses stdlib (muito código para diálogos/tabelas/acessibilidade); urwid (API mais antiga, menos testável); prompt_toolkit (sem tabela pronta).

## Consequências
Nova dependência pip fixada no venv /opt/ipv6-proxy-server; --tui-only instala/atualiza só a TUI sem tocar proxies.
