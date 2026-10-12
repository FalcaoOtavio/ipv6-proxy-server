"""ipv6-proxy-tui: keyboard TUI over the local proxy API.

Every mutation pins its target (id + port, sent as If-Match) when the action
starts, so moving the selection while a request runs never hits another proxy.
Passwords live only in memory and only for proxies whose password this session
set (the API never returns them).
"""
import asyncio
import os
import time
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static

from .client import BATCH_MAX, ApiError, Client
from .screens import (BatchDialog, ConfirmDialog, CreateDialog, EditDialog, ExportDialog, HelpDialog,
                      MessageDialog)

STATUS = {
    "running": ("● ativo", "green"),
    "starting": ("◐ subindo", "yellow"),
    "failed": ("✕ falhou", "bold red"),
    "fenced": ("■ isolado", "bold magenta"),
}
# IPv6 last: it is the widest column and the one cut first on narrow terminals.
COLUMNS = [("Porta", "port"), ("Status", "status"), ("Proto", "protocol"), ("Usuário", "username"),
           ("Senha", "password"), ("IPv6", "ipv6")]
GENERIC_ERROR = "Erro inesperado na operação; nada foi exibido por segurança. Recarregue com a e tente de novo."
# Two fixed lines instead of Footer: Footer truncates from the right at 80 columns. Essentials last
# so they sit on the bottom line; each line fits 60 columns minus padding.
KEY_LINES = [[("l", "Lote"), ("e", "Editar"), ("r", "Rotacionar"), ("d", "Excluir"), ("x", "Exportar")],
             [("?", "Ajuda"), ("c", "Criar"), ("a", "Recarregar"), ("s", "Senha"), ("q", "Sair")]]
EMPTY = "Nenhum proxy cadastrado.\n\nPressione c para criar um proxy ou l para criar um lote.\n? mostra todos os atalhos."

def export_lines(proxies, passwords):
    """IPv4:port[:user:password] lines; proxies with auth but unknown password are skipped."""
    lines, skipped = [], 0
    for proxy in proxies:
        if proxy.get("username") is None:
            lines.append(f"{proxy['ipv4']}:{proxy['port']}")
        elif proxy["id"] in passwords:
            lines.append(f"{proxy['ipv4']}:{proxy['port']}:{proxy['username']}:{passwords[proxy['id']]}")
        else:
            skipped += 1
    return lines, skipped

def key_bar():
    text = Text()
    for number, line in enumerate(KEY_LINES):
        if number:
            text.append("\n")
        for index, (key, label) in enumerate(line):
            text.append(("  " if index else "") + key, style="bold")
            text.append(" " + label)
    return text

def write_private(path, lines):
    """Create (never overwrite) a 0600 file; returns the absolute path."""
    path = Path(path).expanduser().absolute()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("".join(line + "\n" for line in lines))
        handle.flush()
        os.fsync(handle.fileno())
    return path

class ProxyTui(App):
    TITLE = "IPv6 Proxy Server"
    SUB_TITLE = "gestão de proxies"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #table { height: 1fr; }
    #empty { height: 1fr; content-align: center middle; text-align: center; padding: 1 2; }
    #status { height: auto; max-height: 3; padding: 0 1; background: $boost; }
    #status.error { color: $text-error; background: $error-muted; text-style: bold; }
    #status.ok { color: $text-success; }
    #keys { height: 2; padding: 0 1; background: $panel; }
    """
    BINDINGS = [
        Binding("question_mark", "help", "Ajuda", key_display="?"),
        Binding("c", "create", "Criar"),
        Binding("l", "batch", "Lote"),
        Binding("e", "edit", "Editar"),
        Binding("r", "rotate", "Rotacionar"),
        Binding("d", "delete", "Excluir"),
        Binding("x", "export", "Exportar"),
        Binding("s", "reveal", "Senha"),
        Binding("a", "reload", "Recarregar"),
        Binding("q", "quit", "Sair"),
    ]

    def __init__(self, client=None, startup_error=None, refresh_seconds=5.0):
        super().__init__()
        self.client = client
        self.startup_error = startup_error
        self.refresh_seconds = refresh_seconds
        self.proxies = []
        self.passwords = {}
        self.revealed = set()
        self.busy = None
        self.batch_max = BATCH_MAX

    def compose(self) -> ComposeResult:
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static(EMPTY, id="empty", markup=False)
        # API messages carry user data (IPv6, usernames, paths): never parse them as markup/emoji.
        yield Static("Carregando…", id="status", markup=False)
        yield Static(key_bar(), id="keys")

    def on_mount(self):
        self.query_one("#table").display = False
        if self.client is None:
            self.say(self.startup_error or "Cliente da API não configurado.", error=True)
            self.query_one("#empty", Static).update("Sem conexão com a API.\n\nCorrija o problema acima e reabra a TUI.")
            return
        self.run_worker(self.load(), group="refresh")
        if self.refresh_seconds:
            self.set_interval(self.refresh_seconds, self.periodic)

    def _fatal_error(self):
        """Textual's default renders a traceback with show_locals, i.e. passwords in
        clear on the terminal. Exit with a fixed message instead."""
        self.bell()
        self._exit_renderables.append(Text("ipv6-proxy-tui: " + GENERIC_ERROR, style="bold red"))
        self._close_messages_no_wait()

    # -- display -----------------------------------------------------------
    def say(self, message, error=False):
        status = self.query_one("#status", Static)
        status.set_classes("error" if error else "ok")
        status.update(("✕ " if error else "✓ ") + message)

    def selected(self):
        """Snapshot (copy) of the highlighted proxy: the pinned target of an action."""
        table = self.query_one("#table", DataTable)
        if not self.proxies or not table.display:
            return None
        row = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        return next((dict(p) for p in self.proxies if p["id"] == row), None)

    def password_cell(self, proxy):
        if proxy.get("username") is None:
            return "—"
        if proxy["id"] not in self.passwords:
            return "n/d"
        return self.passwords[proxy["id"]] if proxy["id"] in self.revealed else "••••"

    def render_rows(self):
        table = self.query_one("#table", DataTable)
        current = self.selected()
        rows = []
        for proxy in self.proxies:
            label, style = STATUS.get(proxy.get("status"), ("? —", "dim"))
            # Plain str cells are parsed as markup by DataTable (':1234:' in an IPv6 became an emoji).
            rows.append((proxy["id"], [Text(str(proxy["port"])), Text(label, style=style),
                                       Text(proxy["protocol"].upper()), Text(proxy.get("username") or "—"),
                                       Text(self.password_cell(proxy)), Text(proxy["ipv6"])]))
        # Fixed widths computed here: auto widths are only measured when the table goes idle, which
        # under a modal (batch result) can leave columns truncated ('SOCKS' for 'SOCKS5') on screen.
        table.clear(columns=True)
        for index, (label, key) in enumerate(COLUMNS):
            table.add_column(label, key=key, width=max([cell_len(label)] + [c[index].cell_len for _, c in rows]))
        for key, cells in rows:
            table.add_row(*cells, key=key)
        table.display = bool(self.proxies)
        self.query_one("#empty").display = not self.proxies
        if current:
            index = next((i for i, p in enumerate(self.proxies) if p["id"] == current["id"]), None)
            if index is not None:
                table.move_cursor(row=index)

    async def load(self, quiet=False):
        try:
            proxies = await asyncio.to_thread(self.client.list_with_status)
        except ApiError as exc:
            self.say(exc.args[0], error=True)
            if not self.proxies:
                self.query_one("#empty", Static).update(
                    "Não foi possível carregar a lista.\n\nCorrija o problema abaixo e pressione a para recarregar.")
            return False
        self.proxies = sorted(proxies, key=lambda p: p["port"])
        known = {p["id"] for p in self.proxies}
        self.revealed &= known
        self.query_one("#empty", Static).update(EMPTY)
        self.render_rows()
        if not quiet:
            failing = sum(p.get("status") in ("failed", "fenced") for p in self.proxies)
            message = f"{len(self.proxies)} proxy(s)."
            if failing:
                self.say(message + f" {failing} fora do ar: sudo journalctl -u ipv6-proxy@<porta>", error=True)
            else:
                self.say(message)
        return True

    def periodic(self):
        # The timer can fire while the app shuts down (empty screen stack).
        if self.is_running and len(self.screen_stack) == 1 and self.busy is None:
            self.run_worker(self.load(quiet=True), group="refresh", exclusive=True)

    # -- operations --------------------------------------------------------
    def start(self, label, coroutine):
        """Run one mutation at a time in the background; the UI stays responsive."""
        if self.busy:
            coroutine.close()
            self.say(f"Aguarde: {self.busy} em andamento.", error=True)
            return
        self.busy = label
        self.say(f"{label}…")
        self.run_worker(self._guarded(coroutine), group="mutation")

    async def _guarded(self, coroutine):
        try:
            message = await coroutine
        except Exception as exc:
            self.busy = None
            if isinstance(exc, ApiError):
                if exc.limit:
                    self.batch_max = exc.limit
                text = exc.args[0]
            else:
                # Never render the exception: its text/locals can hold passwords.
                text = GENERIC_ERROR
            self.say(text, error=True)
            await self.load(quiet=True)
            return
        self.busy = None
        await self.load(quiet=True)
        if message:
            self.say(message)

    def ready(self):
        """False (with a message) when no API client or another mutation is running."""
        if self.client is None:
            self.say(self.startup_error or "Sem conexão com a API.", error=True)
            return False
        if self.busy:
            self.say(f"Aguarde: {self.busy} em andamento.", error=True)
            return False
        return True

    def needs_target(self):
        if not self.ready():
            return None
        target = self.selected()
        if target is None:
            self.say("Nenhum proxy selecionado. Use c para criar.", error=True)
        return target

    def action_help(self):
        self.push_screen(HelpDialog())

    def action_reload(self):
        if self.busy:
            self.say(f"Aguarde: {self.busy} em andamento; a lista recarrega ao final.", error=True)
            return
        if self.client is not None:
            self.run_worker(self.load(), group="refresh", exclusive=True)

    def action_reveal(self):
        target = self.needs_target()
        if target is None:
            return
        if target.get("username") is None:
            self.say(f"Porta {target['port']} não usa autenticação.")
        elif target["id"] not in self.passwords:
            self.say("Senha desconhecida: a API não devolve senhas; só as definidas nesta sessão aparecem.",
                     error=True)
        else:
            self.revealed ^= {target["id"]}
            self.render_rows()
            self.say(f"Senha da porta {target['port']} " + ("visível." if target["id"] in self.revealed else "oculta."))

    def action_create(self):
        if not self.ready():
            return

        async def run(payload):
            created = await asyncio.to_thread(self.client.create, payload)
            if "password" in payload:
                self.passwords[created["id"]] = payload["password"]
            return f"Proxy criado na porta {created['port']} com IPv6 {created['ipv6']}."

        self.push_screen(CreateDialog(), lambda payload: payload and self.start("Criando proxy", run(payload)))

    def action_batch(self):
        if not self.ready():
            return

        async def run(payload):
            result = await asyncio.to_thread(self.client.batch, payload)
            created, failed = result.get("created", []), result.get("failed", [])
            for item in created:
                password = item.get("password") or payload.get("password")
                if password and item.get("username"):
                    self.passwords[item["id"]] = password
            ports = [item["port"] for item in created]
            summary = f"Criados: {len(created)}"
            if ports:
                summary += f" (portas {ports[0]}–{ports[-1]})"
            if failed:
                summary += f"\nFalhas: {len(failed)} — " + ", ".join(str(f["port"]) for f in failed)
                summary += "\nVeja: sudo journalctl -u ipv6-proxy@<porta>"
            if payload["credentials"] == "random":
                summary += "\nSenhas aleatórias ficam só nesta sessão: exporte com x antes de sair."
                if any(item.get("password") is None for item in created):
                    # Replay after a lost response: the API never stores passwords.
                    summary += "\nSenhas perdidas na reconexão (n/d): defina novas com e."
            self.call_later(self.push_screen, MessageDialog("Resultado do lote", summary))
            return f"Lote concluído: {len(created)} criado(s), {len(failed)} falha(s)."

        self.push_screen(BatchDialog(self.batch_max), lambda payload: payload and self.start("Criando lote", run(payload)))

    def action_edit(self):
        target = self.needs_target()
        if target is None:
            return

        async def run(payload):
            updated = await asyncio.to_thread(self.client.update, target, payload)
            if "password" in payload:
                if payload["password"] is None:
                    self.passwords.pop(target["id"], None)
                else:
                    self.passwords[target["id"]] = payload["password"]
            return f"Proxy {target['port']} atualizado: porta {updated['port']}, {updated['protocol'].upper()}."

        self.push_screen(EditDialog(target),
                         lambda payload: payload and self.start(f"Editando porta {target['port']}", run(payload)))

    def action_rotate(self):
        target = self.needs_target()
        if target is None:
            return

        async def run():
            rotated = await asyncio.to_thread(self.client.rotate, target)
            return f"Porta {target['port']}: novo IPv6 {rotated['ipv6']} (era {target['ipv6']})."

        self.push_screen(ConfirmDialog(f"Rotacionar o IPv6 da porta {target['port']}?", target["port"], typed=False),
                         lambda ok: ok and self.start(f"Rotacionando porta {target['port']}", run()))

    def action_delete(self):
        target = self.needs_target()
        if target is None:
            return

        async def run():
            await asyncio.to_thread(self.client.delete, target)
            self.passwords.pop(target["id"], None)
            return f"Proxy da porta {target['port']} excluído."

        self.push_screen(ConfirmDialog(f"Excluir o proxy da porta {target['port']}?", target["port"]),
                         lambda ok: ok and self.start(f"Excluindo porta {target['port']}", run()))

    def action_export(self):
        if not self.proxies:
            self.say("Nada para exportar.", error=True)
            return
        snapshot = [dict(p) for p in self.proxies]
        lines, skipped = export_lines(snapshot, self.passwords)
        note = (f"{skipped} omitido(s): senha desconhecida (a API não devolve senhas)." if skipped else "")
        default = str(Path.cwd() / time.strftime("proxies-%Y%m%d-%H%M%S.txt"))

        def done(path):
            if not path:
                return
            try:
                written = write_private(path, lines)
            except FileExistsError:
                self.say(f"{path} já existe; escolha outro nome (não sobrescrevo).", error=True)
                return
            except OSError as exc:
                self.say(f"Não foi possível gravar {path}: {exc.strerror}.", error=True)
                return
            except Exception:
                # e.g. expanduser('~nobody-such/x') raises RuntimeError; never show locals.
                self.say(f"Não foi possível gravar {path}: caminho inválido.", error=True)
                return
            message = f"{len(lines)} linha(s) exportada(s) para {written} (0600)."
            self.say(message + (f" {note}" if note else ""), error=bool(skipped))

        self.push_screen(ExportDialog(default, note), done)

def main():
    client, error = None, None
    try:
        client = Client.from_env()
    except ApiError as exc:
        error = exc.args[0]
    seconds = os.environ.get("PROXY_TUI_REFRESH", "5")
    ProxyTui(client, error, float(seconds) if seconds.replace(".", "", 1).isdigit() else 5.0).run()
