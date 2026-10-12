# Limites medidos (QA T08)

Gerado por `tests/acceptance/measure_limits.py` em 2026-10-11 23:06:23 -03.
Escopo de rede: loopback v4-mapped (::ffff:127.0.0.0/120) - loopback/ULA complementar, NAO IPv6 roteado. 3proxy real 0.9.4, supervisor local (um processo por porta),
API uvicorn real no mesmo processo do script. Host: 8 CPUs, load average 28.04 28.34 27.98 (host compartilhado; tempos sao limite superior, nao benchmark).

| Medida | Valor |
|---|---|
| Lote 100 HTTP via CLI (credencial comum, `-f`) | 33.6 s |
| Lote 100 SOCKS5 via CLI (`--random`, `-f`) | 44.27 s |
| Lote 200 SOCKS5 via API (`POST /proxies/batch`) | 84.14 s |
| Remover 200 (DELETE sequencial) | 41.43 s |
| `GET /proxies` com 200 | 456.5 ms |
| curl pelas 200 linhas exportadas (sequencial) | 12.71 s |
| RSS por 3proxy (min/mediana/max) | 4600 / 4780.0 / 4852 KiB |
| RSS total dos 200 3proxy | 930.4 MiB |
| fds por 3proxy ocioso (min/mediana/max) | 4 / 4.0 / 4 |
| fds do processo da API com 200 proxies | 12 |

## Latencia por operacao (API, com 200 proxies vivos)

| Operacao | n | p50 | p95 | max |
|---|---|---|---|---|
| create | 20 | 462.5 ms | 988.0 ms | 1059.3 ms |
| edit | 20 | 517.4 ms | 908.6 ms | 1068.4 ms |
| rotate | 20 | 625.9 ms | 1058.5 ms | 1108.5 ms |
| delete | 20 | 224.4 ms | 723.7 ms | 920.7 ms |

CLI (inclui partida do Python e leitura do env): create 3547.8 ms, edit 2194.2 ms, rotate 1851.8 ms, delete 1483.5 ms

## Observacoes

- Cada proxy e um processo 3proxy: memoria e fds escalam linearmente com o numero de proxies.
- Em systemd cada instancia adiciona uma unidade `ipv6-proxy@<porta>`; o custo de `systemctl` nao esta
  incluido aqui (supervisor local). Medir na VM (T14).
- A faixa v4-mapped de teste (/120) limita a 255 enderecos; em producao o limite e a subnet e
  `PROXY_BATCH_MAX` (padrao 1000).
- Falhas nesta execucao: 0.
