from __future__ import annotations

from fastapi.testclient import TestClient

from staragent import hub
from staragent.dashboard import app as dashboard_app
from staragent.models import SessionConfig, SessionView
from staragent.self_update import StarAgentUpdateError


def node_view(
    name: str,
    *,
    status: str = "connected",
    error: str = "",
    sessions: tuple[str, ...] = (),
    runtime: dict[str, object] | None = None,
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
        runtime=runtime or {},
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


def test_launcher_opens_the_local_harness_workspace_without_hub_navigation(monkeypatch) -> None:
    local = node_view("local")
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    monkeypatch.setattr(dashboard_app, "dashboard_node_view", lambda node_id: local)
    client = TestClient(dashboard_app.create_app(mode="launcher"))

    root = client.get("/", follow_redirects=False)
    nodes = client.get("/nodes", follow_redirects=False)
    agents = client.get("/nodes/local/agents/codex")
    remote = client.get("/nodes/worker/agents")

    assert root.headers["location"] == "/nodes/local/agents"
    assert nodes.headers["location"] == "/nodes/local/agents"
    assert agents.status_code == 200
    assert '<body data-dashboard-mode="launcher">' in agents.text
    assert "StarAgent Launcher" in agents.text
    assert 'href="/nodes/local/agents" aria-label="StarAgent"' in agents.text
    assert 'href="/nodes">Nodes</a>' not in agents.text
    assert 'class="node-workspace-back"' not in agents.text
    assert 'class="node-workspace-node-card"' in agents.text
    assert 'href="/nodes/local"' in agents.text
    assert ">Overview</span>" not in agents.text
    assert remote.status_code == 404


def test_launcher_does_not_start_remote_node_heartbeats(monkeypatch) -> None:
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)

    def fail_if_started():
        raise AssertionError("Launcher must not start the Hub remote-Node heartbeat")

    monkeypatch.setattr(dashboard_app, "node_heartbeat_loop", fail_if_started)

    with TestClient(dashboard_app.create_app(mode="launcher")) as client:
        response = client.get("/", follow_redirects=False)

    assert response.headers["location"] == "/nodes/local/agents"


def test_harness_page_exposes_a_node_scoped_interactive_terminal(monkeypatch) -> None:
    worker = node_view("worker")
    monkeypatch.setattr(dashboard_app, "dashboard_node_view", lambda node_id: worker)
    client = dashboard_client(monkeypatch)

    response = client.get("/nodes/worker/agents/claude")

    assert response.status_code == 200
    assert "data-harness-test-terminal" in response.text
    assert 'data-node="worker"' in response.text
    assert 'data-agent="claude"' in response.text
    assert 'data-xterm-js="http://testserver/static/vendor/xterm/xterm.min.js' in response.text
    assert 'class="web-terminal agent-test-terminal is-input-locked"' in response.text
    assert "PTY" in response.text
    assert "The selected Harness starts immediately." in response.text
    assert "This is not a sandbox" in response.text
    assert 'name="prompt"' not in response.text


def test_node_pages_keep_navigation_and_actions_in_node_scope(monkeypatch) -> None:
    worker = node_view(
        "worker",
        sessions=("worker-only",),
        runtime={
            "reported": True,
            "version": "0.1.0",
            "branch": "dev",
            "channel": "preview",
            "short_commit": "aaaaaaa",
            "update_supported": True,
        },
    )
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
    assert "StarAgent runtime" in node_root.text
    assert "StarAgent · worker" in node_root.text
    assert "v0.1.0 · aaaaaaa" in node_root.text
    assert 'class="node-workspace-node-card is-active"' in node_root.text
    assert 'aria-label="Current Node: worker"' in node_root.text
    assert 'aria-current="page"' in node_root.text
    assert 'data-update-check-url="/api/nodes/worker/staragent-update/check"' in node_root.text
    assert 'data-update-apply-url="/api/nodes/worker/staragent-update/apply"' in node_root.text
    assert 'class="node-workspace-runtime"' in node_root.text
    assert sessions.status_code == 200
    assert "Manage live tmux sessions on worker." in sessions.text
    assert "worker-only" in sessions.text
    assert 'aria-label="Sessions on worker"' in sessions.text
    assert 'href="/nodes/worker/sessions/worker-only"' in sessions.text
    assert 'class="session-switcher-item"' in sessions.text
    assert 'href="/nodes/worker/sessions"' in sessions.text
    assert 'href="/nodes/worker/agents"' in sessions.text
    assert 'href="/nodes/worker/logs"' in sessions.text
    assert 'class="node-workspace-node-card"' in sessions.text
    assert 'aria-label="Current Node: worker"' in sessions.text
    assert ">Overview</span>" not in sessions.text
    assert sessions.text.count('name="node" value="worker"') == 2
    assert 'href="/nodes/local/sessions"' not in sessions.text


def test_node_overview_distinguishes_legacy_from_unavailable_nodes(monkeypatch) -> None:
    node = node_view("worker")
    monkeypatch.setattr(dashboard_app, "dashboard_node_entry", lambda node_id: node.entry)
    monkeypatch.setattr(dashboard_app, "dashboard_node_view", lambda node_id: node)
    client = dashboard_client(monkeypatch)

    legacy = client.get("/nodes/worker")

    assert 'data-update-supported="false"' in legacy.text
    assert 'data-update-available="true"' in legacy.text
    assert "Legacy Node" in legacy.text
    assert "Update StarAgent once from its terminal" in legacy.text

    node = node_view("worker", status="disconnected")
    unavailable = client.get("/nodes/worker")

    assert 'data-update-supported="false"' in unavailable.text
    assert 'data-update-available="false"' in unavailable.text
    assert "Unavailable" in unavailable.text
    assert "This Node is unavailable. Check its connection and try again." in unavailable.text


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
    assert "data-theme-settings" in page.text
    assert "data-staragent-update" not in page.text
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
    assert "StarAgent 更新" not in translated.text
    assert "选择一个节点" in translated_nodes.text


def test_settings_rejects_unsupported_language(monkeypatch) -> None:
    client = dashboard_client(monkeypatch)

    response = client.post("/api/settings/language", json={"language": "fr"})

    assert response.status_code == 400


def test_settings_checks_and_applies_official_update(monkeypatch) -> None:
    calls = []
    before = {
        "status": "update_available",
        "branch": "dev",
        "channel": "preview",
        "current_commit": "a" * 40,
        "current_short_commit": "aaaaaaa",
        "latest_commit": "b" * 40,
        "latest_short_commit": "bbbbbbb",
        "behind": 1,
        "dirty": False,
        "can_update": True,
    }
    after = {
        **before,
        "status": "up_to_date",
        "current_commit": "b" * 40,
        "current_short_commit": "bbbbbbb",
        "behind": 0,
        "can_update": False,
    }

    def status(*, refresh=False):  # type: ignore[no-untyped-def]
        calls.append(("check", refresh))
        return before

    monkeypatch.setattr(dashboard_app, "official_update_status", status)
    monkeypatch.setattr(
        dashboard_app,
        "apply_official_update",
        lambda: {"ok": True, "updated": True, "before": before, "after": after},
    )
    monkeypatch.setattr(dashboard_app, "schedule_dashboard_restart", lambda: True)
    monkeypatch.setattr(dashboard_app, "append_hub_event", lambda *args, **kwargs: None)
    client = dashboard_client(monkeypatch)

    checked = client.post("/api/settings/update/check")
    installed = client.post("/api/settings/update/apply")

    assert checked.status_code == 200
    assert checked.json()["latest_short_commit"] == "bbbbbbb"
    assert calls == [("check", True)]
    assert installed.status_code == 200
    assert installed.json()["updated"] is True
    assert installed.json()["restart_scheduled"] is True
    assert installed.json()["after"]["current_short_commit"] == "bbbbbbb"


def test_settings_update_reports_guard_failure_without_restart(monkeypatch) -> None:
    monkeypatch.setattr(
        dashboard_app,
        "apply_official_update",
        lambda: (_ for _ in ()).throw(
            StarAgentUpdateError("dirty_worktree", "local changes are present")
        ),
    )
    monkeypatch.setattr(
        dashboard_app,
        "schedule_dashboard_restart",
        lambda: (_ for _ in ()).throw(AssertionError("must not restart")),
    )
    monkeypatch.setattr(dashboard_app, "append_hub_event", lambda *args, **kwargs: None)
    client = dashboard_client(monkeypatch)

    response = client.post("/api/settings/update/apply")

    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "error": "dirty_worktree",
        "detail": "local changes are present",
    }


def test_current_node_proxies_staragent_update_without_restarting_the_hub(
    monkeypatch,
) -> None:
    worker = node_view("worker")
    calls = []
    status = {
        "ok": True,
        "status": "update_available",
        "latest_short_commit": "bbbbbbb",
        "can_update": True,
        "node": "worker",
    }
    monkeypatch.setattr(dashboard_app, "dashboard_node_entry", lambda node_id: worker.entry)
    monkeypatch.setattr(
        dashboard_app,
        "node_staragent_update_status_payload",
        lambda node, refresh=False: calls.append(("check", node.name, refresh)) or status,
    )
    monkeypatch.setattr(
        dashboard_app,
        "node_staragent_update_apply_payload",
        lambda node: {
            "ok": True,
            "updated": True,
            "restart_scheduled": True,
            "node": node.name,
        },
    )
    monkeypatch.setattr(
        dashboard_app,
        "schedule_dashboard_restart",
        lambda: (_ for _ in ()).throw(AssertionError("remote Node updates must not restart Hub")),
    )
    monkeypatch.setattr(dashboard_app, "append_hub_event", lambda *args, **kwargs: None)
    client = dashboard_client(monkeypatch)

    current = client.get("/api/nodes/worker/staragent-update")
    checked = client.post("/api/nodes/worker/staragent-update/check")
    installed = client.post("/api/nodes/worker/staragent-update/apply")

    assert current.status_code == 200
    assert checked.status_code == 200
    assert checked.json()["latest_short_commit"] == "bbbbbbb"
    assert installed.status_code == 200
    assert installed.json()["restart_scheduled"] is True
    assert calls == [("check", "worker", False), ("check", "worker", True)]


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
