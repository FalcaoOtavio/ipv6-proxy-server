"""Minimal HTTP client for the local proxy API (same token/env as the CLI).

Blocking stdlib calls; the TUI runs them in threads. Error bodies are never
parsed or shown: they can reflect secrets sent to the server.
"""
import http.client
import ipaddress
import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8000"
ENV_FILE = Path("/etc/ipv6-proxy-api.env")
MAX_BODY = 16 * 1024 * 1024
MAX_ENV = 64 * 1024
TIMEOUT = 60
# The API may take ~5 s per port in a batch; timing out earlier abandons a batch the server completes.
BATCH_SECONDS_PER_PROXY = 5
BATCH_MAX = 1000  # PROXY_BATCH_MAX default; a 422 reporting another limit updates it
LIMIT_RE = re.compile(rb"count above the configured limit \((\d{1,6})\)")

MESSAGES = {
    401: "Token inválido. Confira PROXY_API_TOKEN em /etc/ipv6-proxy-api.env e rode com sudo.",
    403: "Este token não pode agir sobre esse proxy.",
    404: "Proxy não encontrado: pode ter sido removido por outro cliente. Recarregue com a.",
    409: "Conflito: porta já em uso ou faixa de portas esgotada. Nada foi alterado.",
    412: "O proxy desta porta mudou desde a seleção (outro cliente). Nada foi alterado; recarregue com a.",
    422: "Entrada recusada pela API: verifique valores e a faixa de portas configurada.",
    428: "A API exige confirmação do alvo (If-Match). Recarregue com a e tente de novo.",
    503: "Serviço indisponível ou proxy fora do ar. Veja: sudo journalctl -u ipv6-proxy-api -n 50",
}

class ApiError(Exception):
    def __init__(self, message, status=None, limit=None):
        super().__init__(message)
        self.status = status
        self.limit = limit

class _Transport(Exception):
    """Request outcome unknown (connection lost or timeout)."""

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def validate_url(url):
    """Only plain HTTP to loopback: the bearer token travels in clear."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme != "http" or not loopback or parsed.path not in ("", "/") or parsed.query or parsed.username:
        raise ApiError(f"PROXY_TUI_API_URL inválida: use http://127.0.0.1:<porta> (recebido {url!r}).")
    return url.rstrip("/")

def load_token(path=ENV_FILE):
    """PROXY_API_TOKEN from the API env file, with the CLI's ownership/mode checks."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ApiError(f"Não foi possível ler {path}. Rode com sudo e verifique a instalação.") from exc
    try:
        meta = os.fstat(descriptor)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o077:
            raise ApiError(f"Arquivo inseguro: {path} deve ser regular, do root e com modo 0600.")
        data = os.read(descriptor, MAX_ENV + 1)
    except OSError as exc:
        raise ApiError(f"Falha ao ler {path}.") from exc
    finally:
        os.close(descriptor)
    if len(data) > MAX_ENV:
        raise ApiError(f"{path} excede {MAX_ENV} bytes.")
    for line in data.decode("ascii", "replace").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "PROXY_API_TOKEN":
            return check_token(value.strip())
    raise ApiError(f"PROXY_API_TOKEN ausente em {path}.")

def check_token(token):
    if len(token) < 32 or not re.fullmatch(r"[\x21-\x7e]+", token):
        raise ApiError("PROXY_API_TOKEN ausente, curto ou com caracteres inválidos.")
    return token

class Client:
    def __init__(self, url, token):
        self.url = validate_url(url)
        self.token = token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_env(cls, environ=os.environ):
        url = environ.get("PROXY_TUI_API_URL", DEFAULT_URL)
        token = environ.get("PROXY_API_TOKEN")
        return cls(url, check_token(token) if token else load_token())

    def _call(self, method, path, payload=None, if_match=None, timeout=TIMEOUT):
        """Mutations carry one Idempotency-Key and are retried once with it after a
        transport failure: the API replays the stored result instead of repeating it."""
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if if_match:
            headers["If-Match"] = if_match
        attempts = 1
        if method != "GET":
            headers["Idempotency-Key"] = str(uuid.uuid4())
            attempts = 2
        for attempt in range(attempts):
            try:
                return self._send(method, path, data, headers, timeout)
            except _Transport:
                if attempt + 1 < attempts:
                    continue
                message = f"API indisponível em {self.url}. Verifique: sudo systemctl status ipv6-proxy-api"
                if method != "GET":
                    message += " (a operação pode ter sido aplicada; recarregue com a antes de repetir)"
                raise ApiError(message) from None

    def _send(self, method, path, data, headers, timeout):
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as response:
                status, body = response.status, response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as exc:
            # Only a bare number is taken from an error body (they can reflect secrets).
            match = LIMIT_RE.search(exc.read(512)) if exc.code == 422 and path == "/proxies/batch" else None
            exc.close()
            if match:
                limit = int(match.group(1))
                raise ApiError(f"Quantidade acima do limite da API ({limit}, PROXY_BATCH_MAX).", 422, limit) from None
            raise ApiError(MESSAGES.get(exc.code, f"A API respondeu HTTP {exc.code}."), exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise _Transport from None
        if len(body) > MAX_BODY:
            raise ApiError("Resposta da API excedeu o limite permitido.")
        if not body:
            return status, None
        try:
            return status, json.loads(body)
        except ValueError:
            raise ApiError("A API retornou uma resposta inválida.") from None

    def create(self, payload):
        return self._call("POST", "/proxies", payload)[1]

    def batch(self, payload):
        timeout = TIMEOUT + BATCH_SECONDS_PER_PROXY * payload["count"]
        return self._call("POST", "/proxies/batch", payload, timeout=timeout)[1]

    def update(self, target, payload):
        return self._call("PATCH", f"/proxies/{target['port']}", payload, target["id"])[1]

    def rotate(self, target):
        return self._call("POST", f"/proxies/{target['port']}/rotate", None, target["id"])[1]

    def delete(self, target):
        self._call("DELETE", f"/proxies/{target['port']}", None, target["id"])

    def list_with_status(self):
        return self._call("GET", "/proxies?include=status")[1]
