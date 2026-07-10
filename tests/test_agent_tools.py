from __future__ import annotations

import subprocess
import threading
import urllib.error

import pytest
from fastapi.testclient import TestClient

from staragent import agent_tools, agent_usage, hub
from staragent.dashboard import app as dashboard_app
from staragent.node import app as node_app


def tool_by_name(payload: dict[str, object], name: str) -> dict[str, object]:
    tools = payload.get("tools")
    assert isinstance(tools, list)
    return next(item for item in tools if isinstance(item, dict) and item.get("name") == name)


@pytest.fixture(autouse=True)
def stub_agent_usage_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools,
        "probe_agent_usage",
        lambda agent, executable: agent_usage.unknown_agent_usage(agent, "test usage"),
    )


def test_agent_tool_detection_reports_versions_in_parallel(monkeypatch) -> None:
    barrier = threading.Barrier(2)
    versions = {
        "codex": "codex-cli 1.2.3\n",
        "claude": "2.3.4 (Claude Code)\n",
    }
    monkeypatch.setattr(
        agent_tools.shutil,
        "which",
        lambda command: f"/tools/{command}" if command in versions else None,
    )

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        barrier.wait(timeout=1)
        assert kwargs["env"]["DISABLE_AUTOUPDATER"] == "1"
        assert kwargs["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        return subprocess.CompletedProcess(args, 0, versions[args[0].rsplit("/", 1)[-1]], "")

    monkeypatch.setattr(agent_tools.subprocess, "run", fake_run)
    agent_tools.clear_agent_tools_cache()

    payload = agent_tools.agent_tools_payload(force=True)

    assert tool_by_name(payload, "codex")["version"] == "codex-cli 1.2.3"
    assert tool_by_name(payload, "claude")["version"] == "2.3.4 (Claude Code)"
    assert tool_by_name(payload, "gemini")["status"] == "missing"
    assert tool_by_name(payload, "opencode")["status"] == "missing"


def test_agent_tool_detection_distinguishes_missing_and_broken(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools.shutil,
        "which",
        lambda command: None if command == "codex" else "/tools/claude",
    )
    monkeypatch.setattr(
        agent_tools.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "broken install"),
    )
    agent_tools.clear_agent_tools_cache()

    payload = agent_tools.agent_tools_payload(force=True)

    codex = tool_by_name(payload, "codex")
    claude = tool_by_name(payload, "claude")
    assert codex["status"] == "missing"
    assert codex["installed"] is False
    assert claude["status"] == "error"
    assert claude["installed"] is True
    assert claude["error"] == "broken install"


def test_agent_tool_detection_times_out_without_hanging(monkeypatch) -> None:
    monkeypatch.setattr(agent_tools.shutil, "which", lambda command: f"/tools/{command}")

    def timeout(args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(agent_tools.subprocess, "run", timeout)
    agent_tools.clear_agent_tools_cache()

    payload = agent_tools.agent_tools_payload(force=True)

    assert all(item["status"] == "error" for item in payload["tools"])
    assert all("timed out" in str(item["error"]) for item in payload["tools"])


def test_agent_tool_detection_uses_ttl_cache(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(agent_tools.shutil, "which", lambda command: f"/tools/{command}")

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 0, "version 1", "")

    monkeypatch.setattr(agent_tools.subprocess, "run", fake_run)
    agent_tools.clear_agent_tools_cache()

    first = agent_tools.agent_tools_payload(force=True)
    second = agent_tools.agent_tools_payload()

    assert first == second
    assert len(calls) == 4


def test_normalized_remote_payload_only_accepts_known_tools() -> None:
    normalized = agent_tools.normalize_agent_tools_payload(
        {
            "supported": True,
            "checked_at": "2026-07-10T12:00:00Z",
            "tools": [
                {
                    "name": "codex",
                    "status": "available",
                    "version": "codex 1",
                    "executable": "/bin/codex",
                },
                {"name": "rogue", "status": "available", "version": "ignored"},
            ],
        }
    )

    assert [item["name"] for item in normalized["tools"]] == [
        "codex",
        "claude",
        "gemini",
        "opencode",
    ]
    assert tool_by_name(normalized, "claude")["status"] == "unknown"


def test_agent_tool_status_detects_npm_and_exposes_safe_update_command() -> None:
    spec = agent_tools.agent_tool_spec("codex")
    assert spec is not None

    status = agent_tools.tool_status(
        spec,
        status="available",
        executable="/usr/local/bin/codex",
        resolved_executable="/usr/local/lib/node_modules/@openai/codex/bin/codex.js",
        version="codex-cli 1.2.3",
    )

    assert status["install_method"] == "npm"
    assert status["update_command"] == "npm install -g @openai/codex@latest"
    assert status["update_action"] == "update"


def test_agent_catalog_and_session_presets_cover_the_same_clis() -> None:
    from staragent.presets import command_presets_payload

    catalog = {item["name"] for item in agent_tools.agent_catalog_payload()}
    preset_agents = {
        item["agent"] for item in command_presets_payload() if item["agent"] != "shell"
    }

    assert catalog == {"codex", "claude", "gemini", "opencode"}
    assert preset_agents == catalog
    assert all("ops_compatible" in item for item in command_presets_payload())


def test_remote_session_heartbeat_caches_agent_tools(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    hub.clear_node_heartbeat_cache()
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    reported = agent_tools.unknown_agent_tools_payload("not installed", supported=True)
    monkeypatch.setattr(
        hub,
        "request_json",
        lambda *args, **kwargs: {"sessions": [], "agent_tools": reported},
    )

    assert hub.remote_sessions(node) == []
    cached = hub.cached_node_agent_tools(node)

    assert cached is not None
    assert cached["node"] == "worker"
    assert cached["supported"] is True


def test_capable_node_heartbeat_defers_cli_probe_until_agents_page(monkeypatch) -> None:
    hub.clear_node_heartbeat_cache()
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    monkeypatch.setattr(
        hub,
        "request_json",
        lambda *args, **kwargs: {
            "sessions": [],
            "capabilities": {"agent_tools": 1},
        },
    )

    assert hub.remote_sessions(node) == []
    assert hub.cached_node_agent_tools(node) is None


def test_old_remote_node_reports_update_required(monkeypatch) -> None:
    hub.clear_node_heartbeat_cache()
    node = hub.NodeEntry(name="old-worker", url="http://old-worker:8081", mode="lan")

    def missing_endpoint(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError("/api/agent-tools", 404, "Not Found", {}, None)

    monkeypatch.setattr(hub, "request_json", missing_endpoint)

    payload = hub.node_agent_tools_payload(node)

    assert payload["supported"] is False
    assert payload["node"] == "old-worker"
    assert "update required" in str(payload["error"]).lower()
    assert all(item["status"] == "unknown" for item in payload["tools"])


def test_old_node_heartbeat_caches_unsupported_detection(monkeypatch) -> None:
    hub.clear_node_heartbeat_cache()
    node = hub.NodeEntry(name="old-worker", url="http://old-worker:8081", mode="lan")
    monkeypatch.setattr(hub, "request_json", lambda *args, **kwargs: {"sessions": []})

    hub.remote_sessions(node)
    monkeypatch.setattr(
        hub,
        "request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cached legacy result should not make another request")
        ),
    )

    payload = hub.node_agent_tools_payload(node)

    assert payload["supported"] is False
    assert "update required" in str(payload["error"]).lower()


def test_remote_probe_failure_returns_stale_cached_result(monkeypatch) -> None:
    hub.clear_node_heartbeat_cache()
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    cached = {
        "supported": True,
        "checked_at": "2026-07-10T12:00:00Z",
        "tools": [
            {"name": "codex", "status": "available", "version": "codex 1"},
            {"name": "claude", "status": "missing", "error": "not found"},
        ],
    }
    hub.remember_node_agent_tools(node, cached)
    monkeypatch.setattr(
        hub,
        "request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )

    payload = hub.node_agent_tools_payload(node, refresh=True)

    assert payload["stale"] is True
    assert tool_by_name(payload, "codex")["status"] == "available"
    assert "offline" in str(payload["error"])


def test_cached_tool_result_is_stale_when_node_heartbeat_fails() -> None:
    hub.clear_node_heartbeat_cache()
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    hub.remember_node_agent_tools(
        node,
        {
            "supported": True,
            "tools": [
                {"name": "codex", "status": "available", "version": "codex 1"},
                {"name": "claude", "status": "available", "version": "claude 1"},
            ],
        },
    )
    with hub.NODE_HEARTBEATS_LOCK:
        hub.NODE_HEARTBEATS[node.name] = hub.NodeHeartbeat(
            endpoint=node.url or "",
            sessions=(),
            last_success=hub.time.monotonic(),
            failures=1,
            last_error="offline",
        )

    payload = hub.cached_node_agent_tools(node)

    assert payload is not None
    assert payload["stale"] is True


def test_node_agent_tools_endpoint_is_authenticated(monkeypatch) -> None:
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    monkeypatch.setattr(
        node_app,
        "agent_tools_payload",
        lambda force=False: agent_tools.unknown_agent_tools_payload("test", supported=True),
    )
    monkeypatch.setattr(node_app, "collect_session_views", lambda: [])
    client = TestClient(node_app.create_app())

    assert client.get("/api/agent-tools").status_code == 401
    response = client.get(
        "/api/agent-tools?refresh=true",
        headers={"Authorization": "Bearer node-secret"},
    )

    assert response.status_code == 200
    assert response.json()["supported"] is True
    sessions = client.get(
        "/api/sessions",
        headers={"Authorization": "Bearer node-secret"},
    ).json()
    assert sessions["capabilities"]["agent_tools"] == 2
    assert sessions["capabilities"]["agent_usage"] == 1
    assert sessions["capabilities"]["agent_history"] == 1
    assert "agent_tools" not in sessions


def test_dashboard_agent_tools_route_supports_manual_refresh(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    node = hub.NodeEntry(name="local", url="local", mode="local")
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda name: node)

    def fake_payload(selected, refresh=False):  # type: ignore[no-untyped-def]
        calls.append((selected.name, refresh))
        return agent_tools.payload_with_node(
            agent_tools.unknown_agent_tools_payload("test", supported=True),
            selected.name,
        )

    monkeypatch.setattr(dashboard_app, "node_agent_tools_payload", fake_payload)
    response = TestClient(dashboard_app.create_app()).get(
        "/api/nodes/local/agent-tools?refresh=true"
    )

    assert response.status_code == 200
    assert response.json()["node"] == "local"
    assert calls == [("local", True)]
