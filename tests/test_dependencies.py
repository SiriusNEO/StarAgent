from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from staragent import dependencies, hub
from staragent.dashboard import app as dashboard_app
from staragent.node import app as node_app


def dependency_by_name(payload: dict[str, object], name: str) -> dict[str, object]:
    items = payload.get("dependencies")
    assert isinstance(items, list)
    return next(item for item in items if isinstance(item, dict) and item.get("name") == name)


def test_linux_dependencies_offer_detected_package_managers(monkeypatch) -> None:
    installed_commands = {"apt-get", "sh", "sudo"}
    monkeypatch.setattr(dependencies, "current_dependency_platform", lambda: "linux")
    monkeypatch.setattr(dependencies, "is_root", lambda: False)
    monkeypatch.setattr(
        dependencies,
        "find_dependency_executable",
        lambda command, environment: (
            f"/usr/bin/{command}" if command in installed_commands else None
        ),
    )
    dependencies.clear_dependencies_cache()

    payload = dependencies.dependencies_status(force=True)
    tmux = dependency_by_name(payload, "tmux")
    tailscale = dependency_by_name(payload, "tailscale")
    nodejs = dependency_by_name(payload, "nodejs")

    assert payload["platform"] == "linux"
    assert [option["id"] for option in tmux["install_options"]] == ["apt"]
    assert [option["id"] for option in tailscale["install_options"]] == ["official-script"]
    assert [option["id"] for option in nodejs["install_options"]] == ["apt"]
    assert nodejs["resources"][1] == {
        "id": "npmmirror",
        "label": "npmmirror",
        "url": "https://npmmirror.com/mirrors/node",
        "source": "mirror",
        "china": True,
    }


def test_windows_dependencies_use_conpty_and_native_package_ids(monkeypatch) -> None:
    monkeypatch.setattr(dependencies, "current_dependency_platform", lambda: "windows")
    monkeypatch.setattr(
        dependencies,
        "find_dependency_executable",
        lambda command, environment: "C:/Windows/winget.exe" if command == "winget" else None,
    )
    monkeypatch.setattr(
        "staragent.native_sessions.native_session_backend_available",
        lambda: True,
    )
    dependencies.clear_dependencies_cache()

    payload = dependencies.dependencies_status(force=True)
    conpty = dependency_by_name(payload, "conpty")
    tailscale = dependency_by_name(payload, "tailscale")
    nodejs = dependency_by_name(payload, "nodejs")

    assert conpty["installed"] is True
    assert conpty["version"] == "Bundled"
    assert "tmux" not in {item["name"] for item in payload["dependencies"]}
    assert tailscale["install_options"][0]["command"].startswith(
        "winget install --id Tailscale.Tailscale"
    )
    assert nodejs["install_options"][0]["command"].startswith(
        "winget install --id OpenJS.NodeJS.LTS"
    )


def test_bundled_macos_runtime_uses_native_pty_without_tmux(monkeypatch) -> None:
    monkeypatch.setenv("STARAGENT_DESKTOP_BUNDLED", "1")
    monkeypatch.setattr(dependencies, "current_dependency_platform", lambda: "macos")
    monkeypatch.setattr(
        "staragent.native_sessions.native_session_backend_available",
        lambda: True,
    )
    dependencies.clear_dependencies_cache()

    payload = dependencies.dependencies_status(force=True)
    pty = dependency_by_name(payload, "pty")

    assert pty["installed"] is True
    assert pty["version"] == "Bundled"
    assert "tmux" not in {item["name"] for item in payload["dependencies"]}


def test_remote_bundled_posix_dependency_payload_keeps_native_pty() -> None:
    payload = dependencies.normalize_dependencies_payload(
        {
            "supported": True,
            "installs_supported": True,
            "platform": "macos",
            "dependencies": [
                {"name": "pty", "status": "available", "installed": True},
                {"name": "tailscale", "status": "missing", "installed": False},
                {"name": "nodejs", "status": "missing", "installed": False},
            ],
        }
    )

    assert dependency_by_name(payload, "pty")["installed"] is True
    assert "tmux" not in {item["name"] for item in payload["dependencies"]}


def test_dependency_version_probes_hide_windows_console(monkeypatch) -> None:
    dependency = dependencies.Dependency(
        name="nodejs",
        label="Node.js + npm",
        commands=("node", "npm"),
        required=False,
        note="",
        docs_url="https://nodejs.org/en/download",
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(command)
        assert kwargs["creationflags"] == 0x0800_0000
        value = "v24.1.0\n" if "node" in command[0] else "11.0.0\n"
        return subprocess.CompletedProcess(command, 0, value, "")

    monkeypatch.setattr(
        dependencies, "windows_process_argv", lambda command, *args, **kwargs: command
    )
    monkeypatch.setattr(
        dependencies,
        "background_process_kwargs",
        lambda: {"creationflags": 0x0800_0000},
    )
    monkeypatch.setattr(dependencies.subprocess, "run", fake_run)

    version = dependencies.dependency_version(
        dependency,
        {"node": "C:/node.exe", "npm": "C:/npm.cmd"},
        {"PATH": "C:/"},
    )

    assert version == "node v24.1.0 · npm 11.0.0"
    assert len(calls) == 2


def test_dependency_payload_rebuilds_remote_install_options_from_allowlist() -> None:
    payload = dependencies.normalize_dependencies_payload(
        {
            "supported": True,
            "installs_supported": True,
            "platform": "windows",
            "dependencies": [
                {
                    "name": "nodejs",
                    "status": "missing",
                    "installed": False,
                    "install_options": [
                        {
                            "id": "winget",
                            "available": True,
                            "command": "powershell attacker.example",
                            "provider": "attacker",
                        },
                        {"id": "arbitrary-shell", "available": True},
                    ],
                },
                {"name": "rogue", "status": "available", "installed": True},
            ],
        }
    )

    nodejs = dependency_by_name(payload, "nodejs")
    assert [option["id"] for option in nodejs["install_options"]] == ["winget"]
    assert nodejs["install_options"][0]["provider"] == "Windows Package Manager"
    assert "attacker" not in nodejs["install_options"][0]["command"]
    assert "rogue" not in {item["name"] for item in payload["dependencies"]}


def test_remote_windows_install_command_uses_remote_platform(monkeypatch) -> None:
    monkeypatch.setattr(dependencies, "is_root", lambda: False)

    payload = dependencies.normalize_dependencies_payload(
        {
            "supported": True,
            "installs_supported": True,
            "platform": "windows",
            "dependencies": [
                {
                    "name": "nodejs",
                    "status": "missing",
                    "installed": False,
                    "install_options": [
                        {
                            "id": "winget",
                            "available": True,
                            "command": "not reviewed",
                            "missing_requirements": [],
                        }
                    ],
                }
            ],
        }
    )

    option = dependency_by_name(payload, "nodejs")["install_options"][0]
    assert option["command"].startswith("winget install")
    assert not option["command"].startswith("sudo ")


def test_tailscale_package_commands_add_sudo_only_for_display(monkeypatch) -> None:
    monkeypatch.setattr(dependencies, "is_root", lambda: False)
    spec = dependencies.dependency_spec("tailscale", "linux")
    assert spec is not None
    option = dependencies.dependency_install_option(spec, "pacman", platform_name="linux")

    assert option.command == "pacman -S --noconfirm tailscale"
    assert dependencies.manual_dependency_command(option, platform_name="linux") == (
        "sudo pacman -S --noconfirm tailscale"
    )


def test_dependency_install_uses_allowlisted_runner_and_rechecks(monkeypatch) -> None:
    spec = dependencies.dependency_spec("nodejs", "linux")
    assert spec is not None
    statuses = iter(
        (
            dependencies.dependency_status_payload(
                spec,
                platform_name="linux",
                status="missing",
                installed=False,
            ),
            dependencies.dependency_status_payload(
                spec,
                platform_name="linux",
                status="available",
                installed=True,
                version="node v24 · npm 11",
            ),
        )
    )
    monkeypatch.setattr(dependencies, "current_dependency_platform", lambda: "linux")
    monkeypatch.setattr(dependencies, "dependency_status", lambda *args, **kwargs: next(statuses))
    monkeypatch.setattr(
        dependencies,
        "install_option_payload",
        lambda option, reported=None: {"available": True, "missing_requirements": []},
    )
    calls: list[str] = []
    monkeypatch.setattr(
        dependencies,
        "run_dependency_install_option",
        lambda option: (calls.append(option.id) or 0, "installed"),
    )

    result = dependencies.install_dependency("nodejs", "apt")

    assert calls == ["apt"]
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["after_version"] == "node v24 · npm 11"


def test_dependency_installer_rejects_unreviewed_sources() -> None:
    spec = dependencies.dependency_spec("tailscale", "linux")
    assert spec is not None

    with pytest.raises(ValueError, match="Unsupported install option"):
        dependencies.dependency_install_option(
            spec,
            "sh-c-curl-attacker",
            platform_name="linux",
        )

    with pytest.raises(ValueError, match="allowlisted"):
        dependencies.download_dependency_install_script("https://attacker.example/install.sh")


def test_node_dependency_routes_are_authenticated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        node_app,
        "dependencies_status",
        lambda force=False: dependencies.unknown_dependencies_payload("test"),
    )
    monkeypatch.setattr(
        node_app,
        "install_dependency",
        lambda dependency, option: {
            "ok": True,
            "dependency": dependency,
            "label": "Node.js + npm",
            "option": option,
        },
    )
    monkeypatch.setattr(node_app, "collect_session_views", lambda: [])
    client = TestClient(node_app.create_app())

    assert client.get("/api/dependencies").status_code == 401
    response = client.get(
        "/api/dependencies?refresh=true",
        headers={"Authorization": "Bearer node-secret"},
    )
    install = client.post(
        "/api/dependencies/nodejs/install/winget",
        headers={"Authorization": "Bearer node-secret"},
    )

    assert response.status_code == 200
    assert install.status_code == 200
    assert install.json()["option"] == "winget"
    sessions = client.get(
        "/api/sessions",
        headers={"Authorization": "Bearer node-secret"},
    ).json()
    assert sessions["capabilities"]["dependencies"] == 1


def test_hub_proxies_remote_dependency_status_and_install(monkeypatch) -> None:
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str]] = []

    def fake_request(selected, method, path, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((method, path))
        if method == "GET":
            return {
                "supported": True,
                "installs_supported": True,
                "platform": "windows",
                "dependencies": [],
            }
        return {
            "ok": True,
            "dependency": "nodejs",
            "platform": "windows",
            "option": "winget",
            "command": "winget install --id OpenJS.NodeJS.LTS --exact --source winget --accept-package-agreements --accept-source-agreements --silent",
            "before_status": "missing",
            "after_status": "available",
            "changed": True,
        }

    monkeypatch.setattr(hub, "request_json", fake_request)

    status = hub.node_dependencies_payload(node, refresh=True)
    installed = hub.node_dependency_install_payload(node, "nodejs", "winget")

    assert status["platform"] == "windows"
    assert installed["ok"] is True
    assert installed["node"] == "worker"
    assert calls == [
        ("GET", "/api/dependencies?refresh=true"),
        ("POST", "/api/dependencies/nodejs/install/winget"),
    ]


def test_dashboard_dependency_routes_stay_scoped_to_selected_node(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(dashboard_app, "dashboard_node_entry", lambda node_id: node)
    monkeypatch.setattr(
        dashboard_app,
        "node_dependencies_payload",
        lambda selected, refresh=False: (
            calls.append(("status", (selected.name, refresh)))
            or {"supported": True, "dependencies": [], "node": selected.name}
        ),
    )
    monkeypatch.setattr(
        dashboard_app,
        "node_dependency_install_payload",
        lambda selected, dependency, option: (
            calls.append(("install", (selected.name, dependency, option)))
            or {
                "ok": True,
                "node": selected.name,
                "dependency": dependency,
                "option": option,
            }
        ),
    )
    client = TestClient(dashboard_app.create_app())

    status = client.get("/api/nodes/worker/dependencies?refresh=true")
    install = client.post("/api/nodes/worker/dependencies/nodejs/install/winget")

    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert install.status_code == 200
    assert calls == [
        ("status", ("worker", True)),
        ("install", ("worker", "nodejs", "winget")),
    ]


def test_current_node_renders_dependency_manager(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    node = SimpleNamespace(
        name="local",
        status="connected",
        mode="local",
        endpoint="local",
        error="",
        sessions=(),
        runtime={},
        session_count=0,
        staragent_version="0.1.3",
        staragent_update_supported=True,
    )
    monkeypatch.setattr(dashboard_app, "dashboard_node_view", lambda node_id: node)

    response = TestClient(dashboard_app.create_app()).get("/nodes/local")

    assert response.status_code == 200
    assert "data-node-dependencies" in response.text
    assert 'data-node="local"' in response.text
    assert "dependencies.js" in response.text

    script = (
        dashboard_app.PROJECT_ROOT / "staragent" / "dashboard" / "static" / "dependencies.js"
    ).read_text(encoding="utf-8")
    assert 'className = "launcher-source-list"' in script
    assert 'className = "launcher-install-actions"' in script
    assert "agent-install-option" not in script
