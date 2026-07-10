from __future__ import annotations

import json
import subprocess
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest
from typer.testing import CliRunner

from staragent import __version__, hub
from staragent import main as staragent_main
from staragent.hub import NodeEntry, normalize_node_url

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_hub_test_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path / "state"))


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


def test_normalize_node_url_defaults_to_8081() -> None:
    assert normalize_node_url("worker") == "http://worker:8081"
    assert normalize_node_url("100.64.1.10") == "http://100.64.1.10:8081"


def test_normalize_node_url_preserves_explicit_port() -> None:
    assert normalize_node_url("worker:8082") == "http://worker:8082"
    assert normalize_node_url("http://100.64.1.10:8082") == "http://100.64.1.10:8082"


def test_concurrent_node_updates_are_atomic(monkeypatch, tmp_path) -> None:
    nodes_path = tmp_path / "nodes.json"
    monkeypatch.setattr(hub, "NODES_PATH", nodes_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda index: hub.add_node(f"worker-{index}", f"worker-{index}:8081"),
                range(20),
            )
        )

    names = {node.name for node in hub.persisted_nodes()}
    assert names == {"local", *(f"worker-{index}" for index in range(20))}
    assert json.loads(nodes_path.read_text(encoding="utf-8"))["nodes"]
    assert not list(tmp_path.glob("*.tmp"))


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


def test_preferred_remote_cache_skips_network_request(monkeypatch) -> None:
    hub.clear_node_heartbeat_cache()
    node = remote_node()
    monkeypatch.setattr(hub, "request_json", lambda *args, **kwargs: session_payload())
    hub.collect_node_view(node)

    monkeypatch.setattr(
        hub,
        "remote_sessions",
        lambda node: (_ for _ in ()).throw(AssertionError("cached view should not hit network")),
    )

    cached = hub.collect_node_view(node, prefer_cached=True)

    assert cached.status == "connected"
    assert cached.session_count == 1
    assert cached.sessions[0].name == "dev"


def test_remote_node_keeps_cached_sessions_when_health_is_ok(monkeypatch) -> None:
    hub.clear_node_heartbeat_cache()
    node = remote_node()
    monkeypatch.setattr(hub, "request_json", lambda *args, **kwargs: session_payload())
    hub.collect_node_view(node)

    calls: list[str] = []

    def slow_sessions_healthy_node(node, method, path, body=None, timeout=0):  # type: ignore[no-untyped-def]
        calls.append(path)
        if path == "/api/health":
            return {"status": "ok"}
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(hub, "request_json", slow_sessions_healthy_node)

    stale = hub.collect_node_view(node)

    assert calls == ["/api/sessions", "/api/health"]
    assert stale.status == "stale"
    assert stale.session_count == 1
    assert stale.sessions[0].name == "dev"
    assert "health ok" in stale.error
    assert "sessions unavailable" in stale.error


def test_remote_node_reports_stale_without_sessions_when_health_is_ok(monkeypatch) -> None:
    hub.clear_node_heartbeat_cache()
    node = remote_node()

    def slow_sessions_healthy_node(node, method, path, body=None, timeout=0):  # type: ignore[no-untyped-def]
        if path == "/api/health":
            return {"status": "ok"}
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(hub, "request_json", slow_sessions_healthy_node)

    stale = hub.collect_node_view(node)

    assert stale.status == "stale"
    assert stale.session_count == 0
    assert "health ok" in stale.error


def test_local_session_lookup_does_not_collect_every_session(monkeypatch) -> None:
    view = object()
    monkeypatch.setattr(hub, "collect_session_view", lambda name: view if name == "dev" else None)
    monkeypatch.setattr(
        hub,
        "collect_node_view",
        lambda node: (_ for _ in ()).throw(AssertionError("full node scan should not run")),
    )

    result = hub.collect_node_session(NodeEntry(name="local", url="local", mode="local"), "dev")

    assert result is not None
    assert result.view is view


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


def test_verify_node_command_checks_health_and_sessions(monkeypatch) -> None:
    calls: list[tuple[str, str, float, str, str]] = []

    def fake_request_json(node, method, path, body=None, timeout=0):  # type: ignore[no-untyped-def]
        calls.append((node.url, method, timeout, node.mode, path))
        if path == "/api/health":
            return {"status": "ok"}
        if path == "/api/sessions":
            return {"sessions": [{"name": "dev"}, {"name": "docs"}]}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(staragent_main, "request_json", fake_request_json)

    result = runner.invoke(
        staragent_main.app,
        ["verify-node", "worker", "--timeout", "2.5"],
    )

    assert result.exit_code == 0
    assert calls == [
        ("http://worker:8081", "GET", 2.5, "lan", "/api/health"),
        ("http://worker:8081", "GET", 2.5, "lan", "/api/sessions"),
    ]
    assert "Health: ok" in result.output
    assert "Sessions: ok (2 sessions)" in result.output
    assert "http://worker:8081" in result.output


def test_verify_node_command_reports_auth_failure(monkeypatch) -> None:
    def fake_request_json(node, method, path, body=None, timeout=0):  # type: ignore[no-untyped-def]
        if path == "/api/health":
            return {"status": "ok"}
        raise urllib.error.HTTPError(
            url=node.url or "",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr(staragent_main, "request_json", fake_request_json)

    result = runner.invoke(staragent_main.app, ["verify-node", "http://worker:8081"])

    assert result.exit_code == 1
    assert "Health: ok" in result.output
    assert "Sessions: failed" in result.output
    assert "Check STARAGENT_AUTH_TOKEN or STARAGENT_NODE_TOKEN" in result.output


def test_version_command_prints_project_version() -> None:
    result = runner.invoke(staragent_main.app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_dashboard_command_is_hidden_from_public_help() -> None:
    result = runner.invoke(staragent_main.app, ["--help"])

    assert result.exit_code == 0
    assert "hub" in result.output
    assert "│ dashboard" not in result.output


def test_node_command_starts_node_in_tmux(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr(staragent_main, "ensure_dependencies", lambda: None)
    monkeypatch.setattr(staragent_main, "remote_node_token", lambda: "secret")
    monkeypatch.setattr(staragent_main, "staragent_executable", lambda: "/bin/staragent")

    def fake_ensure_tmux_session(session, cwd, command):  # type: ignore[no-untyped-def]
        calls.append((session, cwd, command))

    monkeypatch.setattr(staragent_main, "ensure_tmux_session", fake_ensure_tmux_session)

    result = runner.invoke(
        staragent_main.app,
        ["node", "--host", "127.0.0.1", "--port", "8082", "--session", "node-a"],
    )

    assert result.exit_code == 0
    assert calls
    session, _cwd, command = calls[0]
    assert session == "node-a"
    assert "/bin/staragent node --host 127.0.0.1 --port 8082" in command
    assert "StarAgent node: tmux session node-a" in result.output


def test_node_ts_command_starts_tmux_and_serves(monkeypatch) -> None:
    tmux_calls: list[tuple[str, str, str]] = []
    run_calls: list[list[str]] = []

    monkeypatch.setattr(staragent_main, "ensure_dependencies", lambda: None)
    monkeypatch.setattr(staragent_main, "remote_node_token", lambda: "secret")
    monkeypatch.setattr(staragent_main, "staragent_executable", lambda: "/bin/staragent")
    monkeypatch.setattr(
        staragent_main, "ensure_tmux_session", lambda *args: tmux_calls.append(args)
    )

    def fake_which(name):  # type: ignore[no-untyped-def]
        return f"/usr/bin/{name}"

    def fake_run(command, check, text, capture_output):  # type: ignore[no-untyped-def]
        run_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(staragent_main.shutil, "which", fake_which)
    monkeypatch.setattr(staragent_main.subprocess, "run", fake_run)

    result = runner.invoke(
        staragent_main.app,
        [
            "node-ts",
            "--port",
            "8082",
            "--serve-port",
            "18082",
            "--tailscale-socket",
            ".staragent/tailscaled.sock",
        ],
    )

    assert result.exit_code == 0
    assert tmux_calls
    assert run_calls == [
        [
            "/usr/bin/tailscale",
            "--socket",
            ".staragent/tailscaled.sock",
            "status",
        ],
        [
            "/usr/bin/tailscale",
            "--socket",
            ".staragent/tailscaled.sock",
            "serve",
            "--bg",
            "--tcp=18082",
            "tcp://127.0.0.1:8082",
        ],
    ]
    assert "Tailscale serve: tcp/18082 -> 127.0.0.1:8082" in result.output


def test_node_ts_command_stops_before_tmux_when_tailscale_not_ready(monkeypatch) -> None:
    tmux_calls: list[tuple[str, str, str]] = []
    run_calls: list[list[str]] = []

    monkeypatch.setattr(staragent_main, "ensure_dependencies", lambda: None)
    monkeypatch.setattr(staragent_main, "remote_node_token", lambda: "secret")
    monkeypatch.setattr(staragent_main, "staragent_executable", lambda: "/bin/staragent")
    monkeypatch.setattr(
        staragent_main, "ensure_tmux_session", lambda *args: tmux_calls.append(args)
    )
    monkeypatch.setattr(staragent_main.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(command, check, text, capture_output):  # type: ignore[no-untyped-def]
        run_calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "Logged out.")

    monkeypatch.setattr(staragent_main.subprocess, "run", fake_run)

    result = runner.invoke(staragent_main.app, ["node-ts"])

    assert result.exit_code == 1
    assert run_calls == [["/usr/bin/tailscale", "status"]]
    assert not tmux_calls
    assert "Tailscale is not ready." in result.output
    assert "tailscale up --ssh" in result.output


def test_node_ts_command_stops_before_tmux_when_tailscale_missing(monkeypatch) -> None:
    tmux_calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr(staragent_main, "ensure_dependencies", lambda: None)
    monkeypatch.setattr(staragent_main, "remote_node_token", lambda: "secret")
    monkeypatch.setattr(
        staragent_main, "ensure_tmux_session", lambda *args: tmux_calls.append(args)
    )
    monkeypatch.setattr(staragent_main.shutil, "which", lambda name: None)

    result = runner.invoke(staragent_main.app, ["node-ts"])

    assert result.exit_code == 1
    assert not tmux_calls
    assert "tailscale command not found" in result.output
