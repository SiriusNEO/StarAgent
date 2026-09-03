from __future__ import annotations

import json

from fastapi.testclient import TestClient

from staragent import event_log, hub, main, service_supervisor
from staragent.dashboard.app import create_app as create_dashboard_app
from staragent.node.app import create_app as create_node_app


def test_hub_and_node_logs_are_stored_separately(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))

    event_log.append_hub_event("info", "hub.ready", "Hub ready")
    event_log.append_node_event("worker/a", "warning", "node.stale", "Node stale")

    hub_events = event_log.read_hub_events()
    node_events = event_log.read_node_events("worker/a")
    assert [item["event"] for item in hub_events] == ["hub.ready"]
    assert [item["event"] for item in node_events] == ["node.stale"]
    assert event_log.hub_log_path().parent == tmp_path / "logs"
    assert event_log.node_log_path("worker/a").parent == tmp_path / "logs" / "nodes"


def test_log_values_are_redacted_before_writing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))

    event_log.append_hub_event(
        "error",
        "request.failed",
        "GET /ws?token=top-secret Authorization: Bearer bearer-secret",
        details={"api_key": "key-secret", "safe": "visible"},
    )

    raw = event_log.hub_log_path().read_text(encoding="utf-8")
    assert "top-secret" not in raw
    assert "bearer-secret" not in raw
    assert "key-secret" not in raw
    item = event_log.read_hub_events()[0]
    assert item["details"] == {"api_key": "[REDACTED]", "safe": "visible"}


def test_node_outbox_supports_incremental_reads(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    events = [
        event_log.append_node_outbox_event("info", f"event.{index}", f"message {index}")
        for index in range(3)
    ]

    first = event_log.node_outbox_payload(limit=2)
    assert [item["event"] for item in first["events"]] == ["event.0", "event.1"]
    assert first["has_more"] is True

    second = event_log.node_outbox_payload(after=str(events[1]["id"]), limit=2)
    assert [item["event"] for item in second["events"]] == ["event.2"]
    assert second["has_more"] is False


def test_node_event_ingestion_is_persistently_deduplicated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    source = event_log.event_payload(
        "error",
        "service.exited",
        "Node exited with code 137",
        source="node.supervisor",
    )

    assert event_log.ingest_node_events("worker", [source]) == 1
    assert event_log.ingest_node_events("worker", [source]) == 0
    assert [item["event"] for item in event_log.read_node_events("worker")] == ["service.exited"]
    cursors = json.loads(event_log.log_cursors_path().read_text(encoding="utf-8"))
    assert cursors["nodes"]["worker"]["cursor"] == source["id"]


def test_log_rotation_keeps_a_bounded_backup(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    path = event_log.hub_log_path()
    first = event_log.event_payload("info", "first", "x" * 120, source="test")
    second = event_log.event_payload("info", "second", "y" * 120, source="test")

    event_log.append_event(path, first, max_bytes=300, backups=1)
    event_log.append_event(path, second, max_bytes=300, backups=1)

    assert path.exists()
    assert path.with_name("hub.jsonl.1").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["event"] == "second"


def test_remote_node_logs_sync_when_capability_is_advertised(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    remote_event = event_log.event_payload(
        "error",
        "service.exited",
        "Node exited unexpectedly",
        source="node.supervisor",
    )
    calls: list[str] = []

    def fake_request_json(node, method, path, body=None, timeout=0):  # type: ignore[no-untyped-def]
        calls.append(path)
        if path == "/api/sessions":
            return {"sessions": [], "capabilities": {"logs": 1}}
        if path.startswith("/api/logs?"):
            return {
                "events": [remote_event],
                "next_cursor": remote_event["id"],
                "has_more": False,
            }
        raise AssertionError(path)

    monkeypatch.setattr(hub, "request_json", fake_request_json)

    assert hub.remote_sessions(node) == []
    assert calls[0] == "/api/sessions"
    assert calls[1].startswith("/api/logs?")
    assert event_log.read_node_events("worker")[0]["event"] == "service.exited"


def test_node_connection_events_only_log_state_transitions(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    hub.clear_node_heartbeat_cache()
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")

    hub.report_node_connection_state(node, "connected")
    hub.report_node_connection_state(node, "connected")
    hub.report_node_connection_state(node, "disconnected", "connection refused")

    assert [item["event"] for item in event_log.read_node_events("worker")] == [
        "node.disconnected",
        "node.connected",
    ]


def test_dashboard_logs_page_and_api(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    event_log.append_hub_event("info", "hub.ready", "Hub ready")
    client = TestClient(create_dashboard_app())

    page = client.get("/nodes/local/logs")
    response = client.get("/api/logs?source=hub")

    assert page.status_code == 200
    assert "Hub-centralized service events" in page.text
    assert response.status_code == 200
    assert response.json()["events"][0]["event"] == "hub.ready"


def test_node_log_api_requires_auth_and_returns_outbox(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    event_log.append_node_outbox_event("warning", "node.test", "Test event")
    client = TestClient(create_node_app())

    assert client.get("/api/logs").status_code == 401
    response = client.get(
        "/api/logs",
        headers={"Authorization": "Bearer node-secret"},
    )

    assert response.status_code == 200
    assert response.json()["events"][0]["event"] == "node.test"


def test_node_server_disables_uvicorn_access_log(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(main, "ensure_dependencies", lambda: None)
    monkeypatch.setattr(main, "remote_node_token", lambda: "secret")
    monkeypatch.setattr(main.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))

    main.run_node("127.0.0.1", 8081, False)

    assert calls[0]["access_log"] is False


def test_supervisor_restart_delay_is_bounded() -> None:
    assert service_supervisor.restart_delay(1) == 2.0
    assert service_supervisor.restart_delay(2) == 4.0
    assert service_supervisor.restart_delay(100) == 30.0
