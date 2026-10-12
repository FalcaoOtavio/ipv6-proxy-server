# Limites medidos (QA)

Gerado por `tests/acceptance/measure_limits.py` em 2026-10-12 03:06:50 -03.
Escopo de rede: loopback v4-mapped (::ffff:127.0.0.0/120) - loopback/ULA complementar, NAO IPv6 roteado. 3proxy real 0.9.4, supervisor local (um processo por porta),
API uvicorn real no mesmo processo do script. Host: 8 CPUs, load average 22.85 19.83 19.79 (host compartilhado; tempos sao limite superior, nao benchmark).

| Medida | Valor |
|---|---|
| Lote 100 HTTP via CLI (credencial comum, `-f`) | 26.56 s |
| Lote 100 SOCKS5 via CLI (`--random`, `-f`) | 39.45 s |
| Lote 200 SOCKS5 via API (`POST /proxies/batch`) | 46.34 s |
| Lote 200 HTTP via API com SSRF ligado (padrao de producao) | 48.37 s |
| RSS mediano por 3proxy com SSRF ligado | 4772.0 KiB |
| Remover 200 (DELETE sequencial) | 41.41 s |
| `GET /proxies` com 200 | 133.7 ms |
| curl pelas 200 linhas exportadas (sequencial) | 13.33 s |
| RSS por 3proxy (min/mediana/max) | 4604 / 4772.0 / 4852 KiB |
| RSS total dos 200 3proxy | 929.6 MiB |
| fds por 3proxy ocioso (min/mediana/max) | 4 / 4.0 / 4 |
| fds do processo da API com 200 proxies | 12 |

## Latencia por operacao (API, com 200 proxies vivos)

| Operacao | n | p50 | p95 | max |
|---|---|---|---|---|
| create | 20 | 417.3 ms | 871.0 ms | 1035.9 ms |
| edit | 20 | 282.6 ms | 773.3 ms | 912.4 ms |
| rotate | 20 | 548.6 ms | 698.1 ms | 777.2 ms |
| delete | 20 | 267.0 ms | 815.7 ms | 1103.2 ms |

CLI (inclui partida do Python e leitura do env): create 1078.1 ms, edit 870.6 ms, rotate 1429.7 ms, delete 1230.3 ms

## Observacoes

- SSRF (`PROXY_BLOCK_PRIVATE_DESTINATIONS`): o harness loopback roda com o bloqueio DESLIGADO,
  porque o destino de teste (`::ffff:127.0.0.1`) e um destino privado negado. Lote, curl, streams,
  RSS e latencia acima foram medidos assim. A fase final liga o bloqueio (padrao de producao),
  remede o lote de 200 e o RSS e confirma a recusa do loopback (HTTP 403).
- Cada proxy e um processo 3proxy: memoria e fds escalam linearmente com o numero de proxies.
- Em systemd cada instancia adiciona uma unidade `ipv6-proxy@<porta>`; o custo de `systemctl` nao esta
  incluido aqui (supervisor local). Medir na VM (T14).
- A faixa v4-mapped de teste (/120) limita a 255 enderecos; em producao o limite e a subnet e
  `PROXY_BATCH_MAX` (padrao 1000).
- Falhas nesta execucao: 0.
