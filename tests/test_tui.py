"""TUI flows F01-F08 with Textual Pilot against the real API (uvicorn in-process, fake backend)."""
import socket
import stat
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from textual.widgets import DataTable, Input, Static

from proxy_api.app import create_app
from proxy_api.config import Settings
from proxy_tui.app import ProxyTui, export_lines
from proxy_tui.client import ApiError, Client, validate_url
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
    def __init__(self, tmp_path):
        settings = Settings(subnet="2001:db8:1::/64", bind_ipv4="127.0.0.1", interface="eth0",
                            api_token=TOKEN, data_dir=tmp_path / "data", port_start=30000, port_end=30099)
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

def snapshot(app, name):
    SNAPSHOTS.mkdir(exist_ok=True)
    app.save_screenshot(filename=f"{name}.svg", path=str(SNAPSHOTS))

SIZES = [(80, 24), (60, 20)]
pytestmark = pytest.mark.asyncio

@pytest.mark.parametrize("size", SIZES)
async def test_f01_empty_list_and_reload(api, size):
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        empty = str(app.query_one("#empty", Static).render())
        assert "Nenhum proxy" in empty and "c para criar" in empty
        snapshot(app, f"f01-empty-{size[0]}x{size[1]}")
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
        snapshot(app, f"f01-list-{size[0]}x{size[1]}")

async def test_f01_periodic_refresh(api):
    app = ProxyTui(api.client, refresh_seconds=0.1)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        api.client.create({"port": 30001})
        await settle(pilot, app, lambda: len(rows(app)) == 1)

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
        snapshot(app, f"f02-help-{size[0]}x{size[1]}")
        await pilot.press("escape")
        await pilot.press("c")
        await pilot.pause()
        await fill(pilot, app, port="30003")
        await pilot.press("escape")
        await settle(pilot, app)
        assert app.screen is app.screen_stack[0]
        assert api.records() == {}

@pytest.mark.parametrize("size", SIZES)
async def test_f03_create_with_inline_validation(api, size):
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.query_one("#password", Input).password
        await fill(pilot, app, port="80", username="alice", password="s3cret")
        await pilot.click("#ok")
        await pilot.pause()
        assert "Porta deve ser" in str(app.screen.query_one("#error", Static).render())
        await fill(pilot, app, port="30002", username="bad user")
        await pilot.click("#ok")
        await pilot.pause()
        assert "Usuário" in str(app.screen.query_one("#error", Static).render())
        await fill(pilot, app, username="alice", protocol="socks5")
        snapshot(app, f"f03-create-{size[0]}x{size[1]}")
        await pilot.click("#ok")
        await settle(pilot, app, lambda: rows(app))
        assert "Proxy criado na porta 30002" in status(app)
        assert rows(app)[0][:3] == ["30002", "● ativo", "SOCKS5"]
        assert rows(app)[0][4] == "••••"
        assert api.records()[30002].password == "s3cret"

@pytest.mark.parametrize("size", SIZES)
async def test_f04_batch_random_and_conflict(api, size):
    api.client.create({"port": 30011})
    app = tui(api)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        await pilot.press("l")
        await pilot.pause()
        await fill(pilot, app, count="0")
        await pilot.click("#ok")
        await pilot.pause()
        assert "Quantidade" in str(app.screen.query_one("#error", Static).render())
        await fill(pilot, app, count="3", start="30010", mode="common", username="", password="")
        await pilot.click("#ok")
        await pilot.pause()
        assert "Usuário" in str(app.screen.query_one("#error", Static).render())
        await fill(pilot, app, mode="random", username="cli", protocol="socks5")
        snapshot(app, f"f04-batch-{size[0]}x{size[1]}")
        await pilot.click("#ok")
        await settle(pilot, app, lambda: len(rows(app)) == 4)
        result = str(app.screen.query(Static)[1].render())
        assert "Criados: 3 (portas 30010–30013)" in result and "exporte" in result
        snapshot(app, f"f04-result-{size[0]}x{size[1]}")
        await pilot.press("escape")
        assert {30010, 30012, 30013} <= set(api.records())
        assert all(app.passwords[api.records()[p].id] == api.records()[p].password for p in (30010, 30012, 30013))
        await pilot.press("l")
        await pilot.pause()
        await fill(pilot, app, count="200")
        await pilot.click("#ok")
        await settle(pilot, app)
        assert "Conflito" in status(app)
        assert len(api.records()) == 4

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
        await pilot.click("#ok")
        await settle(pilot, app)
        assert "Conflito" in status(app)
        assert api.records()[30020].username == "bob" and api.records()[30021] == before
        await pilot.press("e")
        await pilot.pause()
        await fill(pilot, app, port="30030", protocol="socks5", username="carol", password="new2")
        snapshot(app, f"f05-edit-{size[0]}x{size[1]}")
        await pilot.click("#ok")
        await settle(pilot, app, lambda: "atualizado" in status(app))
        moved = api.records()[30030]
        assert (moved.protocol, moved.username, moved.password) == ("socks5", "carol", "new2")
        assert 30020 not in api.records() and api.records()[30021] == before
        assert app.passwords[moved.id] == "new2"

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
        await pilot.click("#ok")
        await settle(pilot, app, lambda: "novo IPv6" in status(app))
        assert api.records()[30040].ipv6 != old and api.records()[30041] == other
        assert str(api.records()[30040].ipv6) in status(app)
        await pilot.press("d")
        await pilot.pause()
        await fill(pilot, app, confirm="30041")
        await pilot.click("#ok")
        await pilot.pause()
        assert "Digite exatamente 30040" in str(app.screen.query_one("#error", Static).render())
        snapshot(app, f"f06-delete-{size[0]}x{size[1]}")
        await pilot.press("escape")
        await settle(pilot, app)
        assert set(api.records()) == {30040, 30041}
        await pilot.press("d")
        await pilot.pause()
        await fill(pilot, app, confirm="30040")
        await pilot.click("#ok")
        await settle(pilot, app, lambda: len(rows(app)) == 1)
        assert set(api.records()) == {30041} and api.records()[30041] == other

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
        await pilot.click("#ok")
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
        await pilot.click("#ok")
        await settle(pilot, app)
        assert "mudou desde a seleção" in status(app)
        assert api.records()[30060] == heir

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
        await pilot.click("#ok")
        await pilot.press("c")
        await settle(pilot, app, lambda: len(rows(app)) == 2)
        await pilot.press("c")
        await pilot.pause()
        await fill(pilot, app, port="30072")
        await pilot.click("#ok")
        await settle(pilot, app, lambda: len(rows(app)) == 3)
        assert [r[4] for r in rows(app)] == ["n/d", "••••", "—"]
        await pilot.press("home", "s")
        await pilot.pause()
        assert "Senha desconhecida" in status(app)
        await pilot.press("down", "s")
        await pilot.pause()
        assert rows(app)[1][4] == "Secr3t"
        snapshot(app, f"f07-revealed-{size[0]}x{size[1]}")
        await pilot.press("s")
        await pilot.pause()
        assert rows(app)[1][4] == "••••"
        await pilot.press("x")
        await pilot.pause()
        assert "1 omitido(s): senha desconhecida" in str(app.screen.query(Static)[1].render())
        target = tmp_path / f"out-{size[0]}.txt"
        await fill(pilot, app, path=str(target))
        snapshot(app, f"f07-export-{size[0]}x{size[1]}")
        await pilot.click("#ok")
        await settle(pilot, app)
        assert str(target) in status(app)
        assert target.read_text() == "127.0.0.1:30071:dora:Secr3t\n127.0.0.1:30072\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        await pilot.press("x")
        await pilot.pause()
        await fill(pilot, app, path=str(target))
        await pilot.click("#ok")
        await settle(pilot, app)
        assert "já existe" in status(app)

@pytest.mark.parametrize("size", SIZES)
async def test_f08_api_down_and_bad_token(api, size):
    app = ProxyTui(Client("http://127.0.0.1:9", TOKEN), refresh_seconds=0)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        assert "API indisponível" in status(app) and "systemctl status ipv6-proxy-api" in status(app)
        snapshot(app, f"f08-down-{size[0]}x{size[1]}")
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

def test_export_lines_and_client_guards():
    proxies = [{"id": "a", "ipv4": "1.2.3.4", "port": 1, "username": None},
               {"id": "b", "ipv4": "1.2.3.4", "port": 2, "username": "u"},
               {"id": "c", "ipv4": "1.2.3.4", "port": 3, "username": "v"}]
    assert export_lines(proxies, {"c": "p"}) == (["1.2.3.4:1", "1.2.3.4:3:v:p"], 1)
    for url in ("https://127.0.0.1:8000", "http://example.com:8000", "http://127.0.0.1:8000/x"):
        with pytest.raises(ApiError):
            validate_url(url)
    assert validate_url("http://[::1]:8000/") == "http://[::1]:8000"
    with pytest.raises(ApiError):
        Client.from_env({"PROXY_API_TOKEN": "short"})
    assert Client.from_env({"PROXY_API_TOKEN": TOKEN}).token == TOKEN
