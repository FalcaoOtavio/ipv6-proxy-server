"""TUI flows F01-F08 with Textual Pilot against the real API (uvicorn in-process, fake backend)."""
import html
import os
import re
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from textual.widgets import DataTable, Input, Static

from proxy_api.app import create_app
from proxy_api.config import Settings
from proxy_tui.app import ProxyTui
from proxy_tui.client import Client
from test_api import FakeBackend

TOKEN = "t" * 40
SNAPSHOTS = Path(__file__).parent / "tui_snapshots"

class GatedBackend(FakeBackend):
    """FakeBackend whose replace() can be held to keep a rotate in flight."""
    def __init__(self, settings):
        super().__init__(settings)
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()

    def replace(self, old, new):
        self.entered.set()
        assert self.gate.wait(10)
        super().replace(old, new)

class Api:
    def __init__(self, tmp_path, **overrides):
        settings = Settings(subnet="2001:db8:1::/64", bind_ipv4="127.0.0.1", interface="eth0",
                            api_token=TOKEN, data_dir=tmp_path / "data", port_start=30000, port_end=30099,
                            **overrides)
        self.app = create_app(settings, GatedBackend)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "API did not start"
            time.sleep(0.02)
        self.client = Client(self.url, TOKEN)

    @property
    def backend(self):
        return self.app.state.manager.backend

    def records(self):
        return {r.port: r for r in self.app.state.manager.records}

    def stop(self):
        self.backend.gate.set()
        self.server.should_exit = True
        self.thread.join(10)

@pytest.fixture
def api(tmp_path):
    api = Api(tmp_path)
    yield api
    api.stop()

def tui(api, **kwargs):
    return ProxyTui(api.client, refresh_seconds=0, **kwargs)

async def settle(pilot, app, until=lambda: True, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        await pilot.pause()
        if app.busy is None and not any(w.is_running for w in app.workers) and until():
            return
        assert time.monotonic() < deadline, "TUI did not settle"
        await pilot.pause(0.05)

def status(app):
    return str(app.query_one("#status", Static).render())

def rows(app):
    table = app.query_one("#table", DataTable)
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]

async def fill(pilot, app, **values):
    for name, value in values.items():
        app.screen.query_one(f"#{name}").value = value
    await pilot.pause()

async def click_ok(pilot, app):
    """Button ignores clicks for 0.2 s after a press (-active): a fast second submit was dropped."""
    deadline = time.monotonic() + 2
    while app.screen.query_one("#ok").has_class("-active"):
        assert time.monotonic() < deadline, "#ok stayed pressed"
        await pilot.pause(0.02)
    await pilot.click("#ok")

UPDATE_SNAPSHOTS = os.environ.get("UPDATE_SNAPSHOTS") == "1"
SNAPSHOT_SETTLE_S = 3.0
# Also eats a trailing ':' left when a narrow column truncates the address.
IPV6_RE = re.compile(r"\b[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){2,7}", re.IGNORECASE)
SCROLLBAR_RE = re.compile("[\u2580-\u259f]")

def screen_text(svg):
    """Visible text of an exported screenshot, row by row (public export_screenshot API)."""
    rows = {}
    for x, y, text in re.findall(r'<text[^>]*? x="([\d.]+)" y="([\d.]+)"[^>]*>(.*?)</text>', svg):
        rows.setdefault(float(y), []).append((float(x), html.unescape(text).replace("\xa0", " ")))
    return ["".join(t for _, t in sorted(cells)) for _, cells in sorted(rows.items())]

def normalize(lines):
    """IPv6s are random by design and size their column: mask them, collapse padding and drop
    scrollbar glyphs (a scrollbar shows up or not depending on that random width)."""
    out = [re.sub(r" {2,}", " ", re.sub(r"pytest-\d+", "pytest-N", IPV6_RE.sub("<ipv6>", line))).rstrip()
           for line in lines]
    out = [line for line in (SCROLLBAR_RE.sub("", line).rstrip() for line in out) if line]
    return "\n".join(out).rstrip("\n") + "\n"

async def snapshot(pilot, app, name):
    """Compare the screen with tui_snapshots/<name>.txt; UPDATE_SNAPSHOTS=1 rewrites it and the SVG.

    Captures until two consecutive screens match: under load a capture can land mid-layout
    (footer missing, DataTable columns not yet widened). The stable screen is what is compared."""
    await pilot.wait_for_scheduled_animations()
    deadline = time.monotonic() + SNAPSHOT_SETTLE_S
    svg = app.export_screenshot()
    text = normalize(screen_text(svg))
    while time.monotonic() < deadline:
        await pilot.pause(0.1)
        svg, previous = app.export_screenshot(), text
        text = normalize(screen_text(svg))
        if text == previous:
            break
    reference = SNAPSHOTS / f"{name}.txt"
    if UPDATE_SNAPSHOTS:
        SNAPSHOTS.mkdir(exist_ok=True)
        reference.write_text(text)
        (SNAPSHOTS / f"{name}.svg").write_text(svg)
        return
    assert reference.exists(), f"Missing {reference}; run with UPDATE_SNAPSHOTS=1 and review the diff"
    if text != reference.read_text():
        actual = Path(tempfile.gettempdir()) / "tui-snapshot-diff" / f"{name}.txt"
        actual.parent.mkdir(exist_ok=True)
        actual.write_text(text)
        raise AssertionError(f"Screen differs from {reference}: diff -u {reference} {actual} "
                             "(UPDATE_SNAPSHOTS=1 to accept)")

SIZES = [(80, 24), (60, 20)]

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_f01_empty_list_and_reload(api, size):
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        empty = str(app.query_one("#empty", Static).render())
        assert "Nenhum proxy" in empty and "c para criar" in empty
        await snapshot(pilot, app, f"f01-empty-{size[0]}x{size[1]}")
        api.client.create({"port": 30005, "protocol": "socks5"})
        await pilot.press("a")
        await settle(pilot, app, lambda: rows(app))
        port, state, proto, user, password, ipv6 = rows(app)[0]
        assert (port, state, proto, user, password) == ("30005", "● ativo", "SOCKS5", "—", "—")
        assert ipv6 == str(api.records()[30005].ipv6)
        api.backend.records.clear()  # the process died: status must say so in text, not only color
        await pilot.press("a")
        await settle(pilot, app, lambda: rows(app)[0][1] == "✕ falhou")
        assert "fora do ar" in status(app)
        await snapshot(pilot, app, f"f01-list-{size[0]}x{size[1]}")

@pytest.mark.asyncio
async def test_f01_periodic_refresh(api):
    app = ProxyTui(api.client, refresh_seconds=0.1)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        api.client.create({"port": 30001})
        await settle(pilot, app, lambda: len(rows(app)) == 1)

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_f02_help_footer_and_escape(api, size):
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        footer = app.screen.active_bindings
        for key in ("c", "l", "e", "r", "d", "x", "s", "a", "question_mark", "q"):
            assert key in footer
        await pilot.press("question_mark")
        await pilot.pause()
        help_text = str(app.screen.query(Static).first().render())
        for word in ("criar proxy", "rotacionar", "excluir", "exportar", "recarregar", "Esc"):
            assert word in help_text
        await snapshot(pilot, app, f"f02-help-{size[0]}x{size[1]}")
        await pilot.press("escape")
        await pilot.press("c")
        await pilot.pause()
        await fill(pilot, app, port="30003")
        await pilot.press("escape")
        await settle(pilot, app)
        assert app.screen is app.screen_stack[0]
        assert api.records() == {}

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_f03_create_with_inline_validation(api, size):
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.query_one("#password", Input).password
        await fill(pilot, app, port="80", username="alice", password="s3cret")
        await click_ok(pilot, app)
        await pilot.pause()
        assert "Porta deve ser" in str(app.screen.query_one("#error", Static).render())
        await fill(pilot, app, port="30002", username="bad user")
        await click_ok(pilot, app)
        await pilot.pause()
        assert "Usuário" in str(app.screen.query_one("#error", Static).render())
        await fill(pilot, app, username="alice", protocol="socks5")
        await snapshot(pilot, app, f"f03-create-{size[0]}x{size[1]}")
        await click_ok(pilot, app)
        await settle(pilot, app, lambda: rows(app))
        assert "Proxy criado na porta 30002" in status(app)
        assert rows(app)[0][:3] == ["30002", "● ativo", "SOCKS5"]
        assert rows(app)[0][4] == "••••"
        assert api.records()[30002].password == "s3cret"

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_f04_batch_random_and_conflict(api, size):
    api.client.create({"port": 30011})
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        await pilot.press("l")
        await pilot.pause()
        await fill(pilot, app, count="0")
        await click_ok(pilot, app)
        await pilot.pause()
        assert "Quantidade" in str(app.screen.query_one("#error", Static).render())
        await fill(pilot, app, count="3", start="30010", mode="common", username="", password="")
        await click_ok(pilot, app)
        await pilot.pause()
        assert "Usuário" in str(app.screen.query_one("#error", Static).render())
        await fill(pilot, app, mode="random", username="cli", protocol="socks5")
        await snapshot(pilot, app, f"f04-batch-{size[0]}x{size[1]}")
        await click_ok(pilot, app)
        await settle(pilot, app, lambda: len(rows(app)) == 4)
        result = str(app.screen.query(Static)[1].render())
        assert "Criados: 3 (portas 30010–30013)" in result and "exporte" in result
        await snapshot(pilot, app, f"f04-result-{size[0]}x{size[1]}")
        await pilot.press("escape")
        assert {30010, 30012, 30013} <= set(api.records())
        assert all(app.passwords[api.records()[p].id] == api.records()[p].password for p in (30010, 30012, 30013))
        await pilot.press("l")
        await pilot.pause()
        await fill(pilot, app, count="200")
        await click_ok(pilot, app)
        await settle(pilot, app)
        assert "Conflito" in status(app)
        assert len(api.records()) == 4

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_f05_edit_and_port_conflict(api, size):
    api.client.create({"port": 30020, "username": "bob", "password": "old1"})
    api.client.create({"port": 30021})
    before = api.records()[30021]
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app, lambda: len(rows(app)) == 2)
        await pilot.press("e")
        await pilot.pause()
        await fill(pilot, app, port="30021")
        await click_ok(pilot, app)
        await settle(pilot, app)
        assert "Conflito" in status(app)
        assert api.records()[30020].username == "bob" and api.records()[30021] == before
        await pilot.press("e")
        await pilot.pause()
        await fill(pilot, app, port="30030", protocol="socks5", username="carol", password="new2")
        await snapshot(pilot, app, f"f05-edit-{size[0]}x{size[1]}")
        await click_ok(pilot, app)
        await settle(pilot, app, lambda: "atualizado" in status(app))
        moved = api.records()[30030]
        assert (moved.protocol, moved.username, moved.password) == ("socks5", "carol", "new2")
        assert 30020 not in api.records() and api.records()[30021] == before
        assert app.passwords[moved.id] == "new2"

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_f06_rotate_and_delete_with_typed_confirmation(api, size):
    for port in (30040, 30041):
        api.client.create({"port": port})
    other = api.records()[30041]
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app, lambda: len(rows(app)) == 2)
        old = api.records()[30040].ipv6
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot, app)
        assert api.records()[30040].ipv6 == old
        await pilot.press("r")
        await pilot.pause()
        await click_ok(pilot, app)
        await settle(pilot, app, lambda: "novo IPv6" in status(app))
        assert api.records()[30040].ipv6 != old and api.records()[30041] == other
        assert str(api.records()[30040].ipv6) in status(app)
        await pilot.press("d")
        await pilot.pause()
        await fill(pilot, app, confirm="30041")
        await click_ok(pilot, app)
        await pilot.pause()
        assert "Digite exatamente 30040" in str(app.screen.query_one("#error", Static).render())
        await snapshot(pilot, app, f"f06-delete-{size[0]}x{size[1]}")
        await pilot.press("escape")
        await settle(pilot, app)
        assert set(api.records()) == {30040, 30041}
        await pilot.press("d")
        await pilot.pause()
        await fill(pilot, app, confirm="30040")
        await click_ok(pilot, app)
        await settle(pilot, app, lambda: len(rows(app)) == 1)
        assert set(api.records()) == {30041} and api.records()[30041] == other

@pytest.mark.asyncio
async def test_f06_race_rotate_pins_target_while_selection_moves(api):
    for port in (30050, 30051):
        api.client.create({"port": port})
    a, b = api.records()[30050], api.records()[30051]
    app = tui(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app, lambda: len(rows(app)) == 2)
        api.backend.gate.clear()
        api.backend.entered.clear()
        await pilot.press("r")
        await pilot.pause()
        await click_ok(pilot, app)
        for _ in range(100):
            if api.backend.entered.is_set():
                break
            await pilot.pause(0.02)
        assert api.backend.entered.is_set(), "rotate never reached the backend"
        await pilot.press("down")  # selection moves to B while A's rotate is in flight
        await pilot.press("r")  # second action is refused while one is running
        await pilot.pause()
        assert app.screen is app.screen_stack[0] and "Aguarde" in status(app)
        assert app.selected()["port"] == 30051
        api.backend.gate.set()
        await settle(pilot, app, lambda: "novo IPv6" in status(app))
        assert "Porta 30050" in status(app)
        assert api.records()[30050].ipv6 != a.ipv6 and api.records()[30050].id == a.id
        assert api.records()[30051] == b

@pytest.mark.asyncio
async def test_f06_stale_target_is_rejected_by_if_match(api):
    api.client.create({"port": 30060})
    app = tui(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app, lambda: rows(app))
        await pilot.press("d")
        await pilot.pause()
        # Another client recycles the port before the confirmation is sent.
        api.client.delete({"port": 30060, "id": api.records()[30060].id})
        api.client.create({"port": 30060})
        heir = api.records()[30060]
        await fill(pilot, app, confirm="30060")
        await click_ok(pilot, app)
        await settle(pilot, app)
        assert "mudou desde a seleção" in status(app)
        assert api.records()[30060] == heir

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_f07_reveal_and_export(api, size, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api.client.create({"port": 30070, "username": "known", "password": "pw1"})  # set outside: unknown
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app, lambda: rows(app))
        await pilot.press("c")
        await pilot.pause()
        await fill(pilot, app, port="30071", username="dora", password="Secr3t")
        await click_ok(pilot, app)
        await pilot.press("c")
        await settle(pilot, app, lambda: len(rows(app)) == 2)
        await pilot.press("c")
        await pilot.pause()
        await fill(pilot, app, port="30072")
        await click_ok(pilot, app)
        await settle(pilot, app, lambda: len(rows(app)) == 3)
        assert [r[4] for r in rows(app)] == ["n/d", "••••", "—"]
        await pilot.press("home", "s")
        await pilot.pause()
        assert "Senha desconhecida" in status(app)
        await pilot.press("down", "s")
        await pilot.pause()
        assert rows(app)[1][4] == "Secr3t"
        await snapshot(pilot, app, f"f07-revealed-{size[0]}x{size[1]}")
        await pilot.press("s")
        await pilot.pause()
        assert rows(app)[1][4] == "••••"
        await pilot.press("x")
        await pilot.pause()
        assert "1 omitido(s): senha desconhecida" in str(app.screen.query(Static)[1].render())
        target = tmp_path / f"out-{size[0]}.txt"
        await fill(pilot, app, path=str(target))
        await snapshot(pilot, app, f"f07-export-{size[0]}x{size[1]}")
        await click_ok(pilot, app)
        await settle(pilot, app)
        assert str(target) in status(app)
        assert target.read_text() == "127.0.0.1:30071:dora:Secr3t\n127.0.0.1:30072\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        await pilot.press("x")
        await pilot.pause()
        await fill(pilot, app, path=str(target))
        await click_ok(pilot, app)
        await settle(pilot, app)
        assert "já existe" in status(app)

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_f08_api_down_and_bad_token(api, size):
    app = ProxyTui(Client("http://127.0.0.1:9", TOKEN), refresh_seconds=0)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        assert "API indisponível" in status(app) and "systemctl status ipv6-proxy-api" in status(app)
        await snapshot(pilot, app, f"f08-down-{size[0]}x{size[1]}")
        await pilot.press("r", "d", "e", "x")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
    app = ProxyTui(Client(api.url, "w" * 40), refresh_seconds=0)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        assert "Token inválido" in status(app)
    app = ProxyTui(None, "Não foi possível ler /etc/ipv6-proxy-api.env. Rode com sudo.", refresh_seconds=0)
    async with app.run_test(size=size) as pilot:
        await pilot.press("c", "l")
        await pilot.pause()
        assert "sudo" in status(app) and app.screen is app.screen_stack[0]

@pytest.mark.asyncio
async def test_f08_small_terminal_with_many_rows(api):
    api.client.batch({"count": 40, "credentials": "random"})
    app = tui(api)
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(pilot, app, lambda: len(rows(app)) == 40)
        await pilot.press("end")
        await pilot.press("l")
        await pilot.pause()
        await pilot.press("tab", "tab", "tab", "tab", "tab", "tab", "tab")
        await pilot.pause()

@pytest.mark.asyncio
async def test_a6_export_to_unknown_user_home_shows_generic_error(api, tmp_path):
    """expanduser('~nobody/x') raises RuntimeError: must not reach Textual's show_locals traceback."""
    app = tui(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        await pilot.press("c")
        await pilot.pause()
        await fill(pilot, app, port="30090", username="eve", password="TopS3cretPw")
        await click_ok(pilot, app)
        await settle(pilot, app, lambda: rows(app))
        await pilot.press("x")
        await pilot.pause()
        await fill(pilot, app, path="~usuarioinexistente/x")
        await click_ok(pilot, app)
        await settle(pilot, app)
        assert app.is_running and app.screen is app.screen_stack[0]
        assert "caminho inválido" in status(app) and "TopS3cretPw" not in status(app)

@pytest.mark.asyncio
async def test_a6_unexpected_exception_in_action_is_generic(api, monkeypatch):
    def boom(payload):
        password = payload["password"]  # noqa: F841  (a local the traceback would print)
        raise RuntimeError("leaked " + payload["password"])
    monkeypatch.setattr(api.client, "create", boom)
    app = tui(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        await pilot.press("c")
        await pilot.pause()
        await fill(pilot, app, username="eve", password="TopS3cretPw")
        await click_ok(pilot, app)
        await settle(pilot, app)
        assert app.is_running and "Erro inesperado" in status(app)
        assert "TopS3cretPw" not in status(app)

@pytest.mark.asyncio
async def test_a6_fatal_error_never_renders_locals(api):
    app = tui(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        password = "TopS3cretPw"
        try:
            raise RuntimeError("crash")
        except RuntimeError:
            app._fatal_error()
        rendered = " ".join(str(r) for r in app._exit_renderables)
        assert "Erro inesperado" in rendered and password not in rendered and "crash" not in rendered
        app._exit_renderables.clear()

@pytest.mark.asyncio
async def test_batch_limit_follows_api_and_partial_is_sent(tmp_path):
    api = Api(tmp_path, batch_max=5)
    try:
        original = api.backend.provision
        def flaky(record):
            if record.port == 30001:
                raise RuntimeError("boom")
            original(record)
        api.backend.provision = flaky
        app = tui(api)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot, app)
            await pilot.press("l")
            await pilot.pause()
            await fill(pilot, app, count="6")
            await click_ok(pilot, app)
            await settle(pilot, app)
            assert "limite da API (5" in status(app) and app.batch_max == 5
            await pilot.press("l")
            await pilot.pause()
            await fill(pilot, app, count="6")
            await click_ok(pilot, app)
            await pilot.pause()
            assert "entre 1 e 5" in str(app.screen.query_one("#error", Static).render())
            await fill(pilot, app, count="3", partial=True)
            await click_ok(pilot, app)
            await settle(pilot, app, lambda: len(rows(app)) == 2)
            result = str(app.screen.query(Static)[1].render())
            assert "Falhas: 1 — 30001" in result
            assert set(api.records()) == {30000, 30002}
    finally:
        api.stop()

@pytest.mark.asyncio
async def test_reload_is_blocked_during_mutation(api):
    api.client.create({"port": 30080})
    app = tui(api)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app, lambda: rows(app))
        api.backend.gate.clear()
        api.backend.entered.clear()
        await pilot.press("r")
        await pilot.pause()
        await click_ok(pilot, app)
        for _ in range(100):
            if api.backend.entered.is_set():
                break
            await pilot.pause(0.02)
        await pilot.press("a")
        await pilot.pause()
        assert "Aguarde" in status(app) and not any(w.group == "refresh" and w.is_running for w in app.workers)
        api.backend.gate.set()
        await settle(pilot, app, lambda: "novo IPv6" in status(app))

def visible(app):
    return "\n".join(screen_text(app.export_screenshot()))

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_f02_footer_shows_essential_keys(api, size):
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        bottom = "\n".join(visible(app).splitlines()[-2:])
        for key in ("? Ajuda", "c Criar", "l Lote", "e Editar", "r Rotacionar", "d Excluir", "x Exportar",
                    "s Senha", "a Recarregar", "q Sair"):
            assert key in bottom, f"{key!r} missing from footer at {size}: {bottom!r}"

class LiteralClient:
    """Data that looks like Rich markup/emoji codes must reach the screen verbatim."""
    IPV6 = "2001:db8:abcd:1234::1"

    def list_with_status(self):
        return [{"id": "p1", "port": 30001, "protocol": "http", "username": "[bold]u[/bold]",
                 "ipv6": self.IPV6, "ipv4": "127.0.0.1", "status": "running"}]

    def delete(self, target):
        from proxy_tui.client import ApiError
        raise ApiError("falhou [bold]x[/bold] :1234: [@click=app.quit]y[/]")

@pytest.mark.asyncio
async def test_f01_data_is_never_parsed_as_markup_or_emoji():
    app = ProxyTui(LiteralClient(), refresh_seconds=0)
    async with app.run_test(size=(100, 24)) as pilot:
        await settle(pilot, app)
        assert rows(app)[0][3] == "[bold]u[/bold]" and rows(app)[0][5] == LiteralClient.IPV6
        screen = visible(app)
        assert LiteralClient.IPV6 in screen and "[bold]u[/bold]" in screen
        await pilot.press("d")
        await pilot.pause()
        await fill(pilot, app, confirm="999")
        await click_ok(pilot, app)
        await pilot.pause()
        assert "Digite exatamente 30001" in str(app.screen.query_one("#error", Static).render())
        await fill(pilot, app, confirm="30001")
        await click_ok(pilot, app)
        await settle(pilot, app, lambda: "falhou" in status(app))
        assert "falhou [bold]x[/bold] :1234: [@click=app.quit]y[/]" in status(app)
        assert "falhou [bold]x[/bold] :1234:" in visible(app)
        assert app.is_running
