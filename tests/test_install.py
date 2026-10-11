"""Safe installer regressions: execute only isolated, side-effect-free fragments."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("checkout", [False, True])
def test_standalone_installer_clones_source_but_checkout_uses_local_files(tmp_path, checkout):
    script = (ROOT / "install.sh").read_text()
    # Test exactly the source-selection branch, never the actual installer.
    start = script.index('if [[ -f "$script_dir/proxy_api/app.py"')
    fragment = script[start:script.index('\ngit clone --depth 1 --branch 0.9.4', start)]
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    if checkout:
        for name in ("proxy_api/app.py", "deploy/ipv6-proxy-api.service", "ipv6-proxy"):
            path = source / name
            path.parent.mkdir(exist_ok=True)
            path.touch()
    runner = '''set -eu
script_dir=$1
work_dir=$2
git() { printf '%s\\n' "$*" > "$work_dir/clone-call"; }
''' + fragment + '\nprintf "%s\\n" "$source_dir"\n'
    result = subprocess.run(["bash", "-c", runner, "test", str(source), str(work)],
                            capture_output=True, text=True, check=True, timeout=5)
    assert result.stdout.strip() == str(source if checkout else work / "app")
    assert (work / "clone-call").exists() != checkout
    if not checkout:
        assert (work / "clone-call").read_text().startswith(
            "clone --depth 1 --branch main https://github.com/FalcaoOtavio/ipv6-proxy-server.git ")
    prefix = script[:start]
    assert prefix.index('if [[ $cli_only == true ]]') < prefix.index('[[ -f "$script_dir/ipv6-proxy" ]]')
    assert '--cli-only requer checkout completo' in prefix


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


@pytest.mark.parametrize("legacy", [False, True])
def test_legacy_cron_guard_consumes_large_crontab(tmp_path, legacy):
    # A short-circuiting grep can make the producer fail with SIGPIPE. With
    # pipefail that used to turn a positive legacy match into permission to install.
    import sys
    script = (ROOT / "install.sh").read_text()
    start = script.index('if [[ -d /root/proxyserver ]]')
    fragment = script[start:script.index('\nif pgrep', start)]
    fragment = fragment.replace('/root/proxyserver', str(tmp_path / 'absent'))
    crontab = tmp_path / 'crontab'
    crontab.write_text(('*/5 * * * * /root/proxy-startup.sh\n' if legacy else '')
                       + '# unrelated entry\n' * 100000)
    runner = '''set -euo pipefail
die() { printf '%s\\n' "$*" >&2; exit 1; }
python=$1
cron_file=$2
crontab() { "$python" -c 'import signal,sys; signal.signal(signal.SIGPIPE, signal.SIG_DFL); sys.stdout.write(open(sys.argv[1]).read())' "$cron_file"; }
''' + fragment + '\nprintf "INSTALL CONTINUES\\n"\n'
    result = subprocess.run(['bash', '-c', runner, 'test', sys.executable, str(crontab)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == (1 if legacy else 0)
    assert ('INSTALL CONTINUES' in result.stdout) != legacy
    assert ('Instalacao legada detectada' in result.stderr) == legacy


@pytest.mark.parametrize("marker_index", range(6))
@pytest.mark.parametrize("kind", ["file", "directory", "broken-symlink"])
def test_normal_install_preserves_existing_markers(tmp_path, marker_index, kind):
    script = (ROOT / "install.sh").read_text()
    start = script.index('for path in "$app_dir"')
    fragment = script[start:script.index('\ndone', start) + len('\ndone')]
    markers = [tmp_path / f'marker-{i}' for i in range(6)]
    for source, destination in zip(
        ['$app_dir', '$env_file', '$unit_file', '/var/lib/ipv6-proxy-api',
         '/usr/local/bin/3proxy', '$cli_file'], markers
    ):
        fragment = fragment.replace(source, str(destination))
    marker = markers[marker_index]
    if kind == 'file':
        marker.write_text('existing installation')
    elif kind == 'directory':
        marker.mkdir()
    else:
        marker.symlink_to(tmp_path / 'absent')
    runner = '''set -euo pipefail
die() { printf '%s\\n' "$*" >&2; exit 1; }
''' + fragment + '\nprintf "INSTALL CONTINUES\\n"\n'
    result = subprocess.run(['bash', '-c', runner], capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and not result.stdout
    assert 'Instalacao preservada' in result.stderr
    assert marker.is_symlink() if kind == 'broken-symlink' else marker.exists()
    if kind == 'file':
        assert marker.read_text() == 'existing installation'


@pytest.mark.parametrize('case', ['add', 'update', 'standalone', 'missing-api',
                                  'network-flag', 'unknown-cli', 'symlink', 'mode'])
def test_cli_only_branch_preserves_deployment(tmp_path, case):
    # Run the real branch with all paths redirected into tmp_path. Mock only
    # GNU metadata/ownership operations, so this test needs neither Linux nor root.
    script = (ROOT / 'install.sh').read_text()
    start = script.index('if [[ $cli_only == true ]]')
    fragment = script[start:script.index('\n[[ -f /etc/os-release ]]', start)]
    fragment = fragment.replace('/usr/local/bin/.ipv6-proxy.XXXXXXXX',
                                str(tmp_path / '.ipv6-proxy.XXXXXXXX'))
    source = tmp_path / 'source'
    source.mkdir()
    if case != 'standalone':
        (source / 'ipv6-proxy').write_text('# ipv6-proxy-api-client\n# new version\n')
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'server.py').write_text('existing server')
    env = tmp_path / 'api.env'
    unit = tmp_path / 'api.service'
    env.write_text('existing token')
    env.chmod(0o600)
    unit.write_text('existing unit')
    if case == 'missing-api':
        unit.unlink()
    destination = tmp_path / 'ipv6-proxy'
    if case == 'symlink':
        destination.symlink_to(env)
    elif case in {'update', 'unknown-cli', 'mode'}:
        destination.write_text('# unrelated CLI\n' if case == 'unknown-cli'
                               else '# ipv6-proxy-api-client\n# old version\n')
        destination.chmod(0o644 if case == 'mode' else 0o755)

    def snapshot():
        return {str(p): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in (env, unit, app / 'server.py') if p.exists()}

    before = snapshot()
    original_cli = destination.read_bytes() if destination.exists() else None
    runner = '''set -euo pipefail
umask 077
die() { printf '%s\\n' "$*" >&2; exit 1; }
script_dir=$1
app_dir=$2
env_file=$3
unit_file=$4
cli_file=$5
cli_only=true
subnet=$6
interface=''
ipv4=''
metadata=$7
# Only ownership is simulated. Installation and replacement affect temporary files.
stat() { printf '%s\\n' "$metadata"; }
install() {
    [[ "$1 $2 $3 $4 $5 $6" == '-o root -g root -m 0755' ]] || exit 90
    cp "$7" "$8"
    chmod 0755 "$8"
}
''' + fragment + '\nprintf "UNEXPECTED FALLTHROUGH\\n"\n'
    result = subprocess.run(
        ['bash', '-c', runner, 'test', str(source), str(app), str(env), str(unit),
         str(destination), '2001:db8::/64' if case == 'network-flag' else '',
         'root:root:644' if case == 'mode' else 'root:root:755'],
        capture_output=True, text=True, timeout=5,
    )
    assert snapshot() == before
    assert not list(tmp_path.glob('.ipv6-proxy.*'))
    if case in {'add', 'update'}:
        assert result.returncode == 0, result.stderr
        assert destination.read_bytes() == (source / 'ipv6-proxy').read_bytes()
        assert destination.stat().st_mode & 0o777 == 0o755
        assert 'CLI instalado' in result.stdout
    else:
        assert result.returncode == 1 and not result.stdout
        assert (destination.read_bytes() if destination.exists() else None) == original_cli
        if case == 'symlink':
            assert destination.is_symlink()
