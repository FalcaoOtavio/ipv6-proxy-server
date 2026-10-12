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
from ipaddress import IPv4Address, IPv6Address, IPv6Network
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1]))
from proxy_api.backend import render_config  # noqa: E402

ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "install.sh"
DROPIN = ROOT / "deploy/ipv6-proxy@.service.d/50-block-private.conf"
TOKEN = "f" * 64
SUBNET = "2001:db8::/64"

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
        daemon-reload) [[ ! -f $T/reload-fails ]] || return 1 ;;
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
    # Legacy GET /proxies answers 503 while any proxy is fenced; ?include=status does not.
    if [[ -f $T/legacy-503 && $url == */proxies && " $* " == *" --fail "* ]]; then return 22; fi
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
mode=install assume_yes=false self_test=false subnet='' interface='' ipv4='' ref='' keep_backups=5
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
        for name in ("requirements.txt", "requirements.lock"):
            (app / name).write_bytes((ROOT / name).read_bytes())
            (app / name).chmod(0o644)
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
    (etc / "ipv6-proxy-api.env").write_text(
        f"PROXY_API_TOKEN={TOKEN}\nPROXY_BIND_IPV4=192.0.2.10\nPROXY_IPV6_SUBNET={SUBNET}\n"
        "PROXY_DATA_DIR=/var/lib/ipv6-proxy-api\n")
    (etc / "ipv6-proxy-api.env").chmod(0o600)
    unit = ROOT / "deploy/ipv6-proxy-api.service"
    (etc / "systemd/system/ipv6-proxy-api.service").write_text(
        unit.read_text() if flavor == "independent" else "[Service]\nKillMode=control-group\n")
    if flavor == "independent":
        (etc / "systemd/system/ipv6-proxy@.service").write_bytes((ROOT / "deploy/ipv6-proxy@.service").read_bytes())
        (etc / "systemd/system/ipv6-proxy@.service").chmod(0o644)
        dropin = etc / "systemd/system/ipv6-proxy@.service.d/50-block-private.conf"
        dropin.parent.mkdir()
        dropin.write_bytes(DROPIN.read_bytes())
        dropin.chmod(0o644)
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
        for item in proxies:  # what the installed API rendered: an update with the same code changes nothing
            (state / "proxies" / f"{item['port']}.cfg").write_text(rendered(item))
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

def rendered(item):
    record = SimpleNamespace(username=None, password=None, **{**item, "ipv6": IPv6Address(item["ipv6"])})
    settings = SimpleNamespace(bind_ipv4=IPv4Address("192.0.2.10"), subnet=IPv6Network(SUBNET),
                               block_private_destinations=True)
    return render_config(settings, record)

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
    ("--update --keep-backups 2", "update|false|false||"),
    ("--update --api-timeout 600", "update|false|false||"),
])
def test_parse_args_accepts(tmp_path, argv, expected):
    result = run(tmp_path, f'parse_args {argv}\nprintf "%s|%s|%s|%s|%s\\n" "$mode" "$assume_yes" "$self_test" "$ref" "$subnet"')
    assert result.stdout.strip() == expected

@pytest.mark.parametrize("argv,message", [
    ("--update --cli-only", "somente um"), ("--ref", "Falta valor"), ("--ref --yes", "Falta valor"),
    ("--ref 'a;b'", "--ref invalido"), ("--ref ../x", "--ref invalido"), ("--bogus", "Opcao desconhecida"),
    ("--update --subnet 2001:db8::/64", "nao aceita --subnet"), ("--cli-only --ref main", "nao aceita --ref"),
    ("--tui-only --self-test", "nao aceita --ref nem --self-test"),
    ("--keep-backups 0", "--keep-backups invalido"), ("--keep-backups x", "--keep-backups invalido"),
    ("--api-timeout 9", "--api-timeout invalido"), ("--api-timeout 3601", "--api-timeout invalido"),
    ("--api-timeout 0180", "--api-timeout invalido"), ("--api-timeout x", "--api-timeout invalido"),
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
    backup = next((root / "var/backups/ipv6-proxy").iterdir())
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert (backup / "app/proxy_api/app.py").read_text() == "old\n"

def test_changed_lock_reinstalls_with_hashes_and_is_backed_up(tmp_path):
    root = fake_installation(tmp_path, "independent")
    app = root / "opt/ipv6-proxy-server"
    (app / "requirements.lock").write_text("old-lock\n")
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent")
    assert "Atualizacao concluida" in result.stdout, result.stderr
    pip = [c for c in calls(tmp_path) if c.startswith("pip ")]
    assert len(pip) == 1 and pip[0].startswith("pip install --quiet --disable-pip-version-check --require-hashes -r ")
    assert pip[0].endswith("/stage/app/requirements.lock"), pip
    assert (app / "requirements.lock").read_bytes() == (ROOT / "requirements.lock").read_bytes()
    backup = next((root / "var/backups/ipv6-proxy").iterdir())
    assert (backup / "app/requirements.lock").read_text() == "old-lock\n"
    (tmp_path / "calls").unlink()
    run(tmp_path, stub_source() + "mode=update\nupdate_install independent")
    assert not any(c.startswith("pip ") for c in calls(tmp_path))  # same lock: no pip

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
    (source / "requirements.lock").write_text("textual==8.2.8 --hash=sha256:00\n")
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
        assert (f"pip install --quiet --disable-pip-version-check --require-hashes -r {source}/requirements.lock"
                in calls(tmp_path))
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


# --- T17: transaction, rollback and review fixes ------------------------------------

def old_app(root):
    (root / "opt/ipv6-proxy-server/proxy_api/app.py").write_text("old\n")

def test_unrelated_fenced_proxy_does_not_roll_back_update(tmp_path):
    # A1: 30001 is down before the update; plain GET /proxies is 503, include=status is 200.
    root = fake_installation(tmp_path, "independent")
    old_app(root)
    (tmp_path / "active").write_text("30000\n")
    (tmp_path / "legacy-503").touch()
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent")
    assert "Atualizacao concluida" in result.stdout, result.stderr
    assert any("/proxies?include=status" in c for c in calls(tmp_path))
    assert not any(c.endswith("/proxies") and "--fail" in c for c in calls(tmp_path) if c.startswith("curl"))

def test_die_inside_transaction_rolls_back(tmp_path):
    # A2: die => exit never fires ERR; the EXIT trap must still restore the backup.
    root = fake_installation(tmp_path, "independent")
    old_app(root)
    result = run(tmp_path, stub_source() + "reload_if_units_changed() { die 'injected'; }\n"
                 "mode=update\nupdate_install independent", check=False)
    assert result.returncode == 1 and "Erro: injected" in result.stderr
    assert "Restaurando" in result.stderr and "Rollback concluido" in result.stderr
    assert (root / "opt/ipv6-proxy-server/proxy_api/app.py").read_text() == "old\n"

def test_set_e_error_inside_transaction_rolls_back_with_line(tmp_path):
    root = fake_installation(tmp_path, "independent")
    old_app(root)
    result = run(tmp_path, stub_source() + "reload_if_units_changed() { false; }\n"
                 "mode=update\nupdate_install independent", check=False)
    assert result.returncode == 1 and "na linha" in result.stderr
    assert (root / "opt/ipv6-proxy-server/proxy_api/app.py").read_text() == "old\n"

def test_rollback_keeps_going_and_always_starts_old_api(tmp_path):
    # A3: an incomplete backup and a failing daemon-reload never skip 'systemctl start'.
    root = fake_installation(tmp_path, "independent")
    old_app(root)
    (tmp_path / "on-api-start").write_text(f'touch "{tmp_path}/api-down" "{tmp_path}/reload-fails"\n'
                                           f'rm -f "$(ls -d {root}/var/backups/ipv6-proxy/*)/cli"\n')
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent", check=False)
    assert result.returncode == 1
    assert "FALHOU: systemctl daemon-reload" in result.stderr and "FALHOU: restaurar arquivos" in result.stderr
    mutations = mutating_systemctl(tmp_path)
    assert mutations[-1] == "systemctl start ipv6-proxy-api.service"
    assert (root / "opt/ipv6-proxy-server/proxy_api/app.py").read_text() == "old\n"  # other items restored

def test_inactive_proxy_started_later_is_not_a_restart(tmp_path):
    # A4: MainPID 0 before (inactive/failed) => a new PID after is not "restarted".
    (tmp_path / "before").write_text("30000 100 aaa\n30001 0 bbb\n30002  ccc\n")
    (tmp_path / "after").write_text("30000 100 aaa\n30001 201 bbb\n30002 202 ccc\n")
    assert run(tmp_path, 'restarted_unchanged "$T/before" "$T/after"').stdout.split() == []

def test_inactive_proxy_reconciled_does_not_roll_back(tmp_path):
    root = fake_installation(tmp_path, "independent")
    old_app(root)
    (tmp_path / "active").write_text("30000\n")
    (tmp_path / "pids").write_text("30000.service 31000\n30001.service 0\n")
    (tmp_path / "on-api-start").write_text(f'printf "30000\\n30001\\n" > "{tmp_path}/active"; '
                                           f'sed -i "s/^30001.service .*/30001.service 9999/" "{tmp_path}/pids"\n')
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent")
    assert "Atualizacao concluida" in result.stdout, result.stderr

def test_api_deadline_grows_with_proxy_count(tmp_path):
    # A5/T30: 180s base (slow hosts took ~170s to start the API) + 5s per persisted proxy.
    fake_installation(tmp_path, "independent", ports=tuple(range(30000, 30100)))
    (tmp_path / "api-down").touch()
    result = run(tmp_path, 'api_deadline\nwrite_curl_config\nwait_api || echo DOWN')
    assert result.stdout.split() == ["680", "DOWN"]
    assert sum(c.startswith("curl") for c in calls(tmp_path)) == 680

def test_api_timeout_overrides_base_deadline(tmp_path):
    fake_installation(tmp_path, "independent")
    result = run(tmp_path, 'parse_args --update --api-timeout 600\napi_deadline')
    assert result.stdout.split() == ["610"]

@pytest.mark.parametrize("line,expected", [
    (None, "60"), ("PROXY_READY_TIMEOUT=120", "150"), ("PROXY_READY_TIMEOUT=300", "330"),
    ("PROXY_READY_TIMEOUT=0", "60"), ("PROXY_READY_TIMEOUT=999", "60"), ("PROXY_READY_TIMEOUT=$(id)", "60"),
])
def test_api_max_time_follows_ready_timeout(tmp_path, line, expected):
    # T30: self-test/healthcheck curl must outlast one proxy's readiness wait inside the API.
    root = fake_installation(tmp_path, "independent")
    if line:
        with (root / "etc/ipv6-proxy-api.env").open("a") as env:
            env.write(line + "\n")
    run(tmp_path, 'write_curl_config\nrun_self_test')
    posts = [c for c in calls(tmp_path) if c.startswith("curl") and "-X POST" in c]
    assert posts and all(f"--max-time {expected} " in c for c in posts)

def changed_render(root, port=30000):
    (root / f"var/lib/ipv6-proxy-api/proxies/{port}.cfg").write_text("config rendered by the old release\n")

def test_update_changing_rendered_config_needs_confirmation(tmp_path):
    # M3: announce exactly the proxies that will restart; nothing changes without --yes.
    root = fake_installation(tmp_path, "independent")
    old_app(root)
    changed_render(root)
    before = digest(root)
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent", check=False)
    assert result.returncode == 1 and "--yes" in result.stderr
    assert "1 proxies serao reiniciados uma vez (um por vez)" in result.stdout and "portas: 30000" in result.stdout
    assert digest(root) == before and mutating_systemctl(tmp_path) == []

def test_update_changing_rendered_config_with_yes(tmp_path):
    root = fake_installation(tmp_path, "independent")
    old_app(root)
    changed_render(root)
    # The new API restarts 30000 (config changed) and keeps 30001.
    cfg = root / "var/lib/ipv6-proxy-api/proxies/30000.cfg"
    (tmp_path / "on-api-start").write_text(f'printf "new\\n" > "{cfg}"; '
                                           f'sed -i "s/^30000.service .*/30000.service 7777/" "{tmp_path}/pids"\n')
    result = run(tmp_path, stub_source() + "mode=update\nassume_yes=true\nupdate_install independent")
    assert "Confirmado por --yes" in result.stdout and "Atualizacao concluida" in result.stdout
    assert mutating_systemctl(tmp_path) == ["systemctl restart ipv6-proxy-api.service"]  # never collective

def test_failed_config_update_reverts_only_restarted_proxies(tmp_path):
    root = fake_installation(tmp_path, "independent")
    old_app(root)
    changed_render(root)
    cfg = root / "var/lib/ipv6-proxy-api/proxies"
    # One-shot: the new API rewrites 30000.cfg, then 30000 fails to come up.
    (tmp_path / "on-api-start").write_text(
        f'printf "new\\n" > "{cfg}/30000.cfg"; printf "30001\\n" > "{tmp_path}/active"; rm -f "$0"\n')
    result = run(tmp_path, stub_source() + "mode=update\nassume_yes=true\nupdate_install independent", check=False)
    assert result.returncode == 1 and "ipv6-proxy@30000.service nao esta ativo" in result.stderr
    restarts = [c for c in mutating_systemctl(tmp_path) if c.startswith("systemctl restart ipv6-proxy@")]
    assert restarts == ["systemctl restart ipv6-proxy@30000.service"]
    assert (cfg / "30000.cfg").read_text() == "config rendered by the old release\n"

def test_rollback_never_runs_in_a_subshell(tmp_path):
    # M4: the EXIT handler of a $(...)/( ) subshell must not restore anything.
    fake_installation(tmp_path, "independent")
    result = run(tmp_path, 'rollback() { echo ROLLBACK; }\nbegin_tx update\n'
                 'v=$(false; echo x)\n( set +e; (exit 4); on_exit ) || true\nfor p in $(false); do :; done\n'
                 'end_tx\necho DONE')
    assert "ROLLBACK" not in result.stdout and result.stdout.strip().endswith("DONE")

def test_backups_keep_var_backups_mode_and_are_pruned(tmp_path):
    # M5: /var/backups keeps its mode; our dir is 0700; only the newest N are kept.
    root = fake_installation(tmp_path, "independent")
    shared = root / "var/backups"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    (shared / "dpkg.status.0").write_text("not ours\n")
    ours = shared / "ipv6-proxy"
    ours.mkdir(mode=0o700)
    for day in range(1, 8):
        (ours / f"2026010{day}T000000Z-1").mkdir()
    (ours / "keep-me").mkdir()  # not a backup name: never pruned
    run(tmp_path, "keep_backups=3\nbackup_installation\nprune_backups")
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755 and (shared / "dpkg.status.0").exists()
    assert stat.S_IMODE(ours.stat().st_mode) == 0o700
    assert sorted(p.name for p in ours.iterdir()) == [
        "20260106T000000Z-1", "20260107T000000Z-1", "20261011T000000Z-" + next(
            p.name.split("-")[-1] for p in ours.iterdir() if p.name.startswith("20261011")), "keep-me"]

FRESH = r"""
ask_network() { subnet=2001:db8::/64 interface=eth0 ipv4=192.0.2.10; }
check_api_port_free() { :; }
make() { :; }
git() {
    if [[ $1 == clone ]]; then mkdir -p "${*: -1}/bin"; echo bin > "${*: -1}/bin/3proxy"; return; fi
    if [[ $* == *rev-parse* ]]; then echo "$THREEPROXY_SHA"; return; fi
    command git "$@"
}
python3() {
    if [[ $1 == -m && $2 == venv ]]; then
        mkdir -p "$3/bin"; printf '#!/bin/sh\n[ ! -f "%s/pip-fails" ]\n' "$T" > "$3/bin/pip"
        cp "$3/bin/pip" "$3/bin/python"; chmod 755 "$3/bin/pip" "$3/bin/python"; return
    fi
    command python3 "$@"
}
"""

def test_fresh_install_succeeds(tmp_path):
    result = run(tmp_path, stub_source() + FRESH + 'trap on_exit EXIT\nfresh_install\necho VERSION=$(detect_version)')
    assert "Instalacao concluida" in result.stdout and "VERSION=independent" in result.stdout
    assert stat.S_IMODE((tmp_path / "root/etc/ipv6-proxy-api.env").stat().st_mode) == 0o600

@pytest.mark.parametrize("failure", ["pip-fails", "api-down"])
def test_failed_fresh_install_leaves_nothing_behind(tmp_path, failure):
    # M6: a failed clean install removes what it created; the rerun starts from 'none'.
    (tmp_path / failure).touch()
    result = run(tmp_path, stub_source() + FRESH + 'trap on_exit EXIT\nfresh_install', check=False)
    assert result.returncode == 1 and "Desfazendo a instalacao incompleta" in result.stderr
    assert "execute o mesmo comando novamente" in result.stderr
    assert run(tmp_path, "detect_version").stdout.strip() == "none"
    # The API unit exists only once apply_release got that far; then it is disabled too.
    assert ("systemctl disable --now ipv6-proxy-api.service" in calls(tmp_path)) == (failure == "api-down")

def test_version_unknown_warns(tmp_path):
    checkout = tmp_path / "checkout"
    for name in ("proxy_api/app.py", "deploy/ipv6-proxy-api.service", "ipv6-proxy"):
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text("")
    result = run(tmp_path, f'script_dir="{checkout}"\nresolve_source\necho "sha=$source_sha"',
                 env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert result.stdout.strip() == "sha=unknown" and "Aviso: nao foi possivel ler o SHA" in result.stderr

def test_vm_e2e_shellcheck_clean():
    found = Path("/data/projects/ipv6-proxy-server-independente/.tools/sc-venv/bin/shellcheck")
    if not found.exists():
        pytest.skip("shellcheck not installed")
    result = subprocess.run([str(found), str(ROOT / "tests/e2e/vm_e2e.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout

# --- IPAddressDeny drop-in follows PROXY_BLOCK_PRIVATE_DESTINATIONS (ADR 0007) ------------

def dropin_path(root):
    return root / "etc/systemd/system/ipv6-proxy@.service.d/50-block-private.conf"

def synced_installation(tmp_path):
    """Independent install already at this release (dropin on, as the default block is)."""
    root = fake_installation(tmp_path, "independent")
    run(tmp_path, stub_source() + "resolve_source\nstage_release\nplan_changes\napply_release")
    (tmp_path / "calls").unlink(missing_ok=True)
    return root

def set_block(root, value):
    env = root / "etc/ipv6-proxy-api.env"
    env.write_text(env.read_text() + f"PROXY_BLOCK_PRIVATE_DESTINATIONS={value}\n")

def test_dropin_denies_internal_ipv6_never_ipv4():
    import configparser
    from ipaddress import ip_network
    from proxy_api.backend import PRIVATE_DESTINATIONS
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.read(DROPIN)
    denied = {ip_network(net) for net in unit["Service"]["IPAddressDeny"].split()}
    assert denied == {ip_network(net) for net in PRIVATE_DESTINATIONS.split(",")}  # same set as the 3proxy ACL
    assert all(net.version == 6 for net in denied)  # clients reach -i<bind_ipv4> over IPv4
    assert "IPAddressDeny" not in (ROOT / "deploy/ipv6-proxy@.service").read_text()  # never static

@pytest.mark.parametrize("block,present_before,present_after", [
    (None, False, True),     # default on: missing drop-in is installed
    ("true", False, True),
    ("false", True, False),  # off: drop-in removed
    (" No ", True, False),
])
def test_update_syncs_dropin_without_restarting_proxies(tmp_path, block, present_before, present_after):
    root = synced_installation(tmp_path)
    if block is not None:
        set_block(root, block)
    if not present_before:
        dropin_path(root).unlink()
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent")
    assert "Atualizacao concluida" in result.stdout, result.stderr
    assert dropin_path(root).exists() == present_after
    if present_after:
        assert dropin_path(root).read_bytes() == DROPIN.read_bytes()
        assert stat.S_IMODE(dropin_path(root).stat().st_mode) == 0o644
    # Only daemon-reload: each instance picks it up on its next start, none restarts now.
    assert mutating_systemctl(tmp_path) == ["systemctl daemon-reload"]
    assert "50-block-private.conf) mudou" in result.stdout
    backup = next((root / "var/backups/ipv6-proxy").iterdir())
    assert (backup / "dropin").exists() == present_before and (backup / "dropin.absent").exists() != present_before

@pytest.mark.parametrize("block,present", [("true", True), ("false", False)])
def test_update_with_dropin_in_sync_is_a_noop(tmp_path, block, present):
    root = synced_installation(tmp_path)
    set_block(root, block)
    if not present:
        dropin_path(root).unlink()
    result = run(tmp_path, stub_source() + "mode=update\nupdate_install independent")
    assert "Nada a atualizar" in result.stdout and mutating_systemctl(tmp_path) == []

@pytest.mark.parametrize("present_before", [True, False])
def test_failed_update_restores_dropin_state(tmp_path, present_before):
    root = fake_installation(tmp_path, "independent")
    old_app(root)
    set_block(root, "true" if not present_before else "false")
    if not present_before:
        dropin_path(root).unlink()
    (tmp_path / "on-api-start").write_text(f'touch "{tmp_path}/api-down"\n')
    result = run(tmp_path, stub_source() + "mode=update\nassume_yes=true\nupdate_install independent", check=False)
    assert result.returncode == 1 and "ok: restaurar arquivos do backup" in result.stderr, result.stderr
    assert dropin_path(root).exists() == present_before

@pytest.mark.parametrize("block,present", [(None, True), ("false", False)])
def test_fresh_install_writes_block_and_dropin(tmp_path, block, present):
    env = {} if block is None else {"PROXY_BLOCK_PRIVATE_DESTINATIONS": block}
    result = run(tmp_path, stub_source() + FRESH + "trap on_exit EXIT\nfresh_install", env=env)
    assert "Instalacao concluida" in result.stdout
    root = tmp_path / "root"
    written = (root / "etc/ipv6-proxy-api.env").read_text()
    assert f"PROXY_BLOCK_PRIVATE_DESTINATIONS={'true' if present else 'false'}" in written
    assert dropin_path(root).exists() == present
