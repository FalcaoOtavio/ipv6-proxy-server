#!/usr/bin/env bash
# Toggle PROXY_BLOCK_PRIVATE_DESTINATIONS via install.sh --update on a disposable VM already set up by
# vm_e2e.sh, and record per-proxy evidence: cfg deny/UDPASSOC lines, MainPID before/after (only the
# affected proxies restart), unit IPAddressDeny. With the block ON, vm_block_check.sh then proves
# [::1] is refused (kernel + 3proxy) and IPv4 clients still work.
#   sudo E2E_INSTALLER=/path/install.sh tests/e2e/vm_block_toggle.sh on|off
set -uo pipefail
ENV_FILE=/etc/ipv6-proxy-api.env
CFG_DIR=/var/lib/ipv6-proxy-api/proxies
DROPIN=/etc/systemd/system/ipv6-proxy@.service.d/50-block-private.conf
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
installer=${E2E_INSTALLER:-$here/../../install.sh}
case ${1:-} in on) want=true ;; off) want=false ;; *) echo 'uso: vm_block_toggle.sh on|off' >&2; exit 2 ;; esac
[[ $EUID == 0 ]] || { echo 'Execute com sudo (VM descartavel).' >&2; exit 2; }
rc=0
res() { printf '%s\t%s\n' "$1" "$2"; [[ $1 == PASS ]] || rc=1; }
ports() { systemctl list-units --plain --no-legend --all 'ipv6-proxy@*.service' | sed -nE 's/^ipv6-proxy@([0-9]+)\.service.*/\1/p' | sort -n; }
snapshot() {  # port MainPID deny_lines udpassoc IPAddressDeny
    local p
    for p in $(ports); do
        printf '%s %s %s %s %s\n' "$p" "$(systemctl show -p MainPID --value "ipv6-proxy@$p")" \
            "$(grep -c '^deny \* \* [^*]' "$CFG_DIR/$p.cfg")" "$(grep -c 'UDPASSOC' "$CFG_DIR/$p.cfg")" \
            "$(systemctl show -p IPAddressDeny --value "ipv6-proxy@$p" | tr ' ' ',')"
    done
}

before=$(snapshot)
printf 'INFO\tantes (porta MainPID deny UDPASSOC IPAddressDeny):\n%s\n' "$before"
if grep -q '^PROXY_BLOCK_PRIVATE_DESTINATIONS=' "$ENV_FILE"; then
    sed -i "s/^PROXY_BLOCK_PRIVATE_DESTINATIONS=.*/PROXY_BLOCK_PRIVATE_DESTINATIONS=$want/" "$ENV_FILE"
else
    echo "PROXY_BLOCK_PRIVATE_DESTINATIONS=$want" >>"$ENV_FILE"
fi
if bash "$installer" --update --yes; then res PASS "install.sh --update (bloqueio $1)"; else res FAIL "install.sh --update (bloqueio $1)"; fi
after=$(snapshot)
printf 'INFO\tdepois:\n%s\n' "$after"

while read -r port pid deny udp _; do
    old=$(awk -v p="$port" '$1 == p' <<<"$before")
    old_pid=$(cut -d' ' -f2 <<<"$old") old_deny=$(cut -d' ' -f3 <<<"$old")
    changed=$([[ $old_deny != "$deny" ]] && echo yes || echo no)
    if [[ $want == true ]]; then
        [[ $deny -ge 1 && $udp -ge 1 ]] && res PASS "$port.cfg com deny privado + UDPASSOC" || res FAIL "$port.cfg sem deny ($deny/$udp)"
    else
        [[ $deny == 0 && $udp == 0 ]] && res PASS "$port.cfg sem regras deny" || res FAIL "$port.cfg ainda com deny ($deny/$udp)"
    fi
    if [[ $changed == yes ]]; then
        [[ $pid != "$old_pid" ]] && res PASS "ipv6-proxy@$port reiniciado (cfg mudou): $old_pid -> $pid" \
            || res FAIL "ipv6-proxy@$port NAO reiniciado apesar da cfg nova (MainPID $pid)"
    else
        [[ $pid == "$old_pid" ]] && res PASS "ipv6-proxy@$port intocado (cfg igual): $pid" \
            || res FAIL "ipv6-proxy@$port reiniciado sem mudanca de cfg: $old_pid -> $pid"
    fi
    deny_now=$(systemctl show -p IPAddressDeny --value "ipv6-proxy@$port")
    if [[ $want == true ]]; then
        [[ $deny_now == *::1/128* ]] && res PASS "ipv6-proxy@$port IPAddressDeny ativo" || res FAIL "ipv6-proxy@$port sem IPAddressDeny: '$deny_now'"
    else
        [[ -z $deny_now ]] && res PASS "ipv6-proxy@$port sem IPAddressDeny" || res FAIL "ipv6-proxy@$port ainda com IPAddressDeny: $deny_now"
    fi
done <<<"$after"
if [[ $want == true ]]; then
    [[ -f $DROPIN ]] && res PASS "drop-in presente" || res FAIL "drop-in ausente"
else
    [[ ! -e $DROPIN ]] && res PASS "drop-in removido" || res FAIL "drop-in ainda presente"
fi
exit "$rc"
