#!/usr/bin/env bash
# T38: E2E of `ipv6-proxy update` on a VM (run as root, disposable VM only).
#   vm_update.sh mirror BUNDLE   serve the bundle as the update source (TEST ONLY, see below)
#   vm_update.sh proxies         create 2 proxies (usernames dpm-teste-*) and a local target
#   vm_update.sh check OLD_SHA NEW_SHA
#
# The fork does not have the new commit, so the update source is mirrored locally WITHOUT
# touching the product code: git `url.insteadOf` redirects REPO_URL (ls-remote/clone) to a
# root-owned bare repo, and raw.githubusercontent.com resolves to 127.0.0.1 where a TLS
# server (certificate from a throwaway CA trusted only in this VM) serves install.sh per SHA.
set -uo pipefail

STATE=/var/lib/ipv6-proxy-update-e2e
RAW_PATH=/FalcaoOtavio/ipv6-proxy-server
ORIGIN=/srv/origin.git
mkdir -p "$STATE"
results=$STATE/results.tsv
failed=false

log() { printf '[%(%H:%M:%S)T] %s\n' -1 "$*"; }
record() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}" >>"$results"; log "$2 $1 ${3:-}"; }
check() {  # step description command...
    local step=$1 what=$2; shift 2
    if "$@"; then record "$step" PASS "$what"; else record "$step" FAIL "$what"; failed=true; fi
}
pid() { systemctl show -p MainPID --value "$1"; }
sha() { sed -n 's/^sha=//p' /opt/ipv6-proxy-server/VERSION; }
via_proxy() {  # port scheme -> body of the local target fetched through that proxy
    local port=$1 scheme=$2 auth=()
    [[ $scheme == socks5 ]] && auth=(--proxy-user "dpm-teste-s:$(cat "$STATE/pw")" --socks5-hostname "127.0.0.1:$port") || auth=(-x "http://127.0.0.1:$port")
    curl -s --max-time 20 "${auth[@]}" "http://[::1]:9000/ok"
}
responds() { [[ $(via_proxy 30021 http) == ok ]] && [[ $(via_proxy 30022 socks5) == ok ]]; }
snapshot() {  # tree hash of everything an update could touch
    { find /opt/ipv6-proxy-server /etc/systemd/system/ipv6-proxy* /etc/ipv6-proxy-api.env /etc/ipv6-proxy* -type f 2>/dev/null | sort | xargs sha256sum; } 2>/dev/null | sha256sum | cut -c1-16
}

mirror() {
    local bundle=$1 d=/etc/ipv6-proxy-test-ca
    rm -rf "$ORIGIN"; mkdir -p /srv
    git clone -q --bare "$bundle" "$ORIGIN" 2>/dev/null
    git -C "$ORIGIN" rev-parse --verify main
    printf '[url "%s"]\n\tinsteadOf = https://github.com/FalcaoOtavio/ipv6-proxy-server.git\n' "$ORIGIN" >/etc/gitconfig
    mkdir -p "$d"
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj '/CN=T38 test CA' -keyout "$d/ca.key" -out "$d/ca.crt" 2>/dev/null
    openssl req -newkey rsa:2048 -nodes -subj '/CN=raw.githubusercontent.com' -keyout "$d/srv.key" -out "$d/srv.csr" 2>/dev/null
    printf 'subjectAltName=DNS:raw.githubusercontent.com\n' >"$d/ext"
    openssl x509 -req -in "$d/srv.csr" -CA "$d/ca.crt" -CAkey "$d/ca.key" -CAcreateserial -days 2 -extfile "$d/ext" -out "$d/srv.crt" 2>/dev/null
    cp "$d/ca.crt" /usr/local/share/ca-certificates/t38-test-ca.crt && update-ca-certificates >/dev/null
    grep -q raw.githubusercontent.com /etc/hosts || echo '127.0.0.1 raw.githubusercontent.com' >>/etc/hosts
    cat >"$STATE/raw_server.py" <<PY
import http.server, re, ssl, subprocess
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        m = re.fullmatch(r"$RAW_PATH/([0-9a-f]{40})/install\.sh", self.path)
        out = subprocess.run(["git", "--git-dir=$ORIGIN", "show", f"{m[1]}:install.sh"], capture_output=True) if m else None
        if not out or out.returncode:
            self.send_error(404); return
        self.send_response(200); self.send_header("Content-Length", str(len(out.stdout))); self.end_headers(); self.wfile.write(out.stdout)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain("$d/srv.crt", "$d/srv.key")
s = http.server.ThreadingHTTPServer(("127.0.0.1", 443), H); s.socket = ctx.wrap_socket(s.socket, server_side=True); s.serve_forever()
PY
    systemd-run -q --unit=t38-raw-mirror python3 "$STATE/raw_server.py"
    sleep 2
    local code
    code=$(python3 -c "import urllib.request as u; print(u.urlopen('https://raw.githubusercontent.com$RAW_PATH/$(git -C $ORIGIN rev-parse main)/install.sh').status)")
    echo "mirror https status=$code ls-remote: $(git ls-remote https://github.com/FalcaoOtavio/ipv6-proxy-server.git refs/heads/main)"
}

proxies() {
    openssl rand -hex 8 >"$STATE/pw"; chmod 600 "$STATE/pw"
    ipv6-proxy create --port 30021 --protocol http --yes --output json >/dev/null
    cat "$STATE/pw" | ipv6-proxy create --port 30022 --protocol socks5 --username dpm-teste-s --password-stdin --yes --output json >/dev/null
    mkdir -p "$STATE/www"; printf ok >"$STATE/www/ok"
    systemd-run -q --unit=t38-target python3 -m http.server 9000 --bind :: --directory "$STATE/www"
    sleep 2
    ipv6-proxy list --output json | python3 -c 'import json,sys; [print(p["port"], p["ipv6"], p["status"]) for p in json.load(sys.stdin)]'
}

run_check() {
    local old=$1 new=$2 out
    : >"$results"
    local u1=ipv6-proxy@30021 u2=ipv6-proxy@30022
    # --- baseline
    check base_version "VERSION is the OLD sha ($old)" [ "$(sha)" = "$old" ]
    local p1=$(pid $u1) p2=$(pid $u2) papi=$(pid ipv6-proxy-api)
    echo "baseline MainPID: $u1=$p1 $u2=$p2 api=$papi"
    check base_pids "both proxies have a MainPID" bash -c "[ $p1 -gt 0 ] && [ $p2 -gt 0 ]"
    check base_respond "2 proxies respond (HTTP + SOCKS5 via local target)" responds
    ipv6-proxy list --output json >"$STATE/list-before.json"
    local snap0; snap0=$(snapshot)

    # --- update --check (non-root-safe, read only)
    out=$(ipv6-proxy update --check 2>&1); local rc=$?
    echo "$out"
    check check_exit10 "update --check exits 10" [ $rc = 10 ]
    check check_shas "update --check shows old and new SHA" bash -c "grep -q 'Instalado:  $old' <<<'$out' && grep -q 'Disponivel: $new' <<<'$out'"
    check check_readonly "update --check changed nothing" [ "$(snapshot)" = "$snap0" ]

    # --- update --self-test
    out=$(ipv6-proxy update --self-test 2>&1); rc=$?
    printf '%s\n' "$out" >"$STATE/update-selftest.log"
    echo "$out" | tail -n 25
    check update_rc0 "update --self-test exits 0" [ $rc = 0 ]
    check update_announce "announces OLD -> NEW" grep -q "Atualizando $old -> $new" <<<"$out"
    check new_version "VERSION has the NEW sha and ref=main" bash -c "[ \"$(sha)\" = $new ] && grep -qx ref=main /opt/ipv6-proxy-server/VERSION"
    check same_pids "proxies keep the same MainPID" bash -c "[ \"$(pid $u1)\" = $p1 ] && [ \"$(pid $u2)\" = $p2 ]"
    echo "after MainPID: $u1=$(pid $u1) $u2=$(pid $u2) api=$(pid ipv6-proxy-api) (api before=$papi)"
    check respond_after "2 proxies respond after update" responds
    check api_healthy "API active and lists the same proxies" bash -c "systemctl is-active --quiet ipv6-proxy-api && ipv6-proxy list --output json | cmp -s - $STATE/list-before.json"
    check no_fenced "no port fenced" bash -c "! ipv6-proxy list --output json | grep -q fenced"
    local snap1; snap1=$(snapshot)

    # --- second run
    out=$(ipv6-proxy update 2>&1); rc=$?
    echo "$out"
    check second_nothing "2nd run prints 'Nada a atualizar', exit 0" bash -c "[ $rc = 0 ] && grep -q 'Nada a atualizar' <<<'$out'"
    check second_unchanged "2nd run changed nothing (hash, PIDs)" bash -c "[ \"$(snapshot)\" = $snap1 ] && [ \"$(pid $u1)\" = $p1 ] && [ \"$(pid $u2)\" = $p2 ]"
    out=$(ipv6-proxy update --check 2>&1); rc=$?
    check second_check0 "update --check after update exits 0" [ $rc = 0 ]

    # --- invalid refs
    local bad
    for bad in nao-existe-xyz 0123456789abcdef0123456789abcdef01234567 'a;b' 'abc'; do
        out=$(ipv6-proxy update --ref "$bad" 2>&1); rc=$?
        echo "--ref $bad -> rc=$rc: $(tail -n 1 <<<"$out")"
        check "badref_$bad" "--ref '$bad' fails (rc=$rc != 0)" [ $rc != 0 ]
    done
    check badref_unchanged "invalid refs changed nothing (hash, VERSION, PIDs)" bash -c "[ \"$(snapshot)\" = $snap1 ] && [ \"$(sha)\" = $new ] && [ \"$(pid $u1)\" = $p1 ] && [ \"$(pid $u2)\" = $p2 ]"
    check respond_final "2 proxies still respond" responds
    cp /opt/ipv6-proxy-server/VERSION "$STATE/VERSION"
    log "Resultados: PASS=$(grep -c PASS "$results") FAIL=$(grep -c FAIL "$results")"
    $failed && return 1 || return 0
}

case ${1:-} in
    mirror) mirror "$2" ;;
    proxies) proxies ;;
    check) run_check "$2" "$3" ;;
    *) echo "uso: $0 mirror BUNDLE | proxies | check OLD NEW" >&2; exit 2 ;;
esac
