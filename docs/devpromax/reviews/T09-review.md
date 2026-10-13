T09 revisão bdfe9c7..main (767761c). VEREDITO: AJUSTES. Núcleo correto: sem PartOf/BindsTo, sem killall/pkill/flush, mutações só no alvo (systemctl <verbo> ipv6-proxy@<porta>), readiness por PID dono do socket, If-Match/Idempotency/owner/capabilities preservados (só aditivos). MAP.md atualizado (../docs/MAP.md).

ALTO
A1 install.sh:686-704 + app.py list_proxies: um proxy caído/fenced faz GET /proxies (legado) responder 503; wait_api (install.sh:497) usa essa rota => update com 1 proxy quebrado não relacionado expira em 45s e faz ROLLBACK + restart da API (contradiz comentário 'proxy already down is not this update's failure'). Repro: /tmp/rev_fenced.py (reconcile com 1 porta falha => GET /proxies 503, include=status 200). Fix: wait_api/healthcheck usar /proxies?include=status ou /capabilities; CLI list/export (ipv6-proxy:587,607) idem (hoje não lista nada se 1 proxy cair).
A2 install.sh:46/441/388: die => exit não dispara trap ERR (verificado: bash -Eeuo, trap ERR, exit 1 => sem trap). Falha em install_client/restore após replace_dir deixa código novo + API velha + VERSION velho, sem rollback. Fix: flag in_tx + trap EXIT com rc!=0.
A3 install.sh:546-566 rollback roda com set -e: falha em restore_installation(die) ou daemon-reload sai antes do 'systemctl start' => na migração TODOS os proxies ficam parados. Fix: set +e no rollback, sempre tentar subir API.
A4 install.sh:462-467 restarted_unchanged: unidade inativa/failed antes (MainPID=0) e reconciliada depois tem PID novo => falso positivo => rollback. Fix: ignorar PID 0 / comparar só active_before.
A5 install.sh:499 prazo fixo 45s; reconcile síncrono O(N) com 'enable --now' (daemon-reload implícito do PID1, supervisor.py:70) + até 5s por porta => migração com centenas de proxies estoura e faz rollback. Fix: prazo proporcional a N e 'enable --now --no-reload'? (não existe p/ enable; usar 'enable' só se não enabled + start).
A6 proxy_tui/app.py:325-332 e _guarded:182: só captura ApiError/OSError; outra exceção (ex.: export para '~usuarioinexistente/x' => RuntimeError em expanduser) cai no handler do Textual com show_locals => SENHAS em claro no terminal (reproduzido pelo subagente). Fix: capturar Exception com mensagem genérica.
A7 Clientes não enviam Idempotency-Key (ipv6-proxy:539,617; proxy_tui/client.py:96): timeout de 60s num batch grande (até 5s/porta) => servidor conclui, CLI perde senhas aleatórias (só na 1ª resposta) e retry duplica lote. Fix: uuid4 por invocação + timeout proporcional ao count.

MÉDIO
M1 ipv6-proxy:619-626 rotate/delete da CLI sem If-Match (edit faz certo): entre confirm() e envio, delete+recreate na porta => apaga o herdeiro. Fix: GET antes, mostrar id, enviar If-Match.
M2 tests/acceptance/test_criteria_ab.py:253/314 FLAKY (falhou 2 de 3 execuções): batch count=2 com start_port=ports[39] cria 21040, fora de allowed_ports=ports[4:40]. Bug do teste, não do produto; fix: allowed = h.ports[4:]. Evidência commitada veio de execução sortuda.
M3 install.sh:680 update com render_config alterado reinicia TODOS os proxies via reconcile sem aviso/--yes (caminho coletivo não sinalizado). Fix: pré-renderizar e exigir confirmação.
M4 install.sh:2 set -E: trap ERR roda dentro de $(...) (moved=$(...), for port in $(instance_ports)) => rollback em subshell/duplicado. Fix: [[ $BASHPID == $$ ]] || exit 1 no rollback.
M5 install.sh:403 'install -d -m 0700 $backup_root' muda /var/backups para 0700. Backups com token/senhas nunca são limpos.
M6 install.sh:593-626 instalação limpa sem rollback: falha em pip deixa estado 'partial' que trava execuções seguintes.
M7 backend.py:206 reconcile para (disable --now) toda ipv6-proxy@* ausente de state.json: state.json restaurado antigo/vazio => parada coletiva no start da API. Fix: limite/aviso ou exigir flag para parar >1 órfão.
M8 requirements.txt fastapi/pydantic/uvicorn em faixas, sem hashes (regra 'versões fixadas'; --ref não reprodutível).
M9 tests/test_tui.py:93 snapshots SVG sobrescritos a cada run sem comparação (não testam nada) e sujam o repo; acceptance também reescreve evidence/*.json. Fix: pytest-textual-snapshot ou tmp_path.

BAIXO: export TUI sem fsync (app.py:48); limites de count inconsistentes CLI 64512/TUI 10000/API 1000; TUI nunca envia partial (código de failed morto, app.py:261); reload permitido durante mutação; marca asyncio indevida test_tui.py:395; StartLimit da unidade pode esgotar no boot (RestartSec=2); docs/MIGRATION.md rollback manual não restaura 3proxy/TUI; VERSION=unknown silencioso (dubious ownership); shellcheck rc=1 em tests/e2e/vm_e2e.sh (SC2329) que tests.yml roda; testes de install nunca exercitam o trap ERR nem fresh_install.

Contratos ashford: OK (rotas/headers/campos só aditivos; GET /proxies legado preservado — por isso A1 deve ser resolvido nos clientes/instalador, não mudando a rota).
