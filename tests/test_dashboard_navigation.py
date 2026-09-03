from __future__ import annotations

from fastapi.testclient import TestClient

from staragent import hub
from staragent.dashboard import app as dashboard_app
from staragent.models import SessionConfig, SessionView


def node_view(
    name: str,
    *,
    status: str = "connected",
    error: str = "",
    sessions: tuple[str, ...] = (),
) -> hub.NodeView:
    mode = "local" if name == "local" else "lan"
    url = "local" if name == "local" else f"http://{name}:8081"
    return hub.NodeView(
        entry=hub.NodeEntry(name=name, url=url, mode=mode),
        status=status,
        sessions=tuple(
            hub.HubSession(
                node_id=name,
                view=SessionView(SessionConfig(name=session, node=name)),
            )
            for session in sessions
        ),
        error=error,
    )


def dashboard_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    return TestClient(dashboard_app.create_app())


def test_dashboard_starts_with_node_connection_inventory(monkeypatch) -> None:
    views = [
        node_view("local"),
        node_view("worker", status="disconnected", error="connection refused"),
    ]
    monkeypatch.setattr(
        dashboard_app,
        "collect_session_navigation_nodes",
        lambda: views,
    )
    client = dashboard_client(monkeypatch)

    root = client.get("/", follow_redirects=False)
    page = client.get("/nodes")

    assert root.status_code == 303
    assert root.headers["location"] == "/nodes"
    assert page.status_code == 200
    assert "Available Nodes" in page.text
    assert "connection refused" in page.text
    assert 'class="pill node-status-connected">connected</span>' in page.text
    assert 'class="pill node-status-disconnected">disconnected</span>' in page.text
    assert 'href="/nodes/worker"' in page.text


def test_node_pages_keep_navigation_and_actions_in_node_scope(monkeypatch) -> None:
    worker = node_view("worker", sessions=("worker-only",))
    monkeypatch.setattr(dashboard_app, "dashboard_node_entry", lambda node_id: worker.entry)
    monkeypatch.setattr(dashboard_app, "dashboard_node_view", lambda node_id: worker)
    monkeypatch.setattr(
        dashboard_app,
        "collect_node_views",
        lambda prefer_cached=False: (_ for _ in ()).throw(
            AssertionError("Node workspace must not collect every Node")
        ),
    )
    client = dashboard_client(monkeypatch)

    node_root = client.get("/nodes/worker?focus=active", follow_redirects=False)
    sessions = client.get("/nodes/worker/sessions")

    assert node_root.status_code == 200
    assert "Everything below belongs to this Node only." in node_root.text
    assert sessions.status_code == 200
    assert "Manage live tmux sessions on worker." in sessions.text
    assert "worker-only" in sessions.text
    assert 'aria-label="Sessions on worker"' in sessions.text
    assert 'href="/nodes/worker/sessions/worker-only"' in sessions.text
    assert 'class="session-switcher-item"' in sessions.text
    assert 'href="/nodes/worker/sessions"' in sessions.text
    assert 'href="/nodes/worker/agents"' in sessions.text
    assert 'href="/nodes/worker/logs"' in sessions.text
    assert sessions.text.count('name="node" value="worker"') == 2
    assert 'href="/nodes/local/sessions"' not in sessions.text


def test_legacy_section_links_return_to_node_chooser(monkeypatch) -> None:
    client = dashboard_client(monkeypatch)

    sessions = client.get("/sessions?agent=codex&resume=abc", follow_redirects=False)
    agents = client.get("/agents", follow_redirects=False)
    logs = client.get("/logs?level=error", follow_redirects=False)

    assert sessions.headers["location"] == "/nodes"
    assert agents.headers["location"] == "/nodes"
    assert logs.headers["location"] == "/nodes"


def test_settings_moves_gallery_out_of_brand_menu_and_switches_language(monkeypatch) -> None:
    client = dashboard_client(monkeypatch)

    page = client.get("/settings")

    assert page.status_code == 200
    assert '<html lang="en">' in page.text
    assert 'class="brand-logo"' in page.text
    assert 'href="/settings"' in page.text
    assert 'data-theme-settings' in page.text
    assert 'class="theme-background-library"' in page.text
    assert 'class="brand-theme-button"' not in page.text
    assert 'class="theme-menu"' not in page.text
    assert client.get("/static/staragent-logo.svg").status_code == 200

    response = client.post("/api/settings/language", json={"language": "zh-CN"})
    translated = client.get("/settings")
    translated_nodes = client.get("/nodes")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "language": "zh-CN"}
    assert response.cookies["staragent_language"] == "zh-CN"
    assert '<html lang="zh-CN">' in translated.text
    assert "个性化当前浏览器中的 StarAgent" in translated.text
    assert "选择一个节点" in translated_nodes.text


def test_settings_rejects_unsupported_language(monkeypatch) -> None:
    client = dashboard_client(monkeypatch)

    response = client.post("/api/settings/language", json={"language": "fr"})

    assert response.status_code == 400


def test_log_source_picker_is_limited_to_selected_node(monkeypatch) -> None:
    sources = [
        {"id": "hub", "label": "Hub", "kind": "hub"},
        {"id": "node:local", "label": "Node · local", "kind": "node"},
        {"id": "node:worker", "label": "Node · worker", "kind": "node"},
        {"id": "node:other", "label": "Node · other", "kind": "node"},
    ]
    monkeypatch.setattr(dashboard_app, "log_source_payloads", lambda: sources)

    assert dashboard_app.node_log_source_payloads("worker") == [sources[2]]
    assert dashboard_app.node_log_source_payloads("local") == [sources[1], sources[0]]


def test_unknown_node_page_returns_not_found(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    client = dashboard_client(monkeypatch)

    response = client.get("/nodes/missing", follow_redirects=False)

    assert response.status_code == 404
