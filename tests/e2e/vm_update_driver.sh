#!/usr/bin/env bash
# T38 host-side driver: fresh VM from ub-clean -> install OLD (fd99a99, first rev with `update`)
# -> mirror NEW (synthetic 64116ed, never pushed) -> tests/e2e/vm_update.sh check. Run detached.
set -uo pipefail
V=/data/projects/ipv6-proxy-server-independente/.tools/vm
R=$(cd "$(dirname "$0")/../.." && pwd)
OLD=${OLD:-fd99a99d273cd568fe53d7d7f1c896acdceb70bd} NEW=$(cat "$V/upd-new.sha") port=2231 out=$V/out/ub-upd
mkdir -p "$out"
log() { printf '[%(%F %T)T] %s\n' -1 "$*" | tee -a "$out/driver.log"; }
s() { timeout "${T:-600}" "$V/vm.sh" ssh ub-upd "$port" "$@"; }
retry() { for _ in $(seq 10); do "$@" && return 0; sleep 30; done; return 1; }
"$V/vm.sh" stop ub-upd 2>/dev/null; sleep 3
cp --reflink=auto "$V/ub-clean.qcow2" "$V/ub-upd.qcow2"
"$V/vm.sh" start ub-upd "$port" && "$V/vm.sh" wait ub-upd "$port" || { log "VM boot failed"; exit 1; }
scp_() { scp -q -i "$V/vm_key" -P "$port" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$@"; }
retry scp_ "$V/upd2.bundle" "$R/tests/e2e/vm_update.sh" e2e@127.0.0.1:
s "rm -rf repo && git clone -q upd2.bundle repo 2>/dev/null; git -C repo -c advice.detachedHead=false checkout -q $OLD && git -C repo log --oneline -1" | tee -a "$out/driver.log"
log "install OLD $OLD (--self-test)"
s "sudo -n bash -c 'cd /home/e2e/repo && PROXY_BLOCK_PRIVATE_DESTINATIONS=false setsid nohup bash -c \"bash install.sh --subnet fd00:1234::/64 --interface lo --ipv4 127.0.0.1 --self-test --yes; echo \\\$? >/home/e2e/base.rc\" >/home/e2e/base.log 2>&1 </dev/null &'"
for _ in $(seq 120); do sleep 30; s 'test -e base.rc' 2>/dev/null && break; done
s 'cat base.rc; tail -n 3 base.log' | tee -a "$out/driver.log"
log "proxies + mirror + check"
s 'sudo bash vm_update.sh proxies' 2>&1 | tee "$out/proxies.log"
s 'sudo bash vm_update.sh mirror /home/e2e/upd2.bundle' 2>&1 | tee "$out/mirror.log"
T=1500 s "sudo bash vm_update.sh check $OLD $NEW" >"$out/check.log" 2>&1; echo "check rc=$?" | tee -a "$out/driver.log"
s 'sudo cat /var/lib/ipv6-proxy-update-e2e/results.tsv' >"$out/results.tsv" 2>&1
s 'sudo cat /var/lib/ipv6-proxy-update-e2e/update-selftest.log /var/lib/ipv6-proxy-update-e2e/VERSION' >"$out/update-selftest.log" 2>&1
log "DONE"
