from __future__ import annotations

import json
import urllib.error

from staragent import hub
from staragent.hub import NodeEntry


def remote_node(name: str = "worker") -> NodeEntry:
    return NodeEntry(name=name, url=f"http://{name}:8081", mode="lan")


def session_payload(name: str = "dev") -> dict[str, list[dict[str, object]]]:
    return {
        "sessions": [
            {
                "name": name,
                "agent": "codex",
                "session_type": "agent",
                "status": "idle",
                "repo": "/repo/project",
            }
        ]
    }


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_lan_node_request_bypasses_env_proxy(monkeypatch) -> None:
    node = remote_node()
    calls: list[tuple[str, object]] = []

    class FakeOpener:
        def open(self, request, timeout):  # type: ignore[no-untyped-def]
            calls.append(("opener", (request.full_url, timeout)))
            return FakeResponse(session_payload())

    def fake_proxy_handler(proxies):  # type: ignore[no-untyped-def]
        calls.append(("proxy_handler", proxies))
        return object()

    def fail_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("LAN node requests should not use urlopen's env proxy handling")

    monkeypatch.setattr(hub.urllib.request, "ProxyHandler", fake_proxy_handler)
    monkeypatch.setattr(hub.urllib.request, "build_opener", lambda handler: FakeOpener())
    monkeypatch.setattr(hub.urllib.request, "urlopen", fail_urlopen)

    payload = hub.request_json(node, "GET", "/api/sessions")

    assert payload == session_payload()
    assert ("proxy_handler", {}) in calls
    assert calls[-1] == (
        "opener",
        ("http://worker:8081/api/sessions", hub.NODE_REQUEST_TIMEOUT_SECONDS),
    )


def test_remote_node_request_keeps_default_proxy_handling(monkeypatch) -> None:
    node = NodeEntry(name="worker", url="http://worker:8081", mode="remote")
    calls: list[tuple[str, object]] = []

    def fail_build_opener(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("remote node requests should use urllib default proxy handling")

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        calls.append(("urlopen", (request.full_url, timeout)))
        return FakeResponse(session_payload())

    monkeypatch.setattr(hub.urllib.request, "build_opener", fail_build_opener)
    monkeypatch.setattr(hub.urllib.request, "urlopen", fake_urlopen)

    payload = hub.request_json(node, "GET", "/api/sessions")

    assert payload == session_payload()
    assert calls == [
        ("urlopen", ("http://worker:8081/api/sessions", hub.NODE_REQUEST_TIMEOUT_SECONDS))
    ]


def test_remote_node_uses_cached_heartbeat_during_transient_failure(monkeypatch) -> None:
    hub.clear_node_heartbeat_cache()
    node = remote_node()
    monkeypatch.setattr(hub, "request_json", lambda *args, **kwargs: session_payload())

    connected = hub.collect_node_view(node)

    assert connected.status == "connected"
    assert connected.session_count == 1

    def raise_timeout(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(hub, "request_json", raise_timeout)

    stale = hub.collect_node_view(node)

    assert stale.status == "stale"
    assert stale.session_count == 1
    assert stale.sessions[0].name == "dev"
    assert "last heartbeat" in stale.error
    assert "timed out" in stale.error


def test_remote_node_drops_stale_cache_after_grace_period(monkeypatch) -> None:
    hub.clear_node_heartbeat_cache()
    node = remote_node()
    monkeypatch.setattr(hub, "request_json", lambda *args, **kwargs: session_payload())
    hub.collect_node_view(node)

    with hub.NODE_HEARTBEATS_LOCK:
        hub.NODE_HEARTBEATS[node.name].last_success -= hub.NODE_HEARTBEAT_GRACE_SECONDS + 1

    def raise_timeout(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(hub, "request_json", raise_timeout)

    disconnected = hub.collect_node_view(node)

    assert disconnected.status == "disconnected"
    assert disconnected.session_count == 0


def test_remote_node_auth_failure_is_not_hidden_by_heartbeat_cache(monkeypatch) -> None:
    hub.clear_node_heartbeat_cache()
    node = remote_node()
    monkeypatch.setattr(hub, "request_json", lambda *args, **kwargs: session_payload())
    hub.collect_node_view(node)

    def raise_unauthorized(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(
            url=node.url or "",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr(hub, "request_json", raise_unauthorized)

    disconnected = hub.collect_node_view(node)

    assert disconnected.status == "disconnected"
    assert disconnected.session_count == 0
    assert "401" in disconnected.error
