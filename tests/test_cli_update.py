import io
import subprocess
from pathlib import Path

import pytest

from test_cli import cli

SHA_OLD = "a" * 40
SHA_NEW = "b" * 40

@pytest.fixture
def env(tmp_path, monkeypatch):
    version = tmp_path / "VERSION"
    version.write_text(f"sha={SHA_OLD}\nref=release\ndate=x\n")
    monkeypatch.setattr(cli, "VERSION_FILE", version)
    state = {"ls": [], "runs": [], "downloads": [], "remote": SHA_NEW, "rc": 0}

    def ls_remote(ref):
        state["ls"].append(ref)
        return f"{state['remote']}\trefs/heads/{ref}\n"

    def download(sha, dest):
        state["downloads"].append(sha)
        assert oct(dest.parent.stat().st_mode & 0o777) == "0o700"
        dest.write_text("#!/bin/bash\n")

    def run(command, check):
        state["runs"].append(command)
        assert Path(command[1]).is_file()
        return subprocess.CompletedProcess(command, state["rc"])

    monkeypatch.setattr(cli, "ls_remote", ls_remote)
    monkeypatch.setattr(cli, "download_installer", download)
    monkeypatch.setattr(cli.subprocess, "run", run)
    state["version"] = version
    return state

def invoke(argv, uid=0):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = cli.main(["update", *argv], stdout=stdout, stderr=stderr, env_file=Path("/nonexistent"), effective_uid=uid)
    return code, stdout.getvalue(), stderr.getvalue()

def test_default_ref_comes_from_version_and_flags_are_forwarded(env):
    env["rc"] = 7
    code, out, _ = invoke(["--yes", "--self-test"])
    assert code == 7
    assert env["ls"] == ["release"]
    assert env["downloads"] == [SHA_NEW]
    assert env["runs"][0][0] == "bash"
    assert env["runs"][0][2:] == ["--update", "--ref", "release", "--yes", "--self-test"]
    assert not Path(env["runs"][0][1]).exists()  # tmpdir removed
    assert SHA_NEW in out

@pytest.mark.parametrize("content", ["", "sha=x\nref=local\n"])
def test_default_ref_falls_back_to_main(env, content):
    env["version"].write_text(content)
    invoke(["--check"], uid=1000)
    assert env["ls"] == ["main"]

def test_invalid_ref_is_rejected(env, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["update", "--ref", "x;rm -rf /"])
    assert exc.value.code == 2
    assert "--ref invalido" in capsys.readouterr().err
    assert not env["ls"]

def test_update_requires_root(env):
    code, _, err = invoke([], uid=1000)
    assert code == 3
    assert "sudo ipv6-proxy update" in err
    assert not env["runs"] and not env["downloads"]

def test_check_exit_codes_without_root(env):
    code, out, _ = invoke(["--check"], uid=1000)
    assert code == cli.UPDATE_AVAILABLE
    assert SHA_OLD in out and SHA_NEW in out
    env["remote"] = SHA_OLD
    code, out, _ = invoke(["--check"], uid=1000)
    assert code == 0
    assert "atualizada" in out
    assert not env["runs"]

def test_nothing_to_update_unless_yes(env):
    env["remote"] = SHA_OLD
    code, out, _ = invoke([])
    assert code == 0
    assert "Nada a atualizar" in out
    assert not env["runs"]
    code, _, _ = invoke(["--yes"])
    assert code == 0
    assert len(env["runs"]) == 1

def test_full_sha_skips_ls_remote(env):
    invoke(["--ref", SHA_NEW, "--check"], uid=1000)
    assert not env["ls"]

def test_unknown_ref_and_annotated_tag(env, monkeypatch):
    monkeypatch.setattr(cli, "ls_remote", lambda ref: "")
    code, _, err = invoke(["--ref", "nope", "--check"], uid=1000)
    assert code == 2 and "nao existe" in err
    monkeypatch.setattr(cli, "ls_remote", lambda ref: f"{'c' * 40}\trefs/tags/v1\n{SHA_NEW}\trefs/tags/v1^{{}}\n")
    assert cli.resolve_remote("v1") == SHA_NEW

def test_download_failure_is_reported(env, monkeypatch):
    def fail(sha, dest):
        raise cli.CliError("falha ao baixar", 1)
    monkeypatch.setattr(cli, "download_installer", fail)
    code, _, err = invoke([])
    assert code == 1 and "falha ao baixar" in err
    assert not env["runs"]

def test_download_rejects_non_script(tmp_path, monkeypatch):
    class Opener:
        def open(self, url, timeout):
            assert url == f"{cli.RAW_URL}/{SHA_NEW}/install.sh"
            return io.BytesIO(b"<html>")
    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda *h: Opener())
    with pytest.raises(cli.CliError):
        cli.download_installer(SHA_NEW, tmp_path / "install.sh")
