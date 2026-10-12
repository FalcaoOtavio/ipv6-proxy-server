"""Installer tests: install.sh is sourced as a library (IPV6_PROXY_INSTALL_LIB=1) with
every path under a temporary root and systemctl/curl/pip/ownership stubbed. Nothing
here touches the host. Real systemd/VM coverage: tests/e2e (T13/T14)."""
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "install.sh"
TOKEN = "f" * 64

STUBS = r'''
log() { printf '%s\n' "$*" >>"$T/calls"; }
systemctl() {
    log "systemctl $*"
    case "$1" in
        list-units) if [[ -f $T/active ]]; then sed 's/.*/ipv6-proxy@&.service loaded active running x/' "$T/active"; fi ;;
        show) grep -E "^${4#ipv6-proxy@}" "$T/pids" 2>/dev/null | head -1 | cut -d' ' -f2 || true
              [[ -f $T/pids ]] || echo 0 ;;
        is-active) [[ -f $T/active ]] && grep -qx "$(sed -E 's/.*@([0-9]+).*/\1/' <<<"${*: -1}")" "$T/active"
                   return ;;
        start|restart) if [[ ${*: -1} == ipv6-proxy-api.service ]]; then
                           [[ ! -f $T/api-fails ]] || { rm -f "$T/api-fails"; :; }
                           [[ ! -f $T/on-api-start ]] || bash "$T/on-api-start"
                       fi ;;
    esac
    return 0
}
curl() {
    log "curl $*"
    local url=${*: -1}
    [[ ! -f $T/api-down ]] || return 7
    if [[ " $* " == *" -o /dev/null "* ]]; then return 0; fi
    case "$url" in
        */proxies) if [[ " $* " == *" -X POST "* ]]; then echo '{"port":30099,"id":"'"$(printf 'a%.0s' {1..32})"'"}'; else echo '[]'; fi ;;
        */proxies/30099) if [[ " $* " == *" -X DELETE "* ]]; then :; else echo '{"status":"running"}'; fi ;;
    esac
}
install() {
    local args=() skip=0 a
    for a in "$@"; do
        if ((skip)); then skip=0; continue; fi
        case "$a" in -o|-g) skip=1 ;; *) args+=("$a") ;; esac
    done
    command install "${args[@]}"
}
chown() { :; }
stat() {
    if [[ $1 == -c && $2 == '%U:%G:%a' ]]; then printf 'root:root:%s\n' "$(command stat -c %a "$3")"; else command stat "$@"; fi
}
sleep() { :; }
apt-get() { log "apt-get $*"; }
date() { if [[ ${1:-} == -u ]]; then printf '%s\n' "${FAKE_DATE:-20261011T000000Z}"; else command date "$@"; fi; }
'''

def run(tmp_path, body, env=None, check=True):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    script = f'''set -Eeuo pipefail
export IPV6_PROXY_INSTALL_LIB=1 IPV6_PROXY_TEST_ROOT="$1"
T="$2"
source "{INSTALLER}"
set_paths
{STUBS}
work_dir="$2/work"
script_dir="{ROOT}"
mode=install assume_yes=false self_test=false subnet='' interface='' ipv4='' ref=''
{body}
'''
    result = subprocess.run(["bash", "-c", script, "t", str(root), str(tmp_path)], capture_output=True,
                            text=True, timeout=60, env={**os.environ, **(env or {})}, stdin=subprocess.DEVNULL)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result

def calls(tmp_path):
    path = tmp_path / "calls"
    return path.read_text().splitlines() if path.exists() else []

def mutating_systemctl(tmp_path):
    readonly = ("systemctl list-units", "systemctl show", "systemctl is-active")
    return [c for c in calls(tmp_path) if c.startswith("systemctl") and not c.startswith(readonly)]

def digest(root):
    """path -> (sha256, mode, mtime) for every file under root (venv included)."""
    out = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            info = path.stat()
            out[str(path.relative_to(root))] = (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_mode, info.st_mtime_ns)
    return out

def fake_installation(tmp_path, flavor, ports=(30000, 30001)):
    """An installed tree under root/ of the given version: main, ashford or independent."""
    root = tmp_path / "root"
    app = root / "opt/ipv6-proxy-server"
    (app / "proxy_api").mkdir(parents=True)
    if flavor == "independent":
        for source in (ROOT / "proxy_api").glob("*.py"):
            (app / "proxy_api" / source.name).write_bytes(source.read_bytes())
            (app / "proxy_api" / source.name).chmod(0o644)
        (app / "requirements.txt").write_bytes((ROOT / "requirements.txt").read_bytes())
        (app / "requirements.txt").chmod(0o644)
    else:
        marker = "SPEC = 'ashford.proxy/1'\n" if flavor == "ashford" else "app = None\n"
        (app / "proxy_api/app.py").write_text(marker)
        (app / "requirements.txt").write_text("fastapi==0.1\n")
    venv = app / ".venv/bin"
    venv.mkdir(parents=True)
    for name in ("python", "pip"):
        (venv / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp_path}/calls"\n')
        (venv / name).chmod(0o755)
    etc = root / "etc"
    (etc / "systemd/system").mkdir(parents=True)
    (etc / "ipv6-proxy-api.env").write_text(f"PROXY_API_TOKEN={TOKEN}\nPROXY_DATA_DIR=/var/lib/ipv6-proxy-api\n")
    (etc / "ipv6-proxy-api.env").chmod(0o600)
    unit = ROOT / "deploy/ipv6-proxy-api.service"
    (etc / "systemd/system/ipv6-proxy-api.service").write_text(
        unit.read_text() if flavor == "independent" else "[Service]\nKillMode=control-group\n")
    if flavor == "independent":
        (etc / "systemd/system/ipv6-proxy@.service").write_bytes((ROOT / "deploy/ipv6-proxy@.service").read_bytes())
        (etc / "systemd/system/ipv6-proxy@.service").chmod(0o644)
    state = root / "var/lib/ipv6-proxy-api"
    state.mkdir(parents=True)
    proxies = [{"port": p, "protocol": "http", "ipv6": f"2001:db8::{i + 1}"} for i, p in enumerate(ports)]
    if flavor == "main":
        (state / "proxies.json").write_text(json.dumps(proxies))
        (state / "3proxy.cfg").write_text("legacy single config\n")
    else:
        (state / "state.json").write_text(json.dumps({"version": 2, "proxies": proxies, "idempotency": {}}))
    if flavor == "independent":
        (state / "proxies").mkdir()
        for p in ports:
            (state / "proxies" / f"{p}.cfg").write_text(f"config {p}\n")
        (tmp_path / "active").write_text("".join(f"{p}\n" for p in ports))
        (tmp_path / "pids").write_text("".join(f"{p}.service {1000 + p}\n" for p in ports))
    bin_dir = root / "usr/local/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "3proxy").write_text("binary")
    (bin_dir / "3proxy").chmod(0o755)
    cli = bin_dir / "ipv6-proxy"
    cli.write_bytes((ROOT / "ipv6-proxy").read_bytes() if flavor == "independent"
                    else b"#!/bin/sh\n# ipv6-proxy-api-client\n# old\n")
    cli.chmod(0o755)
    if flavor == "independent":
        (app / "VERSION").write_text("sha=local\nref=local\ndate=x\n")
    return root

def stub_source(sha="local"):
    # The worktree is the source; its SHA comes from resolve_source unless pinned here.
    return f'resolve_source() {{ source_dir="{ROOT}"; source_ref=local; source_sha={sha}; }}\n'

# --- parsing --------------------------------------------------------------------

@pytest.mark.parametrize("argv,expected", [
    ("", "install|false|false||"),
    ("--update --yes --self-test --ref v1.2", "update|true|true|v1.2|"),
    ("--subnet 2001:db8::/64 --interface eth0 --ipv4 192.0.2.1", "install|false|false||2001:db8::/64"),
    ("--cli-only", "cli-only|false|false||"),
    ("--tui-only", "tui-only|false|false||"),
    ("--ref 0123456789abcdef0123456789abcdef01234567", "install|false|false|0123456789abcdef0123456789abcdef01234567|"),
])
def test_parse_args_accepts(tmp_path, argv, expected):
    result = run(tmp_path, f'parse_args {argv}\nprintf "%s|%s|%s|%s|%s\\n" "$mode" "$assume_yes" "$self_test" "$ref" "$subnet"')
    assert result.stdout.strip() == expected

@pytest.mark.parametrize("argv,message", [
    ("--update --cli-only", "somente um"), ("--ref", "Falta valor"), ("--ref --yes", "Falta valor"),
    ("--ref 'a;b'", "--ref invalido"), ("--ref ../x", "--ref invalido"), ("--bogus", "Opcao desconhecida"),
    ("--update --subnet 2001:db8::/64", "nao aceita --subnet"), ("--cli-only --ref main", "nao aceita --ref"),
    ("--tui-only --self-test", "nao aceita --ref nem --self-test"),
])
def test_parse_args_rejects(tmp_path, argv, message):
    result = run(tmp_path, f"parse_args {argv}", check=False)
    assert result.returncode == 1 and message in result.stderr

def test_help_documents_every_flag():
    result = subprocess.run(["bash", str(INSTALLER), "--help"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    for flag in ("--ref", "--subnet", "--interface", "--ipv4", "--update", "--yes", "--self-test", "--cli-only",
                 "--tui-only", "FalcaoOtavio/ipv6-proxy-server"):
        assert flag in result.stdout

# --- source / SHA ----------------------------------------------------------------

@pytest.fixture
def fork(tmp_path):
    """A local stand-in for the fork with a branch, a tag and two commits."""
    repo = tmp_path / "fork"
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    repo.mkdir()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    for name in ("proxy_api/app.py", "deploy/ipv6-proxy-api.service", "ipv6-proxy"):
        (repo / name).parent.mkdir(exist_ok=True)
        (repo / name).write_text("v1\n")
    git("add", "-A"); git("commit", "-qm", "one")
    first = git("rev-parse", "HEAD")
    git("tag", "v1")
    (repo / "ipv6-proxy").write_text("v2\n")
    git("commit", "-qam", "two")
    second = git("rev-parse", "HEAD")
    git("branch", "feature", first)
    return repo, first, second

def test_repo_url_is_the_fork_never_upstream():
    text = INSTALLER.read_text()
    assert "REPO_URL=https://github.com/FalcaoOtavio/ipv6-proxy-server.git" in text
    assert "github.com/3proxy/3proxy.git" in text  # only for the pinned 3proxy build
    assert "omni" not in text.lower() and "github.com/Temporalitas" not in text

@pytest.mark.parametrize("which", ["main", "branch", "tag", "sha", "short-sha"])
def test_ref_resolves_to_one_sha_and_checks_it_out(tmp_path, fork, which):
    repo, first, second = fork
    ref = {"main": "", "branch": "feature", "tag": "v1", "sha": first, "short-sha": first[:10]}[which]
    expected = second if which == "main" else first
    body = (f'REPO_URL="{repo}"\nscript_dir="{tmp_path}"\nref="{ref}"\nresolve_source\n'
            'printf "%s %s %s\\n" "$source_sha" "$source_ref" "$(git -C "$source_dir" rev-parse HEAD)"\n'
            'cat "$source_dir/ipv6-proxy"')
    out = run(tmp_path, body).stdout.split("\n")
    sha, source_ref, head = out[0].split()
    assert sha == head == expected and source_ref == (ref or "main")
    assert out[1] == ("v2" if which == "main" else "v1")

def test_unknown_ref_fails_clearly(tmp_path, fork):
    repo, _, _ = fork
    result = run(tmp_path, f'REPO_URL="{repo}"\nscript_dir="{tmp_path}"\nref=nope\nresolve_source', check=False)
    assert result.returncode == 1 and "--ref nope nao existe" in result.stderr

def test_local_checkout_records_its_sha(tmp_path, fork):
    repo, _, second = fork
    out = run(tmp_path, f'script_dir="{repo}"\nresolve_source\nprintf "%s %s %s\\n" "$source_dir" "$source_ref" "$source_sha"')
    assert out.stdout.split() == [str(repo), "local", second]
    (repo / "ipv6-proxy").write_text("edited\n")
    out = run(tmp_path, f'script_dir="{repo}"\nresolve_source\nprintf "%s\\n" "$source_sha"')
    assert out.stdout.strip() == second + "-dirty"

def test_version_file_records_sha_ref_date(tmp_path):
    fake_installation(tmp_path, "independent")
    run(tmp_path, stub_source("0" * 40) + "resolve_source\nstage_release\nplan_changes\napply_release")
    version = (tmp_path / "root/opt/ipv6-proxy-server/VERSION").read_text().splitlines()
    assert version == ["sha=" + "0" * 40, "ref=local", "date=20261011T000000Z"]

# --- detection / validation -----------------------------------------------------

@pytest.mark.parametrize("flavor", ["main", "ashford", "independent"])
def test_detect_version(tmp_path, flavor):
    fake_installation(tmp_path, flavor)
    assert run(tmp_path, "detect_version").stdout.strip() == flavor

def test_detect_none_and_partial(tmp_path):
    assert run(tmp_path, "detect_version").stdout.strip() == "none"
    (tmp_path / "root/usr/local/bin").mkdir(parents=True)
    (tmp_path / "root/usr/local/bin/3proxy").write_text("x")
    assert run(tmp_path, "detect_version").stdout.strip() == "partial"

def test_existing_installation_is_preserved_without_update(tmp_path):
    root = fake_installation(tmp_path, "ashford")
    before = digest(root)
    body = stub_source() + "check_platform() { :; }\nlegacy_cron_present() { return 1; }\nmain"
    result = run(tmp_path, body, check=False)
    assert result.returncode == 1 and "Use --update para migrar" in result.stderr
    assert digest(root) == before and mutating_systemctl(tmp_path) == []

def test_partial_installation_is_refused_untouched(tmp_path):
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/ipv6-proxy-api.env").write_text("existing")
    result = run(tmp_path, "check_platform() { :; }\nlegacy_cron_present() { return 1; }\nmain", check=False)
    assert result.returncode == 1 and "Instalacao preservada" in result.stderr
    assert (root / "etc/ipv6-proxy-api.env").read_text() == "existing"

def test_foreign_3proxy_detected_but_never_killed(tmp_path):
    proc = tmp_path / "root/proc"
    cgroups = {"100": "0::/system.slice/system-ipv6\\x2dproxy.slice/ipv6-proxy@30000.service",
               "200": "0::/system.slice/ipv6-proxy-api.service",
               "300": "0::/user.slice/user-1000.slice/session-3.scope",
               "400": "0::/system.slice/other.service"}
    for pid, cgroup in cgroups.items():
        (proc / pid).mkdir(parents=True)
        (proc / pid / "comm").write_text("3proxy\n" if pid != "400" else "nginx\n")
        (proc / pid / "cgroup").write_text(cgroup + "\n")
    assert run(tmp_path, "foreign_3proxy_pids").stdout.split() == ["300"]
    assert run(tmp_path, "api_cgroup_3proxy_count").stdout.strip() == "1"
    result = run(tmp_path, "kill() { log kill; }\ncheck_foreign_3proxy", check=False)
    assert result.returncode == 1 and "PIDs: 300" in result.stderr and "Nada foi encerrado" in result.stderr
    assert "kill" not in calls(tmp_path)

@pytest.mark.parametrize("legacy", [False, True])
def test_legacy_cron_guard_consumes_large_crontab(tmp_path, legacy):
    # A short-circuiting grep can make the producer fail with SIGPIPE; with pipefail
    # that used to turn a positive legacy match into permission to install.
    crontab = tmp_path / "crontab"
    crontab.write_text(("*/5 * * * * /root/proxy-startup.sh\n" if legacy else "") + "# unrelated entry\n" * 100000)
    body = (f'crontab() {{ "{sys.executable}" -c \'import signal,sys; signal.signal(signal.SIGPIPE, signal.SIG_DFL); '
            f'sys.stdout.write(open(sys.argv[1]).read())\' "{crontab}"; }}\n'
            'if legacy_cron_present; then echo LEGACY; else echo CLEAN; fi')
    assert run(tmp_path, body).stdout.strip() == ("LEGACY" if legacy else "CLEAN")

@pytest.mark.parametrize("subnet,ipv4,message", [
    ("2001:db8::/64", "192.0.2.10", None),
    ("nope", "192.0.2.10", "subnet IPv6 invalida"),
    ("2001:db8::/127", "192.0.2.10", "prefixo <= 126"),
    ("fe80::/64", "192.0.2.10", "subnet roteada"),
    ("ff00::/64", "192.0.2.10", "subnet roteada"),
    ("2001:db8::/64", "0.0.0.0", "endereco especifico"),
    ("2001:db8::/64", "198.51.100.1", "nao existe na interface"),
    ("2001:db8::/64", "x", "IPv4 invalido"),
])
def test_network_validation_is_actionable(tmp_path, subnet, ipv4, message):
    addresses = json.dumps([{"addr_info": [{"local": "192.0.2.10", "family": "inet"}]}])
    result = run(tmp_path, f"validate_network '{subnet}' '{ipv4}' '{addresses}'", check=False)
    if message is None:
        assert result.returncode == 0
    else:
        assert result.returncode == 1 and message in result.stderr

def test_env_paths_must_match_the_instance_unit(tmp_path):
    root = fake_installation(tmp_path, "independent")
    env = root / "etc/ipv6-proxy-api.env"
    env.write_text(env.read_text() + "PROXY_DATA_DIR=/srv/proxies\n")
    result = run(tmp_path, "check_env_compatible", check=False)
    assert result.returncode == 1 and "PROXY_DATA_DIR=/srv/proxies nao suportado" in result.stderr

# --- idempotency ----------------------------------------------------------------

def test_rerun_without_changes_touches_nothing(tmp_path):
    root = fake_installation(tmp_path, "independent")
    first = run(tmp_path, stub_source() + "resolve_source\nstage_release\nplan_changes\napply_release\n"
                "anything_changes && echo CHANGED || echo SAME")
    before = digest(root)
    second = run(tmp_path, stub_source() + "resolve_source\nstage_release\nplan_changes\n"
                 "anything_changes && echo CHANGED || echo SAME\napply_release")
    assert second.stdout.strip() == "SAME", first.stdout
    assert digest(root) == before  # same bytes, modes and mtimes: token, env, state, units

def test_update_without_changes_restarts_nothing(tmp_path):
    root = fake_installation(tmp_path, "independent")
    run(tmp_path, stub_source() + "resolve_source\nstage_release\nplan_changes\napply_release")
    before = digest(root)
    (tmp_path / "calls").unlink(missing_ok=True)
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent")
    assert "Nada a atualizar" in result.stdout
    assert mutating_systemctl(tmp_path) == [] and digest(root) == before
    assert not (root / "var/backups").exists()

def test_update_restarts_only_the_api(tmp_path):
    root = fake_installation(tmp_path, "independent")
    (root / "opt/ipv6-proxy-server/proxy_api/app.py").write_text("old\n")
    env_before = (root / "etc/ipv6-proxy-api.env").read_bytes()
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent")
    assert "Atualizacao concluida" in result.stdout, result.stderr
    assert mutating_systemctl(tmp_path) == ["systemctl restart ipv6-proxy-api.service"]
    assert not any("ipv6-proxy@" in c for c in mutating_systemctl(tmp_path))
    assert (root / "opt/ipv6-proxy-server/proxy_api/app.py").read_bytes() == (ROOT / "proxy_api/app.py").read_bytes()
    assert (root / "etc/ipv6-proxy-api.env").read_bytes() == env_before
    backup = next((root / "var/backups").iterdir())
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert (backup / "app/proxy_api/app.py").read_text() == "old\n"

def test_update_rolls_back_when_an_unchanged_proxy_restarts(tmp_path):
    root = fake_installation(tmp_path, "independent")
    (root / "opt/ipv6-proxy-server/proxy_api/app.py").write_text("old\n")
    # The new API (wrongly) restarts 30001 although its config did not change.
    (tmp_path / "on-api-start").write_text(f'sed -i "s/^30001.service .*/30001.service 9999/" "{tmp_path}/pids"\n')
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent", check=False)
    assert result.returncode == 1 and "reiniciados durante a atualizacao: 30001" in result.stderr
    assert (root / "opt/ipv6-proxy-server/proxy_api/app.py").read_text() == "old\n"
    assert "Rollback concluido" in result.stderr

def test_update_rolls_back_when_api_does_not_come_back(tmp_path):
    root = fake_installation(tmp_path, "independent")
    (root / "opt/ipv6-proxy-server/proxy_api/app.py").write_text("old\n")
    before = digest(root / "var/lib/ipv6-proxy-api")
    (tmp_path / "on-api-start").write_text(f'touch "{tmp_path}/api-down"\n')
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent", check=False)
    assert result.returncode == 1 and "healthcheck apos atualizacao" in result.stderr
    assert (root / "opt/ipv6-proxy-server/proxy_api/app.py").read_text() == "old\n"
    assert digest(root / "var/lib/ipv6-proxy-api") == before
    assert not any(c.startswith("systemctl disable") for c in calls(tmp_path))  # proxies untouched

# --- migration ------------------------------------------------------------------

@pytest.mark.parametrize("flavor", ["main", "ashford"])
def test_migration_needs_confirmation(tmp_path, flavor):
    root = fake_installation(tmp_path, flavor)
    before = digest(root)
    result = run(tmp_path, stub_source() + f"mode=update\nupdate_install {flavor}", check=False)
    assert result.returncode == 1 and "--yes" in result.stderr
    assert "interrompidos UMA vez" in result.stdout and "Os 2 proxies" in result.stdout
    assert digest(root) == before and mutating_systemctl(tmp_path) == []

@pytest.mark.parametrize("flavor", ["main", "ashford"])
def test_migration_with_yes_is_one_announced_interruption(tmp_path, flavor):
    root = fake_installation(tmp_path, flavor)
    # The new API reconciles: one ipv6-proxy@<port> per persisted proxy.
    (tmp_path / "on-api-start").write_text(f'printf "30000\\n30001\\n" > "{tmp_path}/active"\n')
    result = run(tmp_path, stub_source() + f"mode=update\nassume_yes=true\nupdate_install {flavor}")
    assert "Confirmado por --yes" in result.stdout and "Atualizacao concluida" in result.stdout
    assert mutating_systemctl(tmp_path) == ["systemctl daemon-reload", "systemctl stop ipv6-proxy-api.service",
                                            "systemctl start ipv6-proxy-api.service"]
    units = root / "etc/systemd/system"
    assert (units / "ipv6-proxy@.service").read_bytes() == (ROOT / "deploy/ipv6-proxy@.service").read_bytes()
    assert (units / "ipv6-proxy-api.service").read_bytes() == (ROOT / "deploy/ipv6-proxy-api.service").read_bytes()
    assert (root / "opt/ipv6-proxy-server/proxy_api/instance_address.py").exists()  # used by ExecStartPre
    assert not (root / "var/lib/ipv6-proxy-api/3proxy.cfg").exists()
    if flavor == "main":  # proxies.json is migrated by the API, never by the installer
        assert (root / "var/lib/ipv6-proxy-api/proxies.json").exists()

def test_failed_migration_restores_backup_and_removes_new_instances(tmp_path):
    root = fake_installation(tmp_path, "ashford")
    before = {k: v[:2] for k, v in digest(root).items()}
    (tmp_path / "on-api-start").write_text(f'printf "30000\\n" > "{tmp_path}/active"\n')  # 30001 never comes up
    result = run(tmp_path, stub_source() + "mode=update\nassume_yes=true\nupdate_install ashford", check=False)
    assert result.returncode == 1 and "ipv6-proxy@30001.service nao esta ativo" in result.stderr
    assert "systemctl disable --now ipv6-proxy@30000.service" in calls(tmp_path)
    after = {k: v[:2] for k, v in digest(root).items() if not k.startswith("var/backups/")}
    assert after == before  # app, env, units, state, cli restored byte for byte

def test_backup_restore_roundtrip(tmp_path):
    root = fake_installation(tmp_path, "independent")
    before = {k: v[:2] for k, v in digest(root).items()}
    run(tmp_path, 'backup_installation\nprintf "%s" "$backup_dir" > "$T/backup"\n'
        'echo changed > "$env_file"; rm -rf "$app_dir/proxy_api"; echo new > "$template_file"; echo x > "$tui_file"\n'
        'restore_installation "$(cat "$T/backup")"')
    after = {k: v[:2] for k, v in digest(root).items() if not k.startswith("var/backups/")}
    assert after == before
    assert not (root / "usr/local/bin/ipv6-proxy-tui").exists()  # absent before => absent after

# --- clients only -----------------------------------------------------------------

@pytest.mark.parametrize("which", ["cli", "tui"])
def test_client_only_never_touches_services(tmp_path, which):
    root = fake_installation(tmp_path, "independent")
    source = tmp_path / "src"
    source.mkdir()
    (source / "ipv6-proxy").write_text("#!/bin/sh\n# ipv6-proxy-api-client\n# new\n")
    (source / "ipv6-proxy-tui").write_text("#!/bin/sh\n# tui\n")
    (source / "proxy_tui").mkdir()
    (source / "proxy_tui/__init__.py").write_text("")
    (source / "requirements.txt").write_text("textual==8.2.8\n")
    protected = {k: v for k, v in digest(root).items()
                 if not k.startswith(("usr/local/bin/ipv6-proxy", "opt/ipv6-proxy-server/proxy_tui"))}
    result = run(tmp_path, f'script_dir="{source}"\ninstall_clients_only {which}')
    assert mutating_systemctl(tmp_path) == [] and not any(c.startswith("systemctl") for c in calls(tmp_path))
    after = {k: v for k, v in digest(root).items()
             if not k.startswith(("usr/local/bin/ipv6-proxy", "opt/ipv6-proxy-server/proxy_tui"))}
    assert after == protected
    target = root / "usr/local/bin" / ("ipv6-proxy" if which == "cli" else "ipv6-proxy-tui")
    assert target.read_bytes() == (source / target.name).read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    if which == "tui":
        assert "pip install --quiet --disable-pip-version-check textual==8.2.8" in calls(tmp_path)
        assert (root / "opt/ipv6-proxy-server/proxy_tui/__init__.py").exists()
    assert result.stdout.strip().endswith("sudo ipv6-proxy --help" if which == "cli" else "sudo ipv6-proxy-tui")

@pytest.mark.parametrize("case", ["unknown-cli", "symlink", "mode", "missing-api", "standalone"])
def test_cli_only_refuses_unsafe_or_missing(tmp_path, case):
    root = fake_installation(tmp_path, "independent")
    source = tmp_path / "src"
    source.mkdir()
    if case != "standalone":
        (source / "ipv6-proxy").write_text("# ipv6-proxy-api-client\n# new\n")
    cli = root / "usr/local/bin/ipv6-proxy"
    if case == "unknown-cli":
        cli.write_text("# unrelated tool\n")
    elif case == "symlink":
        cli.unlink()
        cli.symlink_to(root / "etc/ipv6-proxy-api.env")
    elif case == "mode":
        cli.chmod(0o644)
    elif case == "missing-api":
        (root / "etc/systemd/system/ipv6-proxy-api.service").unlink()
    before = digest(root)
    result = run(tmp_path, f'script_dir="{source}"\ninstall_clients_only cli', check=False)
    assert result.returncode == 1 and not result.stdout
    assert digest(root) == before and not list((root / "usr/local/bin").glob(".ipv6-proxy.*"))
    assert calls(tmp_path) == []

# --- token hygiene / self-test ------------------------------------------------------

def test_token_never_in_argv_and_self_test_cleans_up(tmp_path):
    fake_installation(tmp_path, "independent")
    result = run(tmp_path, "write_curl_config\nwait_api\nrun_self_test\nhealthcheck")
    assert "Self-test OK" in result.stdout
    log = (tmp_path / "calls").read_text()
    assert TOKEN not in log and TOKEN not in result.stdout + result.stderr
    assert "-X DELETE -H If-Match: " + "a" * 32 in log and "/proxies/30099" in log
    posted = [c for c in calls(tmp_path) if "-X POST" in c]
    assert len(posted) == 1 and "--data @" in posted[0]  # password from a file, never argv
    assert (tmp_path / "work/curl.conf").read_text().count(TOKEN) == 1

def test_restarted_unchanged_flags_only_same_config_new_pid(tmp_path):
    (tmp_path / "before").write_text("30000 100 aaa\n30001 101 bbb\n30002 102 ccc\n")
    (tmp_path / "after").write_text("30000 100 aaa\n30001 201 bbb\n30002 202 ddd\n")
    out = run(tmp_path, 'restarted_unchanged "$T/before" "$T/after"').stdout.split()
    assert out == ["30001"]  # 30002 restarted because its config changed: allowed

def test_shellcheck_clean():
    candidates = [Path("/data/projects/ipv6-proxy-server-independente/.tools/sc-venv/bin/shellcheck")]
    found = next((c for c in candidates if c.exists()), None) or next(
        (Path(p) / "shellcheck" for p in os.environ.get("PATH", "").split(os.pathsep) if (Path(p) / "shellcheck").exists()), None)
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)
    if found is None:
        pytest.skip("shellcheck not installed (pip install shellcheck-py)")
    result = subprocess.run([str(found), str(INSTALLER)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout

@pytest.mark.parametrize("marker_index", range(4))
@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("kind", ["file", "directory", "broken-symlink"])
def test_legacy_guard_fails_before_side_effects(tmp_path, generated, kind, marker_index):
    # Extract ONLY the guard; never run the legacy manager or generated startup.
    script = (ROOT / "ipv6-proxy-server.sh").read_text()
    if generated:
        script = script[script.index('  cat > $startup_script_path <<-EOF'):]
    start = script.index("for api_marker in ")
    if generated:
        assert start < script.index("proxyserver_process_pids=")
    else:
        assert start < script.index("if [ \"$EUID\" -ne 0 ]")
    fragment = script[start:script.index("\ndone", start) + len("\ndone")]
    fragment = fragment.replace('\\$', '$')
    markers = fragment.splitlines()[0].removeprefix("for api_marker in ").removesuffix("; do").split()
    assert len(markers) == 4
    for index, marker in enumerate(markers):
        fragment = fragment.replace(marker, str(tmp_path / f"marker-{index}"))
    selected = tmp_path / f"marker-{marker_index}"
    if kind == "file":
        selected.touch()
    elif kind == "directory":
        selected.mkdir()
    else:
        selected.symlink_to(tmp_path / "absent")
    result = subprocess.run(["/bin/bash", "-c", fragment + '\nprintf "SIDE EFFECT"'],
                            capture_output=True, text=True, timeout=5,
                            env={**os.environ, "PATH": "/nonexistent"})
    assert result.returncode == 1
    assert not result.stdout
    assert "legacy manager disabled" in result.stderr
