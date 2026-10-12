#!/usr/bin/env bash
# shellcheck disable=SC2317,SC2329  # step_* functions are invoked indirectly via "step_$current"
# VM end-to-end: base aa29685 -> install.sh --update to the target SHA on a clean,
# disposable Linux host with systemd. DESTRUCTIVE for that host's ipv6-proxy setup:
# never run it on a production VPS.
#
#   sudo E2E_SUBNET=... E2E_INTERFACE=... E2E_IPV4=... tests/e2e/vm_e2e.sh --phase pre-reboot [--reboot]
#   (reboot)
#   sudo tests/e2e/vm_e2e.sh --phase post-reboot
#   sudo tests/e2e/vm_e2e.sh --phase no-reboot       # CI: everything except the reboot
#   tests/e2e/vm_e2e.sh --dry-run --phase pre-reboot # print the plan; no root, no changes
#
# Idempotent: finished steps are recorded in $E2E_STATE_DIR and skipped on rerun.
# Every step ends PASS, FAIL or NOT_TESTED in $E2E_STATE_DIR/results.tsv; exit 1 if any FAIL.
set -Eeuo pipefail
umask 077

# Base release to upgrade from: aa29685 (fork main, default) or bdfe9c7 (ashford/isolated-mutations).
BASE_SHA=${E2E_BASE_SHA:-aa2968556f861f56c7e5d37c70a859797a8d8772}
FORK_URL=https://github.com/FalcaoOtavio/ipv6-proxy-server
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd -- "$here/../.." && pwd)
# Root reads a checkout owned by another user (CI runner, sudo): trust only this one.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=$repo

E2E_SUBNET=${E2E_SUBNET:-fd00:1234::/64}
E2E_INTERFACE=${E2E_INTERFACE:-lo}
E2E_IPV4=${E2E_IPV4:-127.0.0.1}
E2E_TARGET_REF=${E2E_TARGET_REF:-HEAD}
# checkout: run install.sh from a local checkout of the target SHA (works before any push);
# fork: pass --ref so install.sh clones FalcaoOtavio/ipv6-proxy-server itself (SHA must be published).
E2E_SOURCE=${E2E_SOURCE:-checkout}
E2E_STATE_DIR=${E2E_STATE_DIR:-/var/lib/ipv6-proxy-e2e}
E2E_STREAM_SECONDS=${E2E_STREAM_SECONDS:-15}
# Echo service reached through the proxies over routed IPv6; must answer with the
# caller's address in the body (e.g. https://api6.ipify.org). Unset => NOT_TESTED.
E2E_ROUTED_IPV6_TARGET=${E2E_ROUTED_IPV6_TARGET:-}
# Streams go to [::1] and egress from the ULA lab subnet: both denied by the SSRF block
# (ACL + IPAddressDeny drop-in). The lab therefore runs with the block OFF (insecure, lab
# only); set E2E_BLOCK_PRIVATE=true only with a public target and a routed subnet.
E2E_BLOCK_PRIVATE=${E2E_BLOCK_PRIVATE:-false}
# false skips install.sh --self-test on the update (recorded NOT_TESTED), e.g. while a
# slow emulated host (QEMU TCG) exceeds the proxy readiness deadline.
E2E_SELF_TEST=${E2E_SELF_TEST:-true}

APP_DIR=/opt/ipv6-proxy-server
ENV_FILE=/etc/ipv6-proxy-api.env
DATA_DIR=/var/lib/ipv6-proxy-api
CLI=/usr/local/bin/ipv6-proxy
TUI=/usr/local/bin/ipv6-proxy-tui
API=http://127.0.0.1:8000
# Base proxies (created on aa29685) and the harness A/B/C (created after the update).
BASE_HTTP=30001 BASE_SOCKS=30002 PORT_A=30010 PORT_B=30011 PORT_C=30012

usage() { sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { printf 'vm_e2e: %s\n' "$*" >&2; exit 2; }

phase='' dry_run=false do_reboot=false
while (($#)); do
    case "$1" in
        --phase)
            (($# >= 2)) || die 'Falta valor para --phase'
            phase=$2; shift 2 ;;
        --dry-run) dry_run=true; shift ;;
        --reboot) do_reboot=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "Opcao desconhecida: $1 (use --help)" ;;
    esac
done
[[ $E2E_SOURCE == checkout || $E2E_SOURCE == fork ]] || die 'E2E_SOURCE deve ser checkout ou fork'
case "$phase" in
    pre-reboot) steps=(base_install base_proxies update migration idempotency harness_a clients traffic snapshot) ;;
    post-reboot) steps=(post_reboot traffic_post) ;;
    no-reboot) steps=(base_install base_proxies update migration idempotency harness_a clients traffic) ;;
    *) die '--phase deve ser pre-reboot, post-reboot ou no-reboot' ;;
esac
[[ $do_reboot == false || $phase == pre-reboot ]] || die '--reboot so vale com --phase pre-reboot'

if $dry_run; then
    printf 'phase=%s target_ref=%s base=%s subnet=%s interface=%s ipv4=%s state=%s routed_target=%s ssrf_block=%s self_test=%s\n' \
        "$phase" "$E2E_TARGET_REF ($E2E_SOURCE)" "$BASE_SHA" "$E2E_SUBNET" "$E2E_INTERFACE" "$E2E_IPV4" "$E2E_STATE_DIR" \
        "${E2E_ROUTED_IPV6_TARGET:-<unset: NOT_TESTED>}" "$E2E_BLOCK_PRIVATE" "$E2E_SELF_TEST"
    printf 'step %s\n' "${steps[@]}"
    $do_reboot && printf 'step reboot\n'
    exit 0
fi

[[ $(uname -s) == Linux ]] || die 'Somente Linux.'
[[ $EUID == 0 ]] || die 'Execute com sudo (VM descartavel).'
[[ -d /run/systemd/system ]] || die 'systemd nao esta ativo neste host.'
for tool in git curl python3 sha256sum systemctl ip; do
    command -v "$tool" >/dev/null || die "Falta $tool"
done

mkdir -p "$E2E_STATE_DIR"
chmod 0700 "$E2E_STATE_DIR"
results=$E2E_STATE_DIR/results.tsv
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# Token only ever lives in this 0600 file and curl reads it with --config.
curl_conf=$work/curl.conf

log() { printf '[%(%H:%M:%S)T] %s\n' -1 "$*"; }
record() {  # step status detail
    printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}" >>"$results"
    log "$2 $1 ${3:-}"
}
fail() { record "$current" FAIL "$*"; failed=true; }
check() {  # description command...: FAIL (not abort) on error
    local what=$1; shift
    if ! "$@"; then fail "$what"; return 1; fi
}

token_conf() {
    local token
    token=$(sed -n 's/^PROXY_API_TOKEN=//p' "$ENV_FILE")
    [[ -n $token ]] || return 1
    printf 'header = "Authorization: Bearer %s"\n' "$token" >"$curl_conf"
}
api() {  # method path [json] -> sets $api_status and $api_body (never a subshell: callers read both)
    local out
    api_status=000 api_body=''
    out=$(curl --config "$curl_conf" --noproxy '*' --silent --max-time 30 -X "$1" \
        -H 'Content-Type: application/json' ${3:+--data "$3"} -w '\n%{http_code}' "$API$2") || return 1
    api_status=${out##*$'\n'}
    api_body=${out%$'\n'*}
}
wait_api() {
    for _ in $(seq 60); do
        token_conf && api GET /proxies && [[ $api_status == 200 ]] && return 0
        sleep 1
    done
    return 1
}
token_hash() { sed -n 's/^PROXY_API_TOKEN=//p' "$ENV_FILE" | sha256sum | cut -d' ' -f1; }
backup_private() {  # /var/backups/ipv6-proxy/ and every backup inside it are 0700
    local dirs=(/var/backups/ipv6-proxy/*/)
    [[ -d ${dirs[0]} ]] && [[ $(stat -c %a /var/backups/ipv6-proxy "${dirs[@]}" | sort -u) == 700 ]]
}
unit_pid() { systemctl show --property=MainPID --value "$1"; }
pids() {  # stable "unit pid" listing of the API and every proxy instance
    local unit
    printf 'ipv6-proxy-api %s\n' "$(unit_pid ipv6-proxy-api)"
    for unit in $(systemctl list-units --plain --no-legend --all 'ipv6-proxy@*.service' | awk '{print $1}' | sort); do
        printf '%s %s\n' "$unit" "$(unit_pid "$unit")"
    done
}
hashes() {
    (cd / && sha256sum etc/ipv6-proxy-api.env "${DATA_DIR#/}/state.json" \
        etc/systemd/system/ipv6-proxy-api.service etc/systemd/system/ipv6-proxy@.service \
        "${DATA_DIR#/}"/proxies/*.cfg 2>&1)
}
proxy_field() {  # port field (from GET /proxies/{port})
    api GET "/proxies/$1" || return 1
    python3 <<<"$api_body" -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$2"
}
checkout() {  # sha dir: full git checkout so install.sh records the SHA it runs from
    git clone --quiet --no-hardlinks "$repo" "$2" 2>/dev/null || git clone --quiet "$FORK_URL" "$2"
    git -C "$2" checkout --quiet --detach "$1" 2>/dev/null || {
        git -C "$2" fetch --quiet "$FORK_URL" "$1" && git -C "$2" checkout --quiet --detach FETCH_HEAD
    }
}
streams() {  # [probe options] name:scheme:port:user ...; passwords from $E2E_PASSWORD_<port>
    local spec port args=()
    while [[ $1 == --* ]]; do args+=("$1" "$2"); shift 2; done
    for spec in "$@"; do
        port=$(cut -d: -f3 <<<"$spec")
        args+=(--proxy "$spec:$(proxy_field "$port" ipv6)")
    done
    python3 "$here/e2e_probe.py" streams --seconds "$E2E_STREAM_SECONDS" "${args[@]}"
}
installer() {  # target install.sh [--ref SHA when E2E_SOURCE=fork; clients always use the checkout]
    local ref=()
    [[ $E2E_SOURCE == fork && $1 != --cli-only && $1 != --tui-only ]] && ref=(--ref "$target_sha")
    [[ -f $work/target/install.sh ]] || checkout "$target_sha" "$work/target"
    bash "$work/target/install.sh" "$@" "${ref[@]}"
}

target_sha=$(git -C "$repo" rev-parse --verify "$E2E_TARGET_REF^{commit}" 2>/dev/null || printf '%s' "$E2E_TARGET_REF")
# Fixed test credentials for a disposable VM; exported only to children that need them.
export E2E_PASSWORD_30002=base-socks-pw E2E_PASSWORD_30011=bob-pw-1 E2E_PASSWORD_30012=carol-pw-1

step_base_install() {
    if [[ -e $ENV_FILE ]]; then fail "host nao esta limpo: $ENV_FILE ja existe"; return; fi
    checkout "$BASE_SHA" "$work/base"
    check "install.sh base ${BASE_SHA:0:7}" bash "$work/base/install.sh" \
        --subnet "$E2E_SUBNET" --interface "$E2E_INTERFACE" --ipv4 "$E2E_IPV4" || return
    check 'API base respondendo' wait_api
}
step_base_proxies() {
    token_conf
    check 'CLI base cria proxy HTTP' "$CLI" create --port "$BASE_HTTP" --protocol http --yes --output json >/dev/null
    check 'CLI base cria proxy SOCKS5 com senha via stdin' "$CLI" create --port "$BASE_SOCKS" --protocol socks5 \
        --username base --password-stdin --yes --output json <<<"$E2E_PASSWORD_30002" >/dev/null || return
    api GET /proxies && printf '%s' "$api_body" >"$E2E_STATE_DIR/base-proxies.json"
    sha256sum "$ENV_FILE" | awk '{print $1}' >"$E2E_STATE_DIR/base-env.sha256"
    token_hash >"$E2E_STATE_DIR/base-token.sha256"
}
step_update() {
    # The base release has no such variable; the update keeps the env, so set it first (labeled).
    if ! grep -q '^PROXY_BLOCK_PRIVATE_DESTINATIONS=' "$ENV_FILE"; then
        printf '# vm_e2e.sh: loopback lab, SSRF block %s\nPROXY_BLOCK_PRIVATE_DESTINATIONS=%s\n' \
            "$([[ $E2E_BLOCK_PRIVATE == true ]] && echo ON || echo OFF)" "$E2E_BLOCK_PRIVATE" >>"$ENV_FILE"
    fi
    # First migration from the single-process layout: one announced interruption, confirmed by --yes.
    # --self-test creates and removes its own proxy without touching the base ones.
    local self_test=(--self-test)
    [[ $E2E_SELF_TEST == true ]] || self_test=()
    check "install.sh --update --yes ${self_test[*]} ($E2E_SOURCE $target_sha)" installer --update --yes "${self_test[@]}" || return
    [[ $E2E_SELF_TEST == true ]] || record "$current" NOT_TESTED 'E2E_SELF_TEST=false: install.sh --self-test nao executado'
    check 'API respondendo apos update' wait_api
    check 'backup 0700 criado em /var/backups/ipv6-proxy/' backup_private
}
step_migration() {
    token_conf
    check 'VERSION registra o SHA alvo' grep -qx "sha=$target_sha" "$APP_DIR/VERSION"
    check 'token preservado' [ "$(token_hash)" = "$(cat "$E2E_STATE_DIR/base-token.sha256")" ]
    check 'state.json v2 contem os proxies base com mesmo IPv6' python3 - "$DATA_DIR/state.json" \
        "$E2E_STATE_DIR/base-proxies.json" <<'EOF'
import ipaddress, json, sys
state, base = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
assert state["version"] == 2, state.get("version")
now = {r["port"]: r for r in state["proxies"]}
for b in base:
    r = now[b["port"]]
    assert ipaddress.ip_address(r["ipv6"]) == ipaddress.ip_address(b["ipv6"]), (b, r)
    assert r["protocol"] == b["protocol"] and r.get("username") == b.get("username"), (b, r)
    assert r.get("id"), r
EOF
    local port
    for port in "$BASE_HTTP" "$BASE_SOCKS"; do
        check "ipv6-proxy@$port ativo e habilitado" systemctl is-active --quiet "ipv6-proxy@$port"
        check "ipv6-proxy@$port enabled" systemctl is-enabled --quiet "ipv6-proxy@$port"
    done
    check 'API nao tem 3proxy filho (cada proxy em sua unidade)' bash -c \
        '! systemd-cgls --no-pager -u ipv6-proxy-api.service 2>/dev/null | grep -q 3proxy'
    check '3proxy.cfg monolitico removido' test ! -e "$DATA_DIR/3proxy.cfg"
}
step_idempotency() {
    token_conf
    hashes >"$work/hashes.before"; pids >"$work/pids.before"
    installer --update --yes >"$work/second.log" 2>&1 || { cat "$work/second.log"; fail '2a execucao de --update falhou'; return; }
    check '2a execucao reporta nada a atualizar' grep -q 'Nada a atualizar' "$work/second.log"
    hashes >"$work/hashes.after"; pids >"$work/pids.after"
    check 'hashes de env/state/unidades/cfg inalterados' diff -u "$work/hashes.before" "$work/hashes.after"
    check 'PIDs da API e de ipv6-proxy@* inalterados' diff -u "$work/pids.before" "$work/pids.after"
}
step_harness_a() {
    token_conf
    local pid_b pid_c id_a
    api POST /proxies "{\"port\":$PORT_B,\"protocol\":\"http\",\"username\":\"bob\",\"password\":\"$E2E_PASSWORD_30011\"}"
    check 'cria B (HTTP)' [ "$api_status" = 201 ]
    api POST /proxies "{\"port\":$PORT_C,\"protocol\":\"socks5\",\"username\":\"carol\",\"password\":\"$E2E_PASSWORD_30012\"}"
    check 'cria C (SOCKS5)' [ "$api_status" = 201 ] || return
    pid_b=$(unit_pid "ipv6-proxy@$PORT_B"); pid_c=$(unit_pid "ipv6-proxy@$PORT_C")
    check 'B com MainPID' [ "$pid_b" -gt 0 ]
    check 'C com MainPID' [ "$pid_c" -gt 0 ] || return
    streams --ready-file "$work/streams.ready" "B-http:http:$PORT_B:bob" "C-socks5:socks5:$PORT_C:carol" >"$work/streams.json" &
    local streams_pid=$!
    for _ in $(seq 50); do [[ -e $work/streams.ready ]] && break; sleep 0.1; done
    # Mutate only A (create/patch/rotate/delete) and restart the API while B/C stream.
    api POST /proxies "{\"port\":$PORT_A,\"protocol\":\"http\"}"
    check 'cria A' [ "$api_status" = 201 ]
    id_a=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$api_body")
    curl --config "$curl_conf" --noproxy '*' -s -o /dev/null -w '%{http_code}' -X PATCH -H "If-Match: \"$id_a\"" \
        -H 'Content-Type: application/json' --data '{"protocol":"socks5"}' "$API/proxies/$PORT_A" >"$work/patch.code"
    check 'PATCH A' grep -qx 200 "$work/patch.code"
    api POST "/proxies/$PORT_A/rotate"; check 'rotate A' [ "$api_status" = 200 ]
    check 'restart da API' systemctl restart ipv6-proxy-api
    check 'API volta' wait_api
    api DELETE "/proxies/$PORT_A"; check 'delete A' [ "$api_status" = 204 ]
    check 'unidade de A removida' bash -c "! systemctl is-active --quiet ipv6-proxy@$PORT_A"
    check 'streams B/C sem queda (gap < 1s, egress = IPv6 do proxy)' wait "$streams_pid"
    cat "$work/streams.json" >"$E2E_STATE_DIR/harness-a-streams.json"
    check 'PID de ipv6-proxy@B inalterado' [ "$(unit_pid "ipv6-proxy@$PORT_B")" = "$pid_b" ]
    check 'PID de ipv6-proxy@C inalterado' [ "$(unit_pid "ipv6-proxy@$PORT_C")" = "$pid_c" ]
}
step_clients() {
    token_conf
    pids >"$work/pids.before"
    check 'CLI list --output json lista B/C' bash -c "'$CLI' list --output json | python3 -c 'import json,sys; ports={p[\"port\"] for p in json.load(sys.stdin)}; assert {$PORT_B,$PORT_C} <= ports, ports'"
    hashes >"$work/hashes.before"
    check 'install.sh --cli-only' installer --cli-only
    local tui=true
    if [[ -f $work/target/ipv6-proxy-tui ]]; then
        check 'install.sh --tui-only' installer --tui-only
    else
        tui=false
    fi
    pids >"$work/pids.after"; hashes >"$work/hashes.after"
    check '--cli-only/--tui-only nao reiniciam API nem proxies' diff -u "$work/pids.before" "$work/pids.after"
    check '--cli-only/--tui-only nao tocam env/state/unidades' diff -u "$work/hashes.before" "$work/hashes.after"
    if $tui; then
        check 'TUI renderiza em pty e lista B' python3 "$here/e2e_probe.py" tui --seconds 6 --expect "$PORT_B" -- "$TUI"
    fi
    api GET /capabilities; check 'API /capabilities' [ "$api_status" = 200 ]
    check 'API status de B = running' [ "$(proxy_field "$PORT_B" status)" = running ]
    check 'API sem token = 401' [ "$(curl -s -o /dev/null -w '%{http_code}' "$API/proxies")" = 401 ]
    $tui || record "$current" NOT_TESTED "SHA $target_sha nao tem ipv6-proxy-tui: TUI nao verificada"
}
routed_traffic() {
    if [[ -z $E2E_ROUTED_IPV6_TARGET ]]; then
        record "$current" NOT_TESTED 'E2E_ROUTED_IPV6_TARGET nao definido: trafego IPv6 roteado nao verificado'
        return
    fi
    local spec port scheme user pw_var pw expected got
    for spec in "$PORT_B:http:bob" "$PORT_C:socks5h:carol"; do
        IFS=: read -r port scheme user <<<"$spec"
        pw_var=E2E_PASSWORD_$port; pw=${!pw_var}
        expected=$(proxy_field "$port" ipv6)
        # Proxy credentials go through curl's stdin config, never argv.
        got=$(printf 'proxy = "%s://%s:%s@127.0.0.1:%s"\n' "$scheme" "$user" "$pw" "$port" |
            curl --config - -6 -s --max-time 20 "$E2E_ROUTED_IPV6_TARGET") || got=''
        check "trafego $scheme via $port sai pelo IPv6 roteado $expected (obtido: ${got:-nada})" \
            python3 -c 'import ipaddress,sys; assert ipaddress.ip_address(sys.argv[1].strip())==ipaddress.ip_address(sys.argv[2])' \
            "${got:-invalid}" "$expected"
    done
}
step_traffic() { routed_traffic; }
step_snapshot() {
    token_conf
    api GET /proxies && printf '%s' "$api_body" >"$E2E_STATE_DIR/pre-reboot-proxies.json"
    pids >"$E2E_STATE_DIR/pre-reboot-pids.txt"
    hashes >"$E2E_STATE_DIR/pre-reboot-hashes.txt"
    cat /proc/sys/kernel/random/boot_id >"$E2E_STATE_DIR/pre-reboot-boot_id"
}
step_post_reboot() {
    check 'houve reboot real (boot_id mudou)' \
        [ "$(cat /proc/sys/kernel/random/boot_id)" != "$(cat "$E2E_STATE_DIR/pre-reboot-boot_id")" ] || return
    check 'API sobe sozinha apos reboot' wait_api || return
    hashes >"$work/hashes.after"
    check 'env/state/unidades/cfg identicos ao pre-reboot' diff -u "$E2E_STATE_DIR/pre-reboot-hashes.txt" "$work/hashes.after"
    api GET /proxies && printf '%s' "$api_body" >"$work/post-reboot-proxies.json"
    check 'mesmos proxies (porta, IPv6, id) apos reboot' python3 - "$E2E_STATE_DIR/pre-reboot-proxies.json" \
        "$work/post-reboot-proxies.json" <<'EOF'
import json, sys
before = {p["port"]: p for p in json.load(open(sys.argv[1]))}
after = {p["port"]: p for p in json.load(open(sys.argv[2]))}
assert before == after, (before, after)
EOF
    local port ipv6
    for port in $(python3 -c 'import json,sys; print(*[p["port"] for p in json.load(open(sys.argv[1]))])' \
            "$E2E_STATE_DIR/pre-reboot-proxies.json"); do
        check "ipv6-proxy@$port ativo apos reboot" systemctl is-active --quiet "ipv6-proxy@$port"
        ipv6=$(proxy_field "$port" ipv6)
        check "/128 $ipv6 de $port restaurado em $E2E_INTERFACE" bash -c \
            "ip -6 -o addr show dev '$E2E_INTERFACE' | grep -qi ' $ipv6/128 '"
    done
}
step_traffic_post() {
    token_conf
    check 'streams B/C locais apos reboot' streams "B-http:http:$PORT_B:bob" "C-socks5:socks5:$PORT_C:carol" \
        >"$E2E_STATE_DIR/post-reboot-streams.json"
    routed_traffic
}

overall=0
for current in "${steps[@]}"; do
    marker=$E2E_STATE_DIR/done.$current
    if [[ -e $marker ]]; then log "SKIP $current (ja concluido; apague $marker para repetir)"; continue; fi
    log "== $current"
    failed=false
    # errexit is off inside a step (|| context): check() records FAIL instead of aborting.
    "step_$current" || true
    if $failed; then overall=1; break; fi
    grep -q "^$current	NOT_TESTED" "$results" 2>/dev/null || record "$current" PASS
    touch "$marker"
done

log "Resultados: $results"
column -t -s $'\t' "$results" 2>/dev/null || cat "$results"
if ((overall == 0)) && $do_reboot; then
    log 'Reiniciando agora; depois rode: sudo tests/e2e/vm_e2e.sh --phase post-reboot'
    systemctl reboot
fi
exit "$overall"
