"""Modal dialogs. Each returns a validated payload (or None on Esc); none calls the API."""
import re

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9_.!@%+=-]{1,128}$")
PROTOCOLS = [("HTTP", "http"), ("SOCKS5", "socks5")]
CREDENTIAL_MODES = [("Sem autenticação", "none"), ("Comum (mesmo usuário/senha)", "common"),
                    ("Aleatória por proxy", "random")]
HELP = """\
[b]Atalhos[/b]
  ↑ ↓      mover seleção          a   recarregar lista
  c        criar proxy            l   criar lote
  e        editar selecionado     r   rotacionar IPv6
  d        excluir (confirma)     s   ver/ocultar senha
  x        exportar lista         ?   esta ajuda
  Esc      fechar diálogo         q   sair

[b]Status[/b]
  ● ativo   ◐ subindo   ✕ falhou
  ■ isolado (reinicie a API)

[b]Senhas[/b]
A API nunca devolve senhas. A TUI conhece só as senhas definidas nesta sessão (criar, editar, lote aleatório) e as guarda apenas em memória. Coluna Senha: •••• oculta, n/d desconhecida, — sem autenticação.

Cada alteração reinicia só o 3proxy da porta afetada e derruba as sessões dela; as demais portas não são tocadas. O alvo é fixado ao abrir a ação: mudar a seleção depois não muda o proxy afetado."""

def parse_port(text, required=False):
    """None when blank and optional; raises ValueError with a PT-BR message otherwise."""
    text = text.strip()
    if not text:
        if required:
            raise ValueError("Informe a porta.")
        return None
    if not text.isdigit() or not 1024 <= int(text) <= 65535:
        raise ValueError("Porta deve ser um número entre 1024 e 65535.")
    return int(text)

def check_credentials(username, password, password_required=True):
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Usuário: 1-64 caracteres entre letras, números, _ . -")
    if password_required and not PASSWORD_RE.fullmatch(password):
        raise ValueError("Senha: 1-128 caracteres entre letras, números e _ . ! @ % + = -")

class Dialog(ModalScreen):
    """Scrollable centered box (fits 60x20) with an inline error line."""
    BINDINGS = [Binding("escape", "cancel", "Cancelar")]
    DEFAULT_CSS = """
    Dialog { align: center middle; }
    Dialog > VerticalScroll {
        width: 64; max-width: 100%; height: auto; max-height: 100%;
        border: round $primary; background: $surface; padding: 0 1;
    }
    Dialog .title { text-style: bold; }
    Dialog .error { color: $error; text-style: bold; }
    Dialog Input, Dialog Select { margin-bottom: 0; }
    Dialog Horizontal { height: auto; }
    Dialog Button { margin-right: 1; }
    """
    title_text = ""

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(self.title_text, classes="title", markup=False)
            yield from self.fields()
            yield Static("", id="error", classes="error", markup=False)
            with Horizontal():
                yield Button("Confirmar", variant="primary", id="ok", compact=True)
                yield Button("Cancelar", id="cancel", compact=True)

    def fields(self):
        return []

    def error(self, message):
        self.query_one("#error", Static).update("✕ " + message)

    def value(self, selector):
        return self.query_one(selector).value

    def action_cancel(self):
        self.dismiss(None)

    def on_button_pressed(self, event):
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self.submit()

    def on_input_submitted(self, event):
        self.submit()

    def submit(self):
        try:
            result = self.build()
        except ValueError as exc:
            self.error(str(exc))
            return
        self.dismiss(result)

class CreateDialog(Dialog):
    title_text = "Criar proxy"

    def fields(self):
        yield Label("Porta (vazio = primeira livre)")
        yield Input(id="port", placeholder="30000", compact=True)
        yield Label("Protocolo")
        yield Select(PROTOCOLS, value="http", allow_blank=False, id="protocol", compact=True)
        yield Label("Usuário (vazio = sem autenticação)")
        yield Input(id="username", compact=True)
        yield Label("Senha")
        yield Input(id="password", password=True, compact=True)

    def build(self):
        payload = {"protocol": self.value("#protocol")}
        port = parse_port(self.value("#port"))
        if port is not None:
            payload["port"] = port
        username, password = self.value("#username").strip(), self.value("#password")
        if username or password:
            check_credentials(username, password)
            payload.update(username=username, password=password)
        return payload

class BatchDialog(Dialog):
    title_text = "Criar lote"

    def __init__(self, max_count=1000):
        super().__init__()
        self.max_count = max_count

    def fields(self):
        yield Label("Quantidade")
        yield Input(id="count", value="10", compact=True)
        yield Label("Porta inicial (vazio = início da faixa)")
        yield Input(id="start", compact=True)
        yield Label("Protocolo")
        yield Select(PROTOCOLS, value="http", allow_blank=False, id="protocol", compact=True)
        yield Label("Credenciais")
        yield Select(CREDENTIAL_MODES, value="none", allow_blank=False, id="mode", compact=True)
        yield Label("Usuário (comum) ou prefixo (aleatória, opcional)")
        yield Input(id="username", compact=True)
        yield Label("Senha (só no modo comum)")
        yield Input(id="password", password=True, compact=True)
        yield Checkbox("Parcial: manter os criados se alguma porta falhar", id="partial", compact=True)

    def build(self):
        count = self.value("#count").strip()
        if not count.isdigit() or not 1 <= int(count) <= self.max_count:
            raise ValueError(f"Quantidade deve ser um número entre 1 e {self.max_count} (PROXY_BATCH_MAX).")
        mode = self.value("#mode")
        payload = {"count": int(count), "protocol": self.value("#protocol"), "credentials": mode,
                   "partial": self.value("#partial")}
        start = parse_port(self.value("#start"))
        if start is not None:
            payload["start_port"] = start
        username, password = self.value("#username").strip(), self.value("#password")
        if mode == "none" and (username or password):
            raise ValueError("Sem autenticação: deixe usuário e senha vazios.")
        if mode == "common":
            check_credentials(username, password)
            payload.update(username=username, password=password)
        if mode == "random":
            if password:
                raise ValueError("Modo aleatório gera as senhas: deixe a senha vazia.")
            if username:
                if len(username) > 58:
                    raise ValueError("Prefixo do usuário: no máximo 58 caracteres.")
                check_credentials(username, "", password_required=False)
                payload["username"] = username
        return payload

class EditDialog(Dialog):
    def __init__(self, target):
        super().__init__()
        self.target = target
        self.title_text = f"Editar proxy da porta {target['port']}"

    def fields(self):
        target = self.target
        yield Label("Porta")
        yield Input(str(target["port"]), id="port", compact=True)
        yield Label("Protocolo")
        yield Select(PROTOCOLS, value=target["protocol"], allow_blank=False, id="protocol", compact=True)
        yield Label("Usuário")
        yield Input(target.get("username") or "", id="username", compact=True)
        yield Label("Nova senha (vazio = manter credenciais)")
        yield Input(id="password", password=True, compact=True)
        yield Checkbox("Remover autenticação", id="noauth", compact=True)

    def build(self):
        target, payload = self.target, {}
        port = parse_port(self.value("#port"), required=True)
        if port != target["port"]:
            payload["port"] = port
        if self.value("#protocol") != target["protocol"]:
            payload["protocol"] = self.value("#protocol")
        username, password = self.value("#username").strip(), self.value("#password")
        if self.value("#noauth"):
            if target.get("username") is not None:
                payload.update(username=None, password=None)
        elif password:
            check_credentials(username, password)
            payload.update(username=username, password=password)
        elif username != (target.get("username") or ""):
            raise ValueError("Para mudar o usuário informe também a nova senha.")
        if not payload:
            raise ValueError("Nada foi alterado.")
        return payload

class ConfirmDialog(Dialog):
    """Asks to type the target port: guards delete against a wrong selection."""

    def __init__(self, title, port, typed=True):
        super().__init__()
        self.title_text, self.port, self.typed = title, port, typed

    def fields(self):
        yield Static("Só o 3proxy desta porta reinicia; as sessões dela caem.")
        if self.typed:
            yield Label(f"Digite {self.port} para confirmar", markup=False)
            yield Input(id="confirm", compact=True)

    def on_mount(self):
        if not self.typed:
            self.query_one("#ok").focus()

    def build(self):
        if self.typed and self.value("#confirm").strip() != str(self.port):
            raise ValueError(f"Digite exatamente {self.port}; nada foi alterado.")
        return True

class ExportDialog(Dialog):
    title_text = "Exportar lista (IPv4:porta:usuário:senha)"

    def __init__(self, default_path, note):
        super().__init__()
        self.default_path, self.note = default_path, note

    def fields(self):
        if self.note:
            yield Static(self.note, markup=False)
        yield Label("Arquivo (criado com permissão 0600; não sobrescreve)")
        yield Input(self.default_path, id="path", compact=True)

    def build(self):
        path = self.value("#path").strip()
        if not path:
            raise ValueError("Informe o caminho do arquivo.")
        return path

class MessageDialog(Dialog):
    def __init__(self, title, body):
        super().__init__()
        self.title_text, self.body = title, body

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(self.title_text, classes="title", markup=False)
            yield Static(self.body, markup=False)
            yield Button("Fechar (Esc)", id="cancel", compact=True)

    def on_mount(self):
        self.query_one("#cancel").focus()

class HelpDialog(Dialog):
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(HELP)
            yield Button("Fechar (Esc)", id="cancel", compact=True)

    def on_mount(self):
        self.query_one(VerticalScroll).focus()  # start at the top; arrows scroll
