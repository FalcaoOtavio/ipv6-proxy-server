#!/usr/bin/env bash
# SSRF block ON on a disposable VM already set up by vm_e2e.sh (proxy B on 30011 running):
# turns PROXY_BLOCK_PRIVATE_DESTINATIONS on via install.sh --update and proves both layers:
#   - systemd drop-in IPAddressDeny: a process inside ipv6-proxy@B's cgroup cannot reach [::1]
#     (kernel refuses, independent of 3proxy), while IPv4 127.0.0.1 still works;
#   - 3proxy ACL: CONNECT to [::1] through B is refused; a client over IPv4 still gets an answer.
#   sudo E2E_INSTALLER=/path/install.sh tests/e2e/vm_block_check.sh
# Prints PASS/FAIL lines; exit 1 on any FAIL.
set -uo pipefail
ENV_FILE=/etc/ipv6-proxy-api.env
DROPIN=/etc/systemd/system/ipv6-proxy@.service.d/50-block-private.conf
PORT=${E2E_BLOCK_PORT:-30011} USER_B=bob PASS_B=${E2E_PASSWORD_30011:-bob-pw-1}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
installer=${E2E_INSTALLER:-$here/../../install.sh}
[[ $EUID == 0 ]] || { echo 'Execute com sudo (VM descartavel).' >&2; exit 2; }
rc=0
res() { printf '%s\t%s\n' "$1" "$2"; [[ $1 == PASS ]] || rc=1; }

if grep -q '^PROXY_BLOCK_PRIVATE_DESTINATIONS=' "$ENV_FILE"; then
    sed -i 's/^PROXY_BLOCK_PRIVATE_DESTINATIONS=.*/PROXY_BLOCK_PRIVATE_DESTINATIONS=true/' "$ENV_FILE"
else
    echo 'PROXY_BLOCK_PRIVATE_DESTINATIONS=true' >>"$ENV_FILE"
fi
pid_before=$(systemctl show -p MainPID --value "ipv6-proxy@$PORT")
if bash "$installer" --update --yes; then res PASS 'install.sh --update com bloqueio ligado'; else res FAIL 'install.sh --update com bloqueio ligado'; fi
[[ -f $DROPIN ]] && res PASS "drop-in presente: $DROPIN" || res FAIL "drop-in ausente: $DROPIN"
deny=$(systemctl show -p IPAddressDeny --value "ipv6-proxy@$PORT")
[[ $deny == *::1/128* ]] && res PASS "IPAddressDeny carregado em ipv6-proxy@$PORT: $deny" || res FAIL "IPAddressDeny nao carregado: '$deny'"
pid_after=$(systemctl show -p MainPID --value "ipv6-proxy@$PORT")
printf 'INFO\tMainPID ipv6-proxy@%s antes=%s depois=%s (mudanca de ACL reinicia so os afetados)\n' "$PORT" "$pid_before" "$pid_after"

# Kernel layer: run a probe inside the instance's own cgroup (bypasses 3proxy entirely).
cg=/sys/fs/cgroup$(systemctl show -p ControlGroup --value "ipv6-proxy@$PORT")
probe='import socket,sys
def try_(fam,addr):
    s=socket.socket(fam,socket.SOCK_STREAM); s.settimeout(5)
    try: s.connect(addr); return "connected"
    except OSError as e: return type(e).__name__+":"+str(e.errno)
    finally: s.close()
print(try_(socket.AF_INET6,("::1",22)), try_(socket.AF_INET,("127.0.0.1",22)))'
read -r v6 v4 < <(bash -c "echo \$\$ >'$cg/cgroup.procs' && exec python3 -c '$probe'")
[[ $v6 == PermissionError* ]] && res PASS "kernel recusa [::1] no cgroup da unidade ($v6)" || res FAIL "kernel nao recusou [::1] no cgroup ($v6)"
[[ $v4 == connected ]] && res PASS "IPv4 127.0.0.1 alcancavel no mesmo cgroup ($v4)" || res FAIL "IPv4 no cgroup: $v4"

# 3proxy layer, seen by a client over IPv4.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -x "http://$USER_B:$PASS_B@127.0.0.1:$PORT" -p http://[::1]:22/ 2>/dev/null)
code=${code:-000}
[[ $code != 200 && $code != 000 ]] && res PASS "CONNECT [::1] via proxy recusado pelo 3proxy (HTTP $code; cliente IPv4 recebeu resposta)" \
    || res FAIL "CONNECT [::1] via proxy: HTTP $code"
exit "$rc"
