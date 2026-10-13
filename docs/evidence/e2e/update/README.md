# T38 — E2E `ipv6-proxy update` (VM Ubuntu 24.04, QEMU TCG)

Resultado: PASS=22 FAIL=0 (`results.tsv`, `check.log`).

- Instalado: `fd99a99` (primeira revisão com `update`); alvo: `64116ed` (revisão sintética, 1 linha em `proxy_api/__init__.py`, nunca publicada).
- Fonte de update espelhada só na VM (produto intocado): `git url.insteadOf` para `REPO_URL` + `raw.githubusercontent.com` -> 127.0.0.1 com TLS de CA descartável confiada só na VM (`tests/e2e/vm_update.sh mirror`).
- Asserções: `update --check` exit 10 e mostra os dois SHAs, sem alterar nada; `update --self-test` -> VERSION com SHA novo, mesmos MainPID dos 2 proxies, proxies HTTP/SOCKS5 respondendo, API saudável e mesma lista; 2ª execução "Nada a atualizar" sem mudar nada; `--ref` inválido (inexistente, SHA inexistente, `a;b`, abreviado) falha sem mudar nada.
- Reproduzir: `tests/e2e/vm_update_driver.sh` (usa `.tools/vm`).
- Nota: 1ª tentativa descartada (instalou 139a726, que ainda não tinha `update`; erro do teste, não do produto).
