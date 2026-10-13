"""T11: validate ipv6-proxy-tui feature by feature in a real pty (80x24 and 60x20).

Real uvicorn API in-process, LocalProcessSupervisor + real 3proxy (only `ip addr`
is simulated, as in tests/acceptance). The TUI runs as its own process in a pty;
pyte renders the screen, saved as text and PNG (Chromium via system Playwright)
under docs/evidence/tui/.

    TEST_3PROXY_BINARY=/data/projects/ipv6-proxy-server-independente/.tools/3proxy \
        .venv/bin/python tests/tui_validation.py [F01 ...]

Exits 1 if any check fails; writes docs/evidence/tui/results.json.
"""
import html
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pexpect
import pyte
import uvicorn

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parent.parent)]
from acceptance.conftest import LoopbackBackend  # noqa: E402

from proxy_api.app import create_app  # noqa: E402
from proxy_api.config import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = Path(os.environ.get("TUI_EVIDENCE_DIR", ROOT / "docs" / "evidence" / "tui")).absolute()
TOKEN = "validation-admin-token-" + "v" * 20
# TUI_VIEWPORTS="80x24,120x40,60x20" (T40 visual critique); default keeps the T11 pair.
VIEWPORTS = {v: tuple(map(int, v.split("x"))) for v in os.environ.get("TUI_VIEWPORTS", "80x24,60x20").split(",")}

def free_port_block(size):
    """Contiguous free ports from a random high base: the acceptance suite (often run
    in parallel by other agents) scans from 21000, so a fixed base races with it."""
    import random
    for _ in range(200):
        base = random.randrange(40000, 60000 - size)
        held = []
        try:
            for port in range(base, base + size):
                sock = socket.socket()
                held.append(sock)
                sock.bind(("127.0.0.1", port))
            return list(range(base, base + size))
        except OSError:
            continue
        finally:
            for sock in held:
                sock.close()
    raise RuntimeError("no free port block")

class GatedBackend(LoopbackBackend):
    """Real backend whose replace() (rotate/edit) can be held to keep a request in flight."""

    def __init__(self, settings):
        super().__init__(settings)
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()

    def replace(self, old, new):
        self.entered.set()
        if not self.gate.wait(30):
            raise RuntimeError("gate timeout")
        super().replace(old, new)

class Api:
    def __init__(self, workdir, subnet="::ffff:127.0.0.0/120"):
        binary = os.environ.get("TEST_3PROXY_BINARY")
        if not binary:
            sys.exit("set TEST_3PROXY_BINARY")
        self.ports = free_port_block(24)
        # loopback-only validation harness: egress/targets are loopback, so the SSRF deny is off
        self.settings = Settings(subnet=subnet, bind_ipv4="127.0.0.1", interface="lo",
                                 block_private_destinations=False, api_token=TOKEN,
                                 data_dir=workdir / "data", binary=Path(binary).resolve(),
                                 port_start=self.ports[0], port_end=self.ports[-1], supervisor="local")
        self.app = create_app(self.settings, GatedBackend)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            assert time.monotonic() < deadline and self.thread.is_alive(), "API did not start"
            time.sleep(0.02)

    @property
    def manager(self):
        return self.app.state.manager

    @property
    def backend(self):
        return self.manager.backend

    def records(self):
        return {r.port: r for r in self.manager.records}

    def calls(self, mark=0):
        return [c for c in self.backend.supervisor.calls[mark:] if c["op"] in ("start", "stop", "restart")]

    def stop(self):
        self.backend.gate.set()
        self.server.should_exit = True
        self.thread.join(15)
        supervisor = self.backend.supervisor
        for port in supervisor.list_managed():
            supervisor.stop(port)

class Tui:
    """ipv6-proxy-tui in a real pty; pyte keeps the rendered screen."""

    def __init__(self, url, viewport, token=TOKEN, cwd=None):
        self.cols, self.rows = VIEWPORTS[viewport]
        self.viewport = viewport
        self.screen = pyte.Screen(self.cols, self.rows)
        self.stream = pyte.ByteStream(self.screen)
        env = {**os.environ, "PROXY_TUI_API_URL": url, "PROXY_API_TOKEN": token, "TERM": "xterm-256color",
               "PROXY_TUI_REFRESH": "0", "COLORTERM": "truecolor"}
        self.child = pexpect.spawn(sys.executable, [str(ROOT / "ipv6-proxy-tui")], env=env, cwd=str(cwd or ROOT),
                                   dimensions=(self.rows, self.cols), timeout=20)

    def pump(self, seconds=0.1):
        end = time.monotonic() + seconds
        while True:
            try:
                self.stream.feed(self.child.read_nonblocking(65536, timeout=0.05))
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                return
            if time.monotonic() >= end:
                return

    def text(self):
        return "\n".join(self.screen.display)

    def wait(self, needle, timeout=15):
        deadline = time.monotonic() + timeout
        while needle not in self.text():
            if time.monotonic() > deadline:
                raise AssertionError(f"{needle!r} not on screen ({self.viewport}):\n{self.text()}")
            self.pump(0.1)
        self.pump(0.3)  # let the frame finish

    def gone(self, needle, timeout=15):
        deadline = time.monotonic() + timeout
        while needle in self.text():
            if time.monotonic() > deadline:
                raise AssertionError(f"{needle!r} still on screen ({self.viewport}):\n{self.text()}")
            self.pump(0.1)
        self.pump(0.3)

    def send(self, keys, settle=0.4):
        self.child.send(keys)
        self.pump(settle)

    def esc(self):
        """A lone Esc followed at once by a key is read as Alt+key: let it time out."""
        self.send("\x1b", settle=0.7)

    def type(self, text):
        for ch in text:
            self.child.send(ch)
        self.pump(0.4)

    def quit(self):
        self.child.send("q")
        self.child.expect(pexpect.EOF, timeout=10)
        self.child.close()
        return self.child.exitstatus

    def kill(self):
        if self.child.isalive():
            self.child.terminate(force=True)

    def shot(self, name):
        """Save screen as .txt and .html (PNG rendered later in one Chromium run)."""
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        base = EVIDENCE / f"{name}-{self.viewport}"
        base.with_suffix(".txt").write_text(self.text() + "\n")
        base.with_suffix(".html").write_text(to_html(self.screen))
        return base

COLORS = {"black": "#000000", "red": "#cd3131", "green": "#0dbc79", "yellow": "#e5e510", "blue": "#2472c8",
          "magenta": "#bc3fbc", "cyan": "#11a8cd", "white": "#e5e5e5", "brightblack": "#666666",
          "brightred": "#f14c4c", "brightgreen": "#23d18b", "brightyellow": "#f5f543", "brightblue": "#3b8eea",
          "brightmagenta": "#d670d6", "brightcyan": "#29b8db", "brightwhite": "#ffffff"}

def color(value, default):
    if value == "default":
        return default
    if value in COLORS:
        return COLORS[value]
    return "#" + value if len(value) == 6 else default

def to_html(screen):
    rows = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        cells = []
        for x in range(screen.columns):
            ch = line[x]
            fg, bg = color(ch.fg, "#e5e5e5"), color(ch.bg, "#1e1e1e")
            if ch.reverse:
                fg, bg = bg, fg
            style = f"display:inline-block;width:9px;color:{fg};background:{bg}" + (";font-weight:bold" if ch.bold else "")
            # Fixed cell width keeps box-drawing fallback glyphs on the terminal grid.
            cells.append(f'<span style="{style}">{html.escape(ch.data or " ")}</span>')
        rows.append("".join(cells))
    return ('<html><body style="margin:0;background:#1e1e1e"><pre style="display:inline-block;margin:0;font:15px/18px '
            "'Liberation Mono','DejaVu Sans Mono',monospace\">" + "\n".join(rows) + "</pre></body></html>")

RENDER = """
import os, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    import glob
    shells = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/*headless*shell")))
    b = p.chromium.launch(executable_path=shells[-1] if shells else None)
    page = b.new_page(device_scale_factor=1)
    for path in sys.argv[1:]:
        page.goto("file://" + path)
        page.locator("pre").screenshot(path=path[:-5] + ".png")
    b.close()
"""

def render_pngs():
    pages = sorted(str(p) for p in EVIDENCE.glob("*.html"))
    if not pages:
        return
    # System python carries Playwright; the project venv does not (no new dependency).
    subprocess.run(["/usr/local/bin/python3", "-c", RENDER, *pages], check=True, timeout=300)
    for page in pages:
        Path(page).unlink()

UP, DOWN, TAB, SHIFT_TAB, ENTER, BACKSPACE = "\x1b[A", "\x1b[B", "\t", "\x1b[Z", "\r", "\x7f"

class Results:
    def __init__(self):
        self.items = {}

    def check(self, feature, viewport, criterion, ok, seen, evidence=None):
        entry = {"viewport": viewport, "criterion": criterion, "ok": bool(ok), "seen": seen}
        if evidence:
            entry["evidence"] = str(Path(evidence).relative_to(ROOT)) + ".png"
        self.items.setdefault(feature, []).append(entry)
        print(("OK  " if ok else "FAIL") + f" {feature} {viewport} {criterion}: {seen}", flush=True)

def choose(tui, option, moves):
    """Open the focused Select, wait for its overlay to list `option`, move and pick."""
    tui.send(ENTER)
    tui.wait(option)
    tui.send(moves)
    tui.send(ENTER)

def select_port(tui, ports, port):
    tui.send(UP * len(ports), settle=0.2)
    tui.send(DOWN * sorted(ports).index(port), settle=0.4)

def snapshot(api):
    return {p: (str(r.ipv6), r.protocol, r.username, r.id) for p, r in api.records().items()}

def check_real_ipv6(viewport, results):
    """Routable-looking /64: hex groups like :1234: or :cd: are also Rich emoji codes."""
    import tempfile
    from proxy_tui.client import Client
    api = Api(Path(tempfile.mkdtemp(prefix=f"tui-val-ipv6-{viewport}-")), subnet="2001:db8:abcd:1234::/64")
    tui = None
    try:
        created = Client(api.url, TOKEN).create({"port": api.ports[0]})
        tui = Tui(api.url, viewport)
        tui.wait("1 proxy(s)")
        tui.send("\x1b[C" * 40, settle=1.0)  # scroll right: IPv6 is the last column
        shot = tui.shot("F01-ipv6-real")
        screen = tui.text()
        row = next((line.strip() for line in screen.splitlines() if "2001:db8" in line), "")
        results.check("F01", viewport, "IPv6 exibido exatamente como na API", created["ipv6"] in screen,
                      f"API: {created['ipv6']} | tela: {row}", shot)
        tui.quit()
    finally:
        if tui:
            tui.kill()
        api.stop()

def run_viewport(viewport, results, only):
    import tempfile
    work = Path(tempfile.mkdtemp(prefix=f"tui-val-{viewport}-"))
    api = Api(work)
    P = api.ports
    want = lambda f: not only or f in only  # noqa: E731
    try:
        if want("F08"):
            bad = Tui(api.url, viewport, token="w" * 40)
            try:
                bad.wait("Token inválido")
                shot = bad.shot("F08-token-invalido")
                screen = bad.text()
                results.check("F08", viewport, "token inválido: erro acionável sem traceback",
                              "PROXY_API_TOKEN" in screen and "Traceback" not in screen,
                              "status mostra 'Token inválido. Confira PROXY_API_TOKEN em /etc/ipv6-proxy-api.env'", shot)
                code = bad.quit()
                results.check("F08", viewport, "sai limpo com q após erro", code == 0, f"exit {code}")
            finally:
                bad.kill()
            closed = socket.socket()
            closed.bind(("127.0.0.1", 0))
            down_url = f"http://127.0.0.1:{closed.getsockname()[1]}"
            closed.close()
            down = Tui(down_url, viewport)
            try:
                down.wait("Não foi possível carregar")
                down.pump(0.5)
                shot = down.shot("F08-api-indisponivel")
                screen = down.text()
                results.check("F08", viewport, "API indisponível: erro acionável sem traceback",
                              "Traceback" not in screen and ("journalctl" in screen or "API" in screen),
                              " | ".join(line.strip() for line in screen.splitlines() if line.strip())[-220:], shot)
                down.send("a", settle=1.0)
                results.check("F08", viewport, "recarregar com API fora não trava", down.child.isalive(),
                              "processo vivo após a")
                down.quit()
            finally:
                down.kill()

        tui = Tui(api.url, viewport, cwd=work)
        try:
            tui.wait("Nenhum proxy cadastrado")
            screen = tui.text()
            if want("F01"):
                shot = tui.shot("F01-vazio")
                results.check("F01", viewport, "estado vazio com instrução de criar",
                              "Pressione c para criar" in screen, "‘Nenhum proxy cadastrado. Pressione c…’", shot)
            if want("F02"):
                tui.wait("0 proxy(s)")
                footer = tui.text().splitlines()[-1]
                complete = all(k in footer for k in ("? Ajuda", "c Criar", "a Recarregar", "q Sair"))
                results.check("F02", viewport, "atalhos visíveis no rodapé", complete,
                              f"rodapé: {footer.strip()!r}", tui.shot("F02-rodape"))
                tui.send("?")
                tui.wait("Atalhos")
                shot = tui.shot("F02-ajuda")
                help_text = tui.text()
                # Body may scroll on small terminals: scroll and accumulate.
                for _ in range(6):
                    tui.send("\x1b[6~", settle=0.3)  # PageDown
                    help_text += tui.text()
                keys = ["mover seleção", "criar proxy", "criar lote", "editar", "rotacionar", "excluir",
                        "ver/ocultar senha", "exportar", "recarregar", "esta ajuda", "fechar diálogo", "sair"]
                flat = " ".join(help_text.split()).replace("│", "")
                flat = " ".join(flat.split())
                missing = [k for k in keys if k not in flat]
                results.check("F02", viewport, "? abre ajuda PT-BR com todos os atalhos", not missing,
                              f"faltando: {missing}" if missing else "12 atalhos presentes", shot)
                tui.esc()
                results.check("F02", viewport, "Esc fecha ajuda", "Atalhos" not in tui.text(), "ajuda fechada")
                tui.send("c")
                tui.wait("Criar proxy")
                tui.send(TAB)  # dialogs open focused on the scroll box
                tui.type(str(P[0]))
                tui.esc()
                tui.pump(1.0)
                results.check("F02", viewport, "Esc fecha diálogo sem efeito colateral",
                              "Criar proxy" not in tui.text() and not api.records(),
                              f"formulário com porta {P[0]} cancelado; registros: {len(api.records())}")

            if want("F03"):
                tui.send("c")
                tui.wait("Criar proxy")
                tui.send(TAB)  # dialogs open focused on the scroll box
                tui.type("80")
                tui.send(ENTER)
                tui.wait("Porta deve ser")
                shot = tui.shot("F03-erro-porta")
                results.check("F03", viewport, "porta inválida: erro inline", not api.records(),
                              "‘✕ Porta deve ser um número entre 1024 e 65535.’", shot)
                tui.send(BACKSPACE * 2)
                tui.type(str(P[0]))
                tui.send(TAB + TAB)  # protocol, username
                tui.type("bad user")
                tui.send(TAB)
                tui.type("x")
                tui.send(ENTER)
                tui.wait("Usuário: 1-64")
                shot = tui.shot("F03-erro-credenciais")
                results.check("F03", viewport, "credenciais inválidas: erro inline", not api.records(),
                              "‘✕ Usuário: 1-64 caracteres…’", shot)
                tui.send(SHIFT_TAB)  # Enter left focus on Senha: back to Usuário
                tui.send(BACKSPACE * 8)
                tui.type("alice")
                tui.send(TAB)
                tui.send(BACKSPACE)
                tui.type("S3cretPw")
                shot = tui.shot("F03-senha-mascarada")
                results.check("F03", viewport, "senha digitada mascarada", "S3cretPw" not in tui.text(),
                              "campo Senha mostra só •", shot)
                tui.send(ENTER)
                tui.wait("Proxy criado na porta")
                tui.wait(str(P[0]))
                shot = tui.shot("F03-criado")
                record = api.records().get(P[0])
                results.check("F03", viewport, "feedback de sucesso e novo item na lista",
                              record is not None and record.username == "alice" and "S3cretPw" not in tui.text(),
                              f"status ‘Proxy criado na porta {P[0]} com IPv6 …’; linha {P[0]} ● ativo", shot)
                # protocol choice: socks5 without auth
                tui.send("c")
                tui.wait("Criar proxy")
                tui.send(TAB)  # dialogs open focused on the scroll box
                tui.type(str(P[1]))
                tui.send(TAB)
                choose(tui, "SOCKS5", DOWN)
                tui.send(TAB + TAB + TAB)  # username, password, confirm button
                tui.send(ENTER)
                tui.wait("Proxy criado na porta " + str(P[1]))
                record = api.records().get(P[1])
                results.check("F03", viewport, "protocolo escolhido no formulário",
                              record is not None and record.protocol == "socks5",
                              f"porta {P[1]} criada como {record and record.protocol}")

            if want("F01"):
                tui.wait("● ativo")
                shot = tui.shot("F01-lista")
                header = next((line for line in tui.text().splitlines() if "Porta" in line), "")
                results.check("F01", viewport, "tabela com porta, protocolo, IPv6 e status em texto/símbolo",
                              all(h in header for h in ("Porta", "Status", "Proto")) and "● ativo" in tui.text(),
                              f"cabeçalho: {header.strip()}", shot)
                ipv6_visible = "IPv6" in header
                results.check("F01", viewport, "coluna IPv6 visível no viewport", ipv6_visible,
                              "IPv6 visível" if ipv6_visible else "IPv6 fora da largura (rolar →)")
                pid = api.backend.supervisor.main_pid(P[1])
                os.kill(pid, 9)
                time.sleep(0.5)
                tui.send("a")
                tui.wait("falhou")
                shot = tui.shot("F01-status-atualizado")
                results.check("F01", viewport, "status atualiza por atalho de recarregar (a)",
                              "✕ falhou" in tui.text() and "fora do ar" in tui.text(),
                              f"3proxy da porta {P[1]} morto (kill -9); após a: ‘✕ falhou’ + aviso journalctl", shot)

            if want("F05"):
                before = snapshot(api)
                mark = len(api.backend.supervisor.calls)
                select_port(tui, list(api.records()), P[1])
                tui.send("e")
                tui.wait("Editar proxy da porta")
                tui.send(TAB)  # dialogs open focused on the scroll box
                tui.send(BACKSPACE * 6)
                tui.type(str(P[0]))
                tui.send(ENTER)
                tui.wait("Conflito")
                shot = tui.shot("F05-conflito")
                results.check("F05", viewport, "conflito de porta mostra erro sem alterar nada",
                              snapshot(api) == before and not api.calls(mark),
                              f"porta {P[1]}→{P[0]}: ‘Conflito: porta já em uso…’; nada mudou", shot)
                select_port(tui, list(api.records()), P[1])
                tui.send("e")
                tui.wait("Editar proxy da porta")
                tui.send(TAB)  # dialogs open focused on the scroll box
                tui.send(BACKSPACE * 6)
                tui.type(str(P[2]))
                tui.send(TAB)
                choose(tui, "HTTP", UP)  # socks5 -> http
                tui.send(TAB)
                tui.type("bob")
                tui.send(TAB)
                tui.type("NewPass1")
                shot = tui.shot("F05-formulario")
                tui.send(ENTER)
                tui.wait("atualizado")
                shot2 = tui.shot("F05-editado")
                after = snapshot(api)
                moved = api.records().get(P[2])
                others = {p: v for p, v in after.items() if p != P[2]}
                touched = {c["port"] for c in api.calls(mark)}
                results.check("F05", viewport, "altera credenciais/protocolo/porta",
                              moved is not None and moved.protocol == "http" and moved.username == "bob"
                              and P[1] not in after,
                              f"{P[1]} SOCKS5 → {P[2]} HTTP usuário bob", shot2)
                results.check("F05", viewport, "demais proxies inalterados",
                              others == {p: v for p, v in before.items() if p not in (P[1],)}
                              and touched <= {P[1], P[2]},
                              f"supervisor tocou só {sorted(touched)}; {P[0]} idêntico", shot)

            if want("F06"):
                before = snapshot(api)
                mark = len(api.backend.supervisor.calls)
                target = P[2] if P[2] in before else P[0]
                select_port(tui, list(before), target)
                tui.send("r")
                tui.wait("Rotacionar o IPv6")
                shot = tui.shot("F06-confirma-rotacao")
                tui.send(ENTER)
                tui.wait("novo IPv6")
                shot2 = tui.shot("F06-rotacionado")
                after = snapshot(api)
                results.check("F06", viewport, "rotação mostra novo IPv6 só do alvo",
                              after[target][0] != before[target][0]
                              and all(after[p] == before[p] for p in before if p != target)
                              and {c["port"] for c in api.calls(mark)} == {target},
                              f"porta {target}: {before[target][0]} → {after[target][0]}; outras iguais", shot2)
                # delete: wrong port, then cancel, then confirm
                before = snapshot(api)
                victim = sorted(before)[0]
                select_port(tui, list(before), victim)
                tui.send("d")
                tui.wait(f"Digite {victim}")
                tui.send(TAB)
                tui.type("1234")
                tui.send(ENTER)
                tui.wait("Digite exatamente")
                shot = tui.shot("F06-exclusao-errada")
                results.check("F06", viewport, "exclusão exige confirmar o alvo (porta)", snapshot(api) == before,
                              "porta errada → ‘Digite exatamente …; nada foi alterado.’", shot)
                tui.esc()
                tui.send("r")
                tui.wait("Rotacionar o IPv6")
                tui.esc()
                tui.pump(1.0)
                results.check("F06", viewport, "cancelar não altera nada", snapshot(api) == before,
                              "Esc em excluir e em rotacionar; registros idênticos")
                # race: rotate held in flight, user moves selection and tries other actions
                ports = sorted(before)
                tui.send("c")
                tui.wait("Criar proxy")
                tui.send(TAB)  # dialogs open focused on the scroll box
                tui.type(str(P[3]))
                tui.send(ENTER)
                tui.wait("Proxy criado na porta " + str(P[3]))
                before = snapshot(api)
                ports = sorted(before)
                mark = len(api.backend.supervisor.calls)
                api.backend.gate.clear()
                api.backend.entered.clear()
                select_port(tui, ports, ports[0])
                tui.send("r")
                tui.wait("Rotacionar o IPv6")
                tui.send(ENTER)
                assert api.backend.entered.wait(10), "rotate never reached the backend"
                tui.send(DOWN)
                tui.send("d")
                try:
                    tui.wait("Aguarde", timeout=5)
                    busy_delete = True
                except AssertionError:
                    busy_delete = False
                tui.send("r")
                tui.send("e")
                tui.esc()
                tui.send("a")
                shot = tui.shot("F06-corrida-em-andamento")
                busy = "Aguarde" in tui.text()
                dialog_open = "Digite" in tui.text() or "Editar proxy" in tui.text()
                api.backend.gate.set()
                tui.wait("novo IPv6")
                tui.pump(0.5)
                after = snapshot(api)
                touched = {c["port"] for c in api.calls(mark)}
                results.check("F06", viewport, "navegar/cancelar durante requisição não opera outro proxy",
                              busy and busy_delete and not dialog_open and touched == {ports[0]}
                              and after[ports[0]][0] != before[ports[0]][0]
                              and all(after[p] == before[p] for p in ports[1:]),
                              f"rotação de {ports[0]} retida; ↓ + d/r/e/Esc/a → ‘Aguarde…’; supervisor tocou "
                              f"só {sorted(touched)}; {ports[1:]} intactos "
                              f"[busy={busy} busy_delete={busy_delete} dialog={dialog_open} "
                              f"rotated={after[ports[0]][0] != before[ports[0]][0]}]", shot)
                before = snapshot(api)
                victim = sorted(before)[-1]
                select_port(tui, list(before), victim)
                tui.send("d")
                tui.wait(f"Digite {victim}")
                tui.send(TAB)
                tui.type(str(victim))
                tui.send(ENTER)
                tui.wait("excluído")
                shot = tui.shot("F06-excluido")
                after = snapshot(api)
                results.check("F06", viewport, "exclusão confirmada remove só o alvo",
                              victim not in after and all(after[p] == before[p] for p in after),
                              f"porta {victim} removida; demais intactas", shot)

            if want("F04"):
                tui.send("l")
                tui.wait("Criar lote")
                tui.send(TAB)  # dialogs open focused on the scroll box
                tui.send(BACKSPACE * 2)
                tui.type("0")
                tui.send(ENTER)
                tui.wait("Quantidade deve ser")
                shot = tui.shot("F04-validacao")
                count_before = len(api.records())
                results.check("F04", viewport, "validação antes de aplicar", len(api.records()) == count_before,
                              "quantidade 0 → ‘Quantidade deve ser um número entre 1 e 1000’", shot)
                tui.send(BACKSPACE)
                tui.type("3")
                tui.send(TAB)
                tui.type(str(P[10]))
                tui.send(TAB)  # protocol
                tui.send(TAB)  # mode
                choose(tui, "Comum (mesmo", DOWN)
                tui.send(TAB)
                tui.type("lote")
                tui.send(TAB)
                tui.type("LotePw1")
                tui.send(TAB)
                tui.send(" ")  # partial
                shot_form = tui.shot("F04-formulario")
                blocker = socket.socket()
                blocker.bind(("127.0.0.1", P[11]))
                blocker.listen()
                try:
                    tui.send(TAB)  # Enter on the checkbox toggles it: submit via Confirmar
                    tui.send(ENTER)
                    tui.wait("Resultado do lote", timeout=40)
                    shot = tui.shot("F04-resultado")
                    screen = tui.text()
                finally:
                    blocker.close()
                records = api.records()
                created = [p for p in (P[10], P[12]) if p in records and records[p].username == "lote"]
                state = {p: (r.username, r.protocol) for p, r in records.items() if p >= P[10]}
                results.check("F04", viewport, "quantidade, porta inicial, protocolo e modo de credenciais",
                              len(created) == 2, f"API: {state}; 3 a partir de "
                              f"{P[10]}, HTTP, comum lote/•••, parcial", shot_form)
                results.check("F04", viewport, "resultado mostra criados e falhas",
                              "Criados: 2" in screen and "Falhas: 1" in screen and str(P[11]) in screen,
                              f"‘Criados: 2 … Falhas: 1 — {P[11]}’ (porta {P[11]} ocupada por socket externo)", shot)
                tui.esc()
                tui.send("l")
                tui.wait("Criar lote")
                tui.send(TAB)  # dialogs open focused on the scroll box
                tui.send(BACKSPACE * 2)
                tui.type("2")
                tui.send(TAB)
                tui.type(str(P[14]))
                tui.send(TAB + TAB)
                choose(tui, "Aleatória por proxy", DOWN + DOWN)
                tui.send(TAB * 4)  # usuário, senha, parcial, Confirmar
                tui.send(ENTER)
                tui.wait("Resultado do lote", timeout=40)
                shot = tui.shot("F04-aleatorio")
                records = api.records()
                results.check("F04", viewport, "modo aleatório cria com usuários distintos",
                              P[14] in records and P[15] in records and records[P[14]].username
                              and records[P[14]].username != records[P[15]].username
                              and "exporte com x" in tui.text(),
                              "2 criados; aviso ‘Senhas aleatórias ficam só nesta sessão: exporte com x’", shot)
                tui.esc()

            if want("F07"):
                # Created by another client: the TUI cannot know its password.
                from proxy_tui.client import Client
                Client(api.url, TOKEN).create({"port": P[20], "username": "outro", "password": "OtherPw9"})
                tui.send("a")
                tui.pump(1.0)
                screen = tui.text()
                known = [p for p, r in api.records().items() if r.username]
                results.check("F07", viewport, "senhas mascaradas por padrão", "••••" in screen
                              and "LotePw1" not in screen and "S3cretPw" not in screen and "NewPass1" not in screen,
                              "coluna Senha ‘••••’ / ‘n/d’", tui.shot("F07-mascarado"))
                target = P[10] if P[10] in api.records() else known[0]
                select_port(tui, list(api.records()), target)
                tui.send("s")
                tui.wait("visível")
                shot = tui.shot("F07-revelado")
                revealed = tui.text().count("LotePw1")
                results.check("F07", viewport, "revelar só por ação explícita (s), só o selecionado",
                              revealed == 1, f"após s: senha da porta {target} visível ({revealed} ocorrência)", shot)
                tui.send("s")
                tui.wait("oculta")
                results.check("F07", viewport, "s de novo oculta", "LotePw1" not in tui.text(), "senha oculta")
                tui.send("x")
                tui.wait("Exportar lista")
                tui.send(TAB)  # dialogs open focused on the scroll box
                shot = tui.shot("F07-dialogo")
                tui.send(ENTER)
                tui.wait("(0600)")
                shot2 = tui.shot("F07-exportado")
                files = sorted(work.glob("proxies-*.txt"))
                lines = files[-1].read_text().splitlines() if files else []
                mode = oct(files[-1].stat().st_mode & 0o777) if files else None
                ok_lines = all(len(line.split(":")) in (2, 4) and line.startswith("127.0.0.1:") for line in lines)
                results.check("F07", viewport, "exporta IPv4:porta:usuario:senha em arquivo 0600 com caminho",
                              files and mode == "0o600" and ok_lines and f"127.0.0.1:{P[10]}:lote:LotePw1" in lines
                              and str(files[-1].name) in "".join(tui.text().split()),
                              f"{len(lines)} linhas, modo {mode}, caminho no status; ex.: {lines[:1]}", shot2)
                results.check("F07", viewport, "diálogo informa omitidos (senha desconhecida)",
                              "omitido" in tui.text() or "omitido" in Path(str(shot) + ".txt").read_text(),
                              "aviso ‘N omitido(s): senha desconhecida’", shot)

            if want("F08"):
                shot = tui.shot("F08-lista-cheia")
                screen = tui.text().splitlines()
                results.check("F08", viewport, "legível sem quebra/crash",
                              tui.child.isalive() and len(screen) == tui.rows
                              and "Traceback" not in tui.text(),
                              f"{tui.cols}x{tui.rows}: tabela, status e rodapé renderizados", shot)
            code = tui.quit()
            results.check("F02", viewport, "q sai com código 0", code == 0, f"exit {code}")
        finally:
            tui.kill()
    finally:
        api.stop()

def main():
    only = set(sys.argv[1:])
    results = Results()
    for old in EVIDENCE.glob("*"):
        if old.suffix in (".txt", ".png", ".html"):
            old.unlink()
    for viewport in VIEWPORTS:
        try:
            if not only or "F01" in only:
                check_real_ipv6(viewport, results)
            run_viewport(viewport, results, only)
        except Exception as exc:  # a scenario that breaks is a failure, not a skip
            results.check("RUN", viewport, "cenário completo", False, repr(exc)[:2000])
    render_pngs()
    (EVIDENCE / "results.json").write_text(json.dumps(results.items, indent=2, ensure_ascii=False) + "\n")
    failed = [e for items in results.items.values() for e in items if not e["ok"]]
    print(f"{sum(len(v) for v in results.items.values()) - len(failed)} ok, {len(failed)} falhas")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
