# E2E em VM real (T29): Ubuntu 24.04, QEMU TCG

Data: 2026-10-12/13. Executor: agente `vm` (tester). Nenhum token ou senha nestes arquivos (verificado com grep).

## Ambiente

- Host Debian 12 compartilhado, 8 CPUs, load 18 a 33, sem `/dev/kvm` e sem root.
- QEMU 7.2 (pacotes Debian extraídos com `dpkg -x`, sem root), `-accel tcg,thread=multi -smp 2 -m 3072`, rede slirp com hostfwd SSH.
- Imagem oficial `ubuntu-24.04-server-cloudimg-amd64.img` (serial 20260926). SHA256 conferido contra `SHA256SUMS`, e `SHA256SUMS.gpg` com assinatura válida da chave `D2EB 4462 6FDD C30B 513D 5BB7 1A5D 6C4C 7DB8 7C81` (UEC Image Automatic Signing Key). O kernel e o initrd `unpacked/` foram verificados da mesma forma.
- Boot com kernel direto (`systemd.default_device_timeout_sec=900`, `nowatchdog`). No boot padrão, o udev sob TCG passava dos 90 s e o sistema caía em emergency mode.
- cloud-init NoCloud (ISO via `genisoimage`) com usuário `e2e` e chave SSH gerada só para a VM, fora do git. Cada boot leva ~15 a 20 min.
- Laboratório: subnet ULA `fd00:1234::/64` em `lo` e cliente IPv4 `127.0.0.1`. `PROXY_BLOCK_PRIVATE_DESTINATIONS=false` está rotulado no env (streams vão para `[::1]`); a exceção é o ciclo com bloqueio LIGADO.
- **IPv6 roteado real: NOT_TESTED** (sem subnet roteada; `E2E_ROUTED_IPV6_TARGET` indefinido).

## SHAs

| Papel | SHA |
|---|---|
| Base main do fork | `aa2968556f861f56c7e5d37c70a859797a8d8772` |
| Base ashford | `bdfe9c7634691f51e893f6e53b6137f95792e4f5` |
| Rodada completa de upgrade + reboot | `1797601493521db85ab3335c46882536c024e87f` (T30 + T32) |
| Toggle de bloqueio (T34) | `9adde280b11c08ad735194ca538d54f07d50a693` |
| **Final (T35)**: update da VM migrada, instalação limpa e toggle off | `55bc2e2361d542a7cab71a520c63259f00342c60` |

Rodadas anteriores em `750a8c5` e `ad7413d` geraram os achados abaixo; os logs estão em `findings/`.

## Resultado por cenário

| Cenário | Resultado | Evidência |
|---|---|---|
| Upgrade aa29685 -> 1797601: base, proxies base, `--update --yes --self-test`, token/state preservados, state v2 com mesmo IPv6, unidades por proxy, 2ª execução idempotente (hashes e PIDs iguais), harness A (create/PATCH/rotate/delete de A + restart da API com streams B/C sem queda, gap < 1 s, egress = IPv6 do proxy, PIDs de B/C iguais), CLI/TUI/`--cli-only`/`--tui-only` sem reiniciar nada, 401 sem token | **PASS** | `upgrade-aa29685/results-*.tsv`, `pre-*`/`post-*` |
| Reboot REAL dessa VM (boot_id mudou) + post-reboot: API sobe sozinha, env/state/unidades/cfg idênticos, mesmos proxies (porta/IPv6/id), cada `/128` restaurado pelo ExecStartPre da própria instância, streams B/C OK | **PASS** | `upgrade-aa29685/post-boots.txt`, `journal-post-reboot.txt` |
| Upgrade bdfe9c7 -> 1797601 (mesma sequência + reboot real + post-reboot) | **PASS** (*) | `upgrade-bdfe9c7/` |
| Update 1797601 -> 9adde28 com `--self-test` (bloqueio ON): PIDs dos 4 proxies inalterados | **PASS** | `block-toggle/update-1797601-to-9adde28.log` |
| Toggle OFF via `--update` (9adde28): anuncia 4 proxies, reinicia um a um (PIDs novos só dos 4 afetados), cfg sem deny, drop-in removido, `IPAddressDeny` vazio | **PASS** | `block-toggle/toggle-off-9adde28.log` |
| Harness A/B/C em 9adde28 (bloqueio OFF) | **PASS** | `block-toggle/state-after-real-reboot-block-on.txt` (results.tsv) |
| Toggle ON via `--update` (9adde28): cfg com `deny ... privados` + `UDPASSOC`, os 4 reiniciados um a um, drop-in presente, `systemctl show -p IPAddressDeny` com `::1/128 ...` nas 4 unidades | **PASS** | `block-toggle/toggle-on-9adde28.log` |
| Bloqueio ON, camada kernel: processo dentro do cgroup de `ipv6-proxy@30011` não alcança `[::1]:22` (timeout; o controle fora da unidade conecta) e alcança `127.0.0.1:22` | **PASS** | `block-toggle/block-check-9adde28.log` |
| Bloqueio ON, camada 3proxy: `CONNECT [::1]:22` via proxy recebe 403 (cliente IPv4 atendido) | **PASS** | idem |
| Reboot REAL com bloqueio ON: post-reboot PASS e checagem de bloqueio PASS de novo; streams locais para `[::1]` recusados (403 / SOCKS code 2), comportamento esperado com o bloqueio ligado | **PASS** (streams: esperado) | `block-toggle/block-check-after-real-reboot-9adde28.log` |
| Update 9adde28 -> **55bc2e2** com `--self-test` (PIDs inalterados), toggle OFF (um a um), streams B/C locais OK | **PASS** | `block-toggle/update-9adde28-to-55bc2e2-toggle-off-post.log` |
| Instalação limpa **55bc2e2** logo após o 1º boot, sem espera manual (cloud-init `done`), com `--self-test --apt-lock-timeout 3600`: concluiu em 45 min sem intervenção | **PASS** (**) | `clean-install/` |
| Instalação limpa: CLI cria 2 proxies, unidades enabled, 0 3proxy no cgroup da API, env 0600; reboot REAL: mesmos proxies/IPv6/id, `/128` restaurados (`address added`), API pronta | **PASS** | `clean-install/checks-*.txt` |
| Tráfego por IPv6 roteado real | **NOT_TESTED** | sem subnet roteada |
| Debian 12 | **NOT_TESTED** | opcional; não houve tempo (cada ciclo leva horas sob TCG) |

(*) `base_install` aparece como NOT_TESTED: o `install.sh` **antigo** (aa29685/bdfe9c7) espera a API por 30 s fixos, e sob TCG ela leva 70 a 170 s. O teste espera a API manualmente e rotula. Isso é do instalador base, fora do produto.

(**) Espera pelo lock do apt (T35), três execuções na mesma imagem limpa:
1. `--apt-lock-timeout` padrão (600 s): esperou 600 s pelo apt do 1º boot (PID 1417), abortou com mensagem clara e não instalou nada (`findings/T35-apt-lock-timeout600-55bc2e2.log`). Sob TCG o apt do 1º boot passou de 10 min; numa VPS KVM leva poucos minutos.
2. Execução contaminada: um bug do meu driver lançou dois instaladores em paralelo. Ela mostra `Aguardando apt de outro processo (PID ...)` várias vezes e nunca mata processo alheio (`findings/T35-wait-evidence-dup-run.txt`), mas terminou rc=1 por causa da concorrência; não conta como resultado.
3. Execução final (a da tabela): no momento da instalação nenhum apt de terceiros segurava o lock (o log não tem mensagem de espera), então ela não prova a espera, só a instalação limpa logo após o boot.

## Achados (todos reportados ao orquestrador, com repro; corrigidos por outras tarefas)

| # | Achado | Tarefa | Status no SHA final |
|---|---|---|---|
| 1 | `READY_TIMEOUT=5` fixo: o self-test e o POST falham em host lento (bind ~21 s) | T30 | corrigido (`PROXY_READY_TIMEOUT`); self-test OK |
| 2 | Na migração, `systemctl start` estourava o timeout fixo de 30 s do subprocess e a porta ficava `fenced` (GET /proxies 503) enquanto o instalador reportava sucesso | T32 | corrigido; 0 portas fenced nas rodadas seguintes (`findings/T32-*`) |
| 3 | Toggle de `PROXY_BLOCK_PRIVATE_DESTINATIONS` via `--update` no mesmo SHA instalava o drop-in mas não re-renderizava as cfg nem reiniciava os proxies: falso "protegido" | T34 | corrigido; toggle on/off PASS (`findings/T34-*`) |
| 4 | Instalação limpa logo após o 1º boot falhava com o lock do apt | T35 | corrigido (espera o lock; timeout configurável) |
| 5 | Teste: `backup_private` procurava o layout antigo `/var/backups/ipv6-proxy-*` | (teste) | corrigido em `vm_e2e.sh` |

Notas sem bug de produto: dois kernel panics do guest (double fault / soft lockup) por falta de CPU no host sob TCG (`findings/guest-kernel-panic-tcg.txt`), mitigados com `-cpu qemu64,+sse4.2...`. Depois do panic, os proxies voltaram sozinhos no boot seguinte. `ipv6-proxy create --output json` não devolve `id` (o `list` devolve); é só uma observação de UX.

## Mudanças nos testes (branch agent/vm)

- `tests/e2e/vm_e2e.sh`: `E2E_BASE_SHA` (aa29685/bdfe9c7), `E2E_SELF_TEST`, `E2E_API_WAIT`, `E2E_READY_TIMEOUT`, `E2E_TUI_SECONDS`, `E2E_HTTP_TIMEOUT`; tolerância rotulada à espera fixa de 30 s dos instaladores base; checagem de backup no layout `/var/backups/ipv6-proxy/`.
- `tests/e2e/vm_block_toggle.sh` (novo): toggle via `--update` com cfg, PIDs por porta e `IPAddressDeny`.
- `tests/e2e/vm_block_check.sh` (novo): prova as duas camadas do bloqueio (probe no cgroup da unidade + ACL do 3proxy).

## Reproduzir

Os scripts host-side ficam em `.tools/vm/` (fora do git): `vm.sh` (new/start/ssh/wait/stop), `scenario.sh`/`go.sh` (pre-reboot, reboot real e post-reboot), `fresh.sh` (instalação limpa) e `ub-a-final.sh` (toggles). Na VM:

```
sudo E2E_BASE_SHA=<aa29685|bdfe9c7> E2E_TARGET_REF=<sha> E2E_SOURCE=checkout E2E_API_WAIT=600 \
     E2E_TUI_SECONDS=90 E2E_HTTP_TIMEOUT=180 tests/e2e/vm_e2e.sh --phase pre-reboot
sudo systemctl reboot
sudo E2E_TARGET_REF=<sha> E2E_API_WAIT=900 tests/e2e/vm_e2e.sh --phase post-reboot
sudo E2E_INSTALLER=<checkout>/install.sh tests/e2e/vm_block_toggle.sh on|off
sudo E2E_INSTALLER=<checkout>/install.sh tests/e2e/vm_block_check.sh
```
