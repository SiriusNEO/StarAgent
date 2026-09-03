from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from fastapi.testclient import TestClient

from staragent import agent_history, hub
from staragent.dashboard import app as dashboard_app
from staragent.node import app as node_app


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )


def test_codex_history_scan_returns_bounded_resume_metadata(monkeypatch, tmp_path) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    sessions_root = tmp_path / ".codex" / "sessions"
    index_path = tmp_path / ".codex" / "history.jsonl"
    write_jsonl(
        sessions_root / "2026" / "07" / "10" / f"rollout-test-{session_id}.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": "/work/Star Agent",
                    "cli_version": "0.144.0",
                    "timestamp": "2026-07-10T08:00:00Z",
                    "git": {"branch": "dev"},
                },
            },
            {"type": "response_item", "payload": {"private": "not returned"}},
        ],
    )
    write_jsonl(
        index_path,
        [
            {"session_id": session_id, "text": "First prompt", "ts": 1},
            {"session_id": session_id, "text": "Continue the dashboard work", "ts": 2},
        ],
    )
    monkeypatch.setattr(agent_history, "codex_sessions_root", lambda: sessions_root)
    monkeypatch.setattr(agent_history, "codex_history_index_path", lambda: index_path)
    agent_history.clear_agent_history_cache()

    payload = agent_history.agent_history_payload(agent="codex", force=True)

    assert len(payload["sessions"]) == 1
    session = payload["sessions"][0]
    assert session["id"] == session_id
    assert session["title"] == "Continue the dashboard work"
    assert session["prompt_count"] == 2
    assert session["git_branch"] == "dev"
    assert session["resume_command"] == (
        "codex resume -C '/work/Star Agent' 11111111-2222-4333-8444-555555555555"
    )
    assert "updated_epoch" not in session
    assert "source_path" not in session
    assert "private" not in json.dumps(session)


def test_claude_history_scan_finds_metadata_after_non_session_record(monkeypatch, tmp_path) -> None:
    session_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    projects_root = tmp_path / ".claude" / "projects"
    index_path = tmp_path / ".claude" / "history.jsonl"
    write_jsonl(
        projects_root / "-work-project" / f"{session_id}.jsonl",
        [
            {"type": "queue-operation", "operation": "enqueue"},
            {
                "type": "user",
                "sessionId": session_id,
                "cwd": "/work/project",
                "version": "2.1.138",
                "gitBranch": "main",
                "timestamp": "2026-07-10T09:00:00Z",
            },
        ],
    )
    write_jsonl(
        index_path,
        [
            {
                "sessionId": session_id,
                "display": "Fix the mobile layout",
                "project": "/work/project",
                "timestamp": 1,
            }
        ],
    )
    monkeypatch.setattr(agent_history, "claude_projects_root", lambda: projects_root)
    monkeypatch.setattr(agent_history, "claude_history_index_path", lambda: index_path)
    agent_history.clear_agent_history_cache()

    payload = agent_history.agent_history_payload(agent="claude", force=True)

    session = payload["sessions"][0]
    assert session["agent"] == "claude"
    assert session["title"] == "Fix the mobile layout"
    assert session["cli_version"] == "2.1.138"
    assert session["resume_command"] == (
        "cd /work/project && claude --resume aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    )


def test_resume_worker_command_preserves_the_selected_agent_preset() -> None:
    session_id = "11111111-2222-4333-8444-555555555555"

    assert agent_history.resume_worker_command(
        "codex", session_id, "/work/Star Agent", "codex --yolo"
    ) == ("codex resume --yolo -C '/work/Star Agent' 11111111-2222-4333-8444-555555555555")
    assert agent_history.resume_worker_command(
        "claude", session_id, "/work/project", "claude --dangerously-skip-permissions"
    ) == ("claude --dangerously-skip-permissions --resume 11111111-2222-4333-8444-555555555555")


def test_resume_worker_command_rejects_mismatched_cli_or_invalid_id() -> None:
    session_id = "11111111-2222-4333-8444-555555555555"

    for agent, candidate_id, command in (
        ("codex", session_id, "claude"),
        ("claude", "not-a-session", "claude"),
        ("opencode", session_id, "opencode"),
    ):
        try:
            agent_history.resume_worker_command(agent, candidate_id, "/work", command)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid resume command should be rejected")


def test_history_payload_rejects_unsupported_agents() -> None:
    try:
        agent_history.agent_history_payload(agent="unknown")
    except ValueError as exc:
        assert "not supported" in str(exc)
    else:
        raise AssertionError("unsupported history agent should fail")


def test_remote_history_payload_is_allowlisted_and_strips_extra_fields() -> None:
    payload = agent_history.normalize_agent_history_payload(
        {
            "supported": True,
            "sessions": [
                {
                    "id": "session-1",
                    "agent": "codex",
                    "title": "Visible preview",
                    "cwd": "/work",
                    "source_path": "/home/user/.codex/private.jsonl",
                    "resume_command": "codex resume session-1",
                },
                {"id": "rogue", "agent": "other", "title": "ignored"},
            ],
        }
    )

    assert len(payload["sessions"]) == 1
    assert "source_path" not in payload["sessions"][0]


def test_old_remote_node_reports_history_update_required(monkeypatch) -> None:
    node = hub.NodeEntry(name="old-worker", url="http://old-worker:8081", mode="lan")

    def missing_endpoint(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError("/api/agent-history", 404, "Not Found", {}, None)

    monkeypatch.setattr(hub, "request_json", missing_endpoint)

    payload = hub.node_agent_history_payload(node, refresh=True)

    assert payload["supported"] is False
    assert payload["node"] == "old-worker"
    assert payload["sessions"] == []
    assert "update required" in str(payload["error"]).lower()


def test_node_history_endpoint_is_authenticated_and_bounded(monkeypatch) -> None:
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    calls: list[tuple[str, int, bool]] = []

    def fake_history(agent="", limit=50, force=False):  # type: ignore[no-untyped-def]
        calls.append((agent, limit, force))
        return agent_history.unavailable_agent_history_payload("test")

    monkeypatch.setattr(node_app, "agent_history_payload", fake_history)
    client = TestClient(node_app.create_app())

    assert client.get("/api/agent-history").status_code == 401
    response = client.get(
        "/api/agent-history?agent=codex&limit=25&refresh=true",
        headers={"Authorization": "Bearer node-secret"},
    )

    assert response.status_code == 200
    assert calls == [("codex", 25, True)]


def test_dashboard_history_route_proxies_selected_node(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str, int, bool]] = []
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda name: node)

    def fake_payload(selected, agent="", limit=50, refresh=False):  # type: ignore[no-untyped-def]
        calls.append((selected.name, agent, limit, refresh))
        return agent_history.history_payload_with_node(
            agent_history.unavailable_agent_history_payload("test"), selected.name
        )

    monkeypatch.setattr(dashboard_app, "node_agent_history_payload", fake_payload)
    response = TestClient(dashboard_app.create_app()).get(
        "/api/nodes/worker/agent-history?agent=claude&limit=25&refresh=true"
    )

    assert response.status_code == 200
    assert response.json()["node"] == "worker"
    assert calls == [("worker", "claude", 25, True)]


def test_dashboard_worker_creation_resolves_structured_resume_on_the_hub(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    monkeypatch.setattr(
        dashboard_app,
        "node_by_name",
        lambda _name: hub.NodeEntry(name="local", url="local", mode="local"),
    )
    started = []
    monkeypatch.setattr(
        dashboard_app,
        "start_tmux_worker",
        lambda name, cwd, command: started.append((name, cwd, command)),
    )
    monkeypatch.setattr(dashboard_app, "append_node_event", lambda *args, **kwargs: None)
    session_id = "11111111-2222-4333-8444-555555555555"

    response = TestClient(dashboard_app.create_app()).post(
        "/api/workers",
        json={
            "node": "local",
            "name": "resume-demo",
            "cwd": "/work/Star Agent",
            "command": "codex --yolo",
            "resume": {"agent": "codex", "id": session_id},
        },
    )

    assert response.status_code == 200
    assert started == [
        (
            "resume-demo",
            "/work/Star Agent",
            "codex resume --yolo -C '/work/Star Agent' 11111111-2222-4333-8444-555555555555",
        )
    ]


def test_dashboard_resolves_resume_before_forwarding_to_an_older_remote_node(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda _name: node)
    forwarded = []

    def fake_request(selected, method, path, payload=None):  # type: ignore[no-untyped-def]
        forwarded.append((selected.name, method, path, payload))
        return {"status": "created", "name": payload["name"]}

    monkeypatch.setattr(dashboard_app, "request_json", fake_request)
    session_id = "11111111-2222-4333-8444-555555555555"

    response = TestClient(dashboard_app.create_app()).post(
        "/api/workers",
        json={
            "node": "worker",
            "name": "resume-remote",
            "cwd": "/work/project",
            "command": "claude --dangerously-skip-permissions",
            "resume": {"agent": "claude", "id": session_id},
        },
    )

    assert response.status_code == 200
    assert forwarded == [
        (
            "worker",
            "POST",
            "/api/workers",
            {
                "name": "resume-remote",
                "cwd": "/work/project",
                "command": (
                    "claude --dangerously-skip-permissions --resume "
                    "11111111-2222-4333-8444-555555555555"
                ),
            },
        )
    ]


def test_agents_page_renders_node_scoped_catalog_and_presets(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    node_view = hub.NodeView(
        entry=hub.NodeEntry(name="local", url="local", mode="local"),
        status="connected",
    )
    monkeypatch.setattr(
        dashboard_app,
        "dashboard_node_view",
        lambda node_id: node_view,
    )

    client = TestClient(dashboard_app.create_app())
    response = client.get("/nodes/local/agents")

    assert response.status_code == 200
    assert "Codex YOLO" in response.text
    assert "Gemini CLI" not in response.text
    assert "OpenCode" in response.text
    assert "Create Session" in response.text
    assert "Start from a preset" not in response.text
    assert 'href="/nodes/local/agents"' in response.text
    assert 'aria-label="Agent harnesses on local"' in response.text
    assert 'href="/nodes/local/agents/codex"' in response.text
    assert 'href="/nodes/local/agents/claude"' in response.text
    assert 'href="/nodes/local/agents/opencode"' in response.text
    assert 'class="agent-switcher-item agent-switcher-item-codex is-current"' in response.text
    assert response.text.count('class="agent-cli-card agent-cli-card-') == 1
    assert 'href="/nodes/local/sessions"' in response.text
    assert 'name="node" value="local"' in response.text

    claude = client.get("/nodes/local/agents/claude")
    missing = client.get("/nodes/local/agents/not-a-harness")

    assert claude.status_code == 200
    assert "<h1>Claude Code</h1>" in claude.text
    assert "Claude Skip Permissions" in claude.text
    assert "Codex YOLO" not in claude.text
    assert 'class="agent-switcher-item agent-switcher-item-claude is-current"' in claude.text
    assert 'class="agent-cli-card agent-cli-card-claude"' in claude.text
    assert 'name="agent" value="claude"' in claude.text
    assert missing.status_code == 404
