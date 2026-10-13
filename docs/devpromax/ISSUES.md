# Problemas encontrados e soluções — IPv6 Proxy Server Independente

> Formato por item: ID · sintoma · causa raiz · solução · como evitar · commit.

## ISS-001 · GATE (critério E não satisfeito): E2E do instalador em VM systemd e IPv6 roteado
- Sintoma: critério E do brief (instalação limpa/upgrade/reboot em VM Linux com systemd, tráfego IPv6 roteado) sem ambiente.
- Causa raiz: host DPM sem root/docker/KVM; push público para CI não autorizado (pergunta #152 expirou); nenhuma VPS fornecida.
- Solução: systemd/reboot real cobertos em VM QEMU local Ubuntu 24.04 (T29, 38 PASS; evidência docs/evidence/e2e/ubuntu-24.04/). IPv6 ROTEADO real: critério NÃO satisfeito, documentado (ADR decisão provisória #158); executar tests/e2e/vm_e2e.sh com E2E_ROUTED_IPV6_TARGET na VPS do Otávio. Tarefa T14; quando autorizado, push da branch dedicada e rodar workflow E2E (T08) ou executar na VPS do Otávio.
- Como evitar: pedir ambiente de teste (VM/runner) no brief de projetos de infraestrutura.

## ISS-002 — test_failure_and_rollback_of_a_preserve_b erro no fixture env: ValidationError Settings.bi
- **Quando:** 2026-10-11 20:34 · **Autor:** `ipv6-proxy-server-independente--core`
- **Sintoma:** test_failure_and_rollback_of_a_preserve_b erro no fixture env: ValidationError Settings.binary=None (tests/test_isolation_3proxy.py:161) quando TEST_3PROXY_BINARY nao definido
- **Causa raiz:** tests/test_hermes_failure_isolation.py importa o fixture env de test_isolation_3proxy mas nao o pytestmark skipif do modulo; gate nao e herdado entre modulos
- **Solução:** importar pytestmark junto com o fixture, aplicando o mesmo gate TEST_3PROXY_BINARY
- **Como evitar:** testes que reutilizam fixtures de 3proxy real devem importar/declarar o pytestmark do gate
- **Commit:** -

## ISS-003 — argparse ecoava em stderr argumentos nao reconhecidos e expandia abreviacoes: 'ipv6-proxy 
- **Quando:** 2026-10-11 22:06 · **Autor:** `ipv6-proxy-server-independente--cli`
- **Sintoma:** argparse ecoava em stderr argumentos nao reconhecidos e expandia abreviacoes: 'ipv6-proxy batch --password S3cret' exibia a senha no erro (e '--password' casaria por prefixo com '--password-stdin')
- **Causa raiz:** ArgumentParser padrao usa allow_abbrev=True e error() inclui os valores nao reconhecidos
- **Solução:** Subclasse Parser com allow_abbrev=False e error() que omite os valores de 'unrecognized arguments'
- **Como evitar:** Teste test_abbreviated_password_flag_is_rejected_without_echo; todo parser de CLI que lida com segredos deve usar essa subclasse
- **Commit:** -

## ISS-004 — update com falha via die (exit) deixava codigo novo + API velha sem rollback
- **Quando:** 2026-10-11 23:50 · **Autor:** `ipv6-proxy-server-independente--inst`
- **Sintoma:** update com falha via die (exit) deixava codigo novo + API velha sem rollback
- **Causa raiz:** bash nao dispara trap ERR em exit explicito; so em comando com status != 0 sob set -e
- **Solução:** transacao begin_tx/end_tx: trap ERR so registra a linha; trap EXIT com rc!=0 e BASHPID==$$ executa rollback com set +e
- **Como evitar:** teste que injeta die e false dentro da transacao (tests/test_install.py::test_die_inside_transaction_rolls_back)
- **Commit:** -

## ISS-005 — TUI: exceção não prevista numa ação (ex.: export para ~usuarioinexistente/x) caía no handl
- **Quando:** 2026-10-11 23:56 · **Autor:** `ipv6-proxy-server-independente--clients`
- **Sintoma:** TUI: exceção não prevista numa ação (ex.: export para ~usuarioinexistente/x) caía no handler do Textual, que imprime traceback com show_locals=True: senhas em claro no terminal
- **Causa raiz:** _guarded/export só capturavam ApiError/OSError; App._fatal_error do Textual usa rich Traceback(show_locals=True)
- **Solução:** Capturar Exception nas ações com mensagem genérica PT-BR e sobrescrever ProxyTui._fatal_error com linha fixa sem traceback
- **Como evitar:** tests/test_tui.py::test_a6_* verificam que nenhuma senha aparece no status nem nos renderables de saída
- **Commit:** -

## ISS-006 — /usr/local/bin/ipv6-proxy-tui instalado falhava com ModuleNotFoundError: proxy_tui
- **Quando:** 2026-10-11 23:56 · **Autor:** `ipv6-proxy-server-independente--clients`
- **Sintoma:** /usr/local/bin/ipv6-proxy-tui instalado falhava com ModuleNotFoundError: proxy_tui
- **Causa raiz:** entrypoint inseria no sys.path o diretório do próprio arquivo (/usr/local/bin), não /opt/ipv6-proxy-server; e o Python do sistema não tem Textual
- **Solução:** entrypoint deriva o app_dir de ../../../opt/ipv6-proxy-server relativo a si e re-executa uma vez com .venv/bin/python
- **Como evitar:** tests/test_tui_pty.py::test_installed_entrypoint_finds_app_and_venv_from_any_cwd simula a árvore instalada
- **Commit:** -

## ISS-007 — reconcile de 200 proxies reais levava 86s (430 ms/proxy) a frio e 28s a quente; 2 portas f
- **Quando:** 2026-10-12 00:31 · **Autor:** `ipv6-proxy-server-independente--back`
- **Sintoma:** reconcile de 200 proxies reais levava 86s (430 ms/proxy) a frio e 28s a quente; 2 portas falhavam no teste
- **Causa raiz:** Por proxy: 2 fsync do .addr mesmo inalterado, 1 'ip -j addr show' + 1 fsync do journal, espera de prontidão serial; teste usava portas efêmeras (free_ports) reocupadas por conexões durante a execução longa
- **Solução:** Gravar .addr só quando muda; ler endereços da interface e gravar o journal uma vez por reconcile; lançar todas as instâncias e depois esperar prontidão; portas do teste abaixo da faixa efêmera. Resultado: 42s frio / 8s quente para N=200
- **Como evitar:** tests/test_supervisor_3proxy.py::test_reconcile_of_200_proxies_is_measured_and_adopts_on_restart com teto de tempo (cold<60s, warm<20s)
- **Commit:** -

## ISS-008 — SOCKS5 UDP ASSOCIATE com deny de redes privadas: clientes padrao ficam sem resposta, e cli
- **Quando:** 2026-10-12 02:52 · **Autor:** `ipv6-proxy-server-independente--sec2`
- **Sintoma:** SOCKS5 UDP ASSOCIATE com deny de redes privadas: clientes padrao ficam sem resposta, e cliente que declara IPv6 publico no ASSOCIATE alcanca [::1] por UDP
- **Causa raiz:** 3proxy 0.9.4 avalia a ACL do UDP ASSOCIATE contra o endereco declarado pelo cliente (0.0.0.0 cai em ::ffff:0:0/96), nao contra o destino de cada datagrama do relay
- **Solução:** Em aberto (T22 N1): negar UDPASSOC com bloqueio ligado ou filtrar destino no kernel (IPAddressDeny= na unit ipv6-proxy@)
- **Como evitar:** tests/test_security_udp.py (xfail strict) com 3proxy real; nunca confiar em ACL de destino do 3proxy para UDP
- **Commit:** -

## ISS-009 — Com PROXY_BLOCK_PRIVATE_DESTINATIONS=true, SOCKS5 UDP ASSOCIATE declarando IPv6 publico en
- **Quando:** 2026-10-12 03:46 · **Autor:** `ipv6-proxy-server-independente--back2`
- **Sintoma:** Com PROXY_BLOCK_PRIVATE_DESTINATIONS=true, SOCKS5 UDP ASSOCIATE declarando IPv6 publico entregava datagramas a [::1]; ASSOCIATE 0.0.0.0 pendurava
- **Causa raiz:** 3proxy 0.9.4 avalia a ACL do UDP ASSOCIATE so contra o endereco declarado pelo cliente e nao checa o destino de cada datagrama do relay
- **Solução:** render_config emite 'deny * * * * UDPASSOC' antes do deny de redes; /capabilities so anuncia socks5_udp_associate com bloqueio off; IPAddressDeny= de faixas IPv6 internas na unit ipv6-proxy@ (ADR 0007). Obs.: o 3proxy nao envia REP de erro, fecha a conexao apos ~30s
- **Como evitar:** tests/test_security_udp.py com 3proxy real (verificado por mutacao: sem a regra o teste falha); teste unitario da unit
- **Commit:** -

## ISS-010 — TUI: IPv6 com grupo ':1234:' virava emoji; rodapé truncado em 80x24/60x20; snapshot f04 in
- **Quando:** 2026-10-12 05:11 · **Autor:** `ipv6-proxy-server-independente--tuifix`
- **Sintoma:** TUI: IPv6 com grupo ':1234:' virava emoji; rodapé truncado em 80x24/60x20; snapshot f04 intermitente (SOCKS vs SOCKS5)
- **Causa raiz:** DataTable/Static interpretam str como markup Rich (emoji codes e [tags]); Footer compacto corta à direita; DataTable mede larguras auto só no idle, e o modal de resultado do lote abre antes; Button ignora clique por 0.2s após press (-active), derrubando submit rápido nos testes
- **Solução:** Células como Text() literal e Static(markup=False) para dados da API; rodapé próprio em duas linhas fixas (essenciais embaixo, <=58 col); render_rows recria colunas com width explícita; helper click_ok nos testes espera -active sair
- **Como evitar:** Nunca passar dado externo como str a widgets Textual com markup; teste com ':1234:' e '[bold]' em test_tui.py
- **Commit:** -

## ISS-003 · SSRF nos proxies (preexistente em bdfe9c7)
- Sintoma: cliente de qualquer proxy alcançava [::1], fc00::/7 (IMDS), fe80::/10 do host; UDP ASSOCIATE relayava a [::1].
- Causa: `auth none`/`allow *` sem ACL de destino; ACL do 3proxy não cobre destino real do relay UDP.
- Solução: auth iponly/strong + deny de faixas privadas e subnet; deny UDPASSOC; drop-in IPAddressDeny (T18, T23; ADR-0007).
- Como evitar: teste de segurança real com 3proxy em todo render_config.

## ISS-004 · Rollback do instalador não disparava / parava no meio (revisão T09 A2/A3)
- Causa: `exit` não aciona `trap ERR`; rollback com `set -e`. Solução: transação com `trap EXIT` rc!=0, rollback `set +e` por passo (T17).

## ISS-005 · TUI exibia senhas em traceback e corrompia IPv6 com emoji
- Causa: handler padrão do Textual com show_locals; dados renderizados como markup Rich. Solução: captura genérica + `_fatal_error`; Text literal (T19, T24).

## ISS-006 · Conflitos repetidos em tests/tui_snapshots
- Causa: suíte regravava SVG/JSON a cada execução. Solução: comparação com referência e regravação só por flag (T20).

## ISS-015 — Reconcile of 200 real proxies fenced all of them after per-proxy deadlines counted from la
- **Quando:** 2026-10-12 11:56 · **Autor:** `ipv6-proxy-server-independente--slowfix`
- **Sintoma:** Reconcile of 200 real proxies fenced all of them after per-proxy deadlines counted from launch
- **Causa raiz:** _wait_ready checked the deadline before probing; by the time a proxy's turn came in the serial wait its deadline from launch was already past, so it was never probed
- **Solução:** Probe ready() first, then check is_active/deadline; a proxy already up is accepted even if its own deadline elapsed
- **Como evitar:** Unit test with fake clock (since in the past + ready) and the 200-proxy real 3proxy test in the full suite
- **Commit:** -

## ISS-016 — Port stayed fenced forever (mutations 503) after reconcile: systemctl start ipv6-proxy@N e
- **Quando:** 2026-10-12 16:51 · **Autor:** `ipv6-proxy-server-independente--fencefix`
- **Sintoma:** Port stayed fenced forever (mutations 503) after reconcile: systemctl start ipv6-proxy@N exceeded the fixed 30 s subprocess timeout, but the unit came up right after
- **Causa raiz:** run_command used a fixed timeout=30 and TimeoutExpired propagated as a launch failure, fencing the port with no recovery path other than restarting the API
- **Solução:** systemctl timeout = max(30, PROXY_READY_TIMEOUT+15); start/restart TimeoutExpired is logged and judged by readiness (is-active including activating + MainPID owning the socket); admin POST /proxies/{port}/recover adopts or brings up only that port
- **Como evitar:** Never treat a timed-out systemd job as a failure; always re-check the real state before fencing; every fence needs a per-port recovery path
- **Commit:** -

## ISS-017 — install.sh --update --yes after toggling PROXY_BLOCK_PRIVATE_DESTINATIONS on the same SHA 
- **Quando:** 2026-10-12 22:14 · **Autor:** `ipv6-proxy-server-independente--togglefix`
- **Sintoma:** install.sh --update --yes after toggling PROXY_BLOCK_PRIVATE_DESTINATIONS on the same SHA swapped the drop-in and said 'Atualizacao concluida', but proxies/<port>.cfg kept 0 deny lines and MainPIDs were unchanged (false security success)
- **Causa raiz:** update_install only computed config_changes and restarted the API when app_changed; an env-only change left the API (and its reconciliation) untouched, so the rendered configs were never re-applied
- **Solução:** config_changes always runs with the current env; non-empty result triggers confirmation (REINICIAR/--yes), API restart (reconcile restarts divergent proxies one by one) and verify_configs_applied (cfg matches env, deny/UDPASSOC present or absent, MainPID changed for affected ports) with rollback on failure
- **Como evitar:** tests/test_install.py toggle tests (off->on, on->off, no --yes, only affected, not-applied rolls back); never print success without verifying the effect
- **Commit:** -

## ISS-018 — Instalacao limpa logo apos 1o boot falha rc=100: E: Could not get lock /var/lib/apt/lists/
- **Quando:** 2026-10-12 23:44 · **Autor:** `ipv6-proxy-server-independente--aptfix`
- **Sintoma:** Instalacao limpa logo apos 1o boot falha rc=100: E: Could not get lock /var/lib/apt/lists/lock (apt-get de outro processo)
- **Causa raiz:** install.sh rodava apt-get update/install sem esperar o lock; apt-daily/unattended-upgrades segura o lock no 1o boot; apt antigo ignora DPkg::Lock::Timeout no lock de lists
- **Solução:** apt-get com -o DPkg::Lock::Timeout=<restante>; antes do update espera o lock de /var/lib/apt/lists/lock lendo /proc/locks (fcntl, invisivel a flock) com prazo --apt-lock-timeout (600 s), sem matar processo; prazo esgotado = erro acionavel antes de instalar algo
- **Como evitar:** Testes com stub de /proc/locks (espera->sucesso, prazo->erro limpo, corrida no update->retry)
- **Commit:** -

## ISS-007 · Readiness de 5 s fixa falhava em host lento (achado E2E VM)
- Sintoma: create/self-test falhavam em VM TCG (start ~14 s, bind ~21 s). Solução: PROXY_READY_TIMEOUT (30 s) e --api-timeout (T30).

## ISS-008 · Timeout de systemctl cercava porta para sempre (achado E2E VM)
- Sintoma: migração bdfe9c7→final deixou porta fenced com unidade ativa. Solução: timeout derivado, adoção por readiness, POST /proxies/{port}/recover (T32).

## ISS-009 · Ligar bloqueio anti-SSRF via --update não reaplicava configs (achado E2E VM, segurança)
- Sintoma: instalador dizia "concluída" com proxies sem deny. Solução: config_changes com env efetivo, reinício um a um, verificação final (T34).

## ISS-010 · Instalação limpa logo após 1º boot falhava no lock do apt (achado E2E VM)
- Solução: espera do lock com --apt-lock-timeout (600 s), sem matar processo alheio (T35). Ressalva: sob TCG o apt do 1º boot passou de 10 min.
