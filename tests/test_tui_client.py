"""TUI API client: idempotent retry of mutations, batch timeout, URL/token guards (sync, no Textual)."""
import pytest

from proxy_tui import client as client_module
from proxy_tui.app import export_lines
from proxy_tui.client import ApiError, Client, validate_url
from test_tui import TOKEN, api  # noqa: F401  (fixture)

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

def test_a7_mutation_retries_lost_response_with_same_key(api):
    """The first response is lost after the server applied it: the retry replays, nothing duplicates."""
    sent = []
    real_open = api.client._opener.open

    def flaky_open(request, timeout):
        sent.append((request.get_header("Idempotency-key"), timeout))
        response = real_open(request, timeout=timeout)
        if len(sent) == 1:
            response.close()
            raise TimeoutError("response lost")
        return response

    api.client._opener.open = flaky_open
    created = api.client.batch({"count": 3, "credentials": "none"})
    assert len(sent) == 2 and sent[0][0] == sent[1][0] and sent[0][0]
    assert sent[0][1] == client_module.TIMEOUT + client_module.BATCH_SECONDS_PER_PROXY * 3
    assert len(created["created"]) == 3 and len(api.records()) == 3
    sent.clear()
    api.client.create({"port": 30050})
    api.client.create({"port": 30051})
    keys = [k for k, _ in sent]
    assert len(set(keys)) == 2  # one fresh key per mutation

def test_a7_mutation_gives_up_after_one_retry(api):
    calls = []
    def dead(request, timeout):
        calls.append(request.get_header("Idempotency-key"))
        raise TimeoutError
    api.client._opener.open = dead
    with pytest.raises(ApiError, match="pode ter sido aplicada"):
        api.client.rotate({"port": 30000, "id": "a" * 32})
    assert len(calls) == 2 and calls[0] == calls[1]
    calls.clear()
    with pytest.raises(ApiError):
        api.client.list_with_status()
    assert calls == [None]  # reads carry no key and are not retried
