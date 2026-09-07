from __future__ import annotations

import json
import urllib.error

from fastapi.testclient import TestClient

from staragent import agent_skills, hub
from staragent.dashboard import app as dashboard_app
from staragent.node import app as node_app


def write_skill(directory, name: str, description: str) -> None:  # type: ignore[no-untyped-def]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# ignored body\n",
        encoding="utf-8",
    )


def test_codex_skill_scan_distinguishes_bundled_personal_and_plugins(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    write_skill(codex_home / "skills" / ".system" / "review", "review", "Review code")
    write_skill(codex_home / "skills" / "local", "local", "A personal skill")
    write_skill(home / ".agents" / "skills" / "shared", "shared", "A shared skill")

    plugin = codex_home / "plugins" / "cache" / "official" / "figma"
    plugin.mkdir(parents=True)
    (plugin / ".codex-remote-plugin-install.json").write_text("{}", encoding="utf-8")
    version = plugin / "2.0.0"
    (version / ".codex-plugin").mkdir(parents=True)
    (version / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "Figma", "skills": "./skills"}),
        encoding="utf-8",
    )
    write_skill(version / "skills" / "design", "figma-design", "Build a design")

    monkeypatch.setattr(
        agent_skills,
        "harness_process_environment",
        lambda agent: {"HOME": str(home), "CODEX_HOME": str(codex_home)},
    )
    agent_skills.clear_agent_skills_cache()

    payload = agent_skills.agent_skills_payload("codex", force=True)

    assert payload["counts"] == {
        "total": 4,
        "bundled": 1,
        "installed": 3,
        "personal": 2,
        "plugin": 1,
    }
    skills = {item["name"]: item for item in payload["skills"]}
    assert skills["review"]["scope"] == "bundled"
    assert skills["shared"]["kind"] == "compatible"
    assert skills["figma-design"]["plugin"] == "Figma"
    assert all("path" not in item for item in payload["skills"])
    assert {root["directory"] for root in payload["roots"]} <= agent_skills.SKILL_DIRECTORIES


def test_skill_scan_is_bounded_and_does_not_follow_symlinks(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    write_skill(codex_home / "skills" / "first", "first", "First")
    write_skill(codex_home / "skills" / "second", "second", "Second")
    outside = tmp_path / "outside"
    write_skill(outside / "secret", "secret", "Must not be scanned")
    (codex_home / "skills" / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        agent_skills,
        "harness_process_environment",
        lambda agent: {"HOME": str(home), "CODEX_HOME": str(codex_home)},
    )
    monkeypatch.setattr(agent_skills, "SKILL_SCAN_MAX_FILES", 1)
    agent_skills.clear_agent_skills_cache()

    payload = agent_skills.agent_skills_payload("codex", force=True)

    assert payload["counts"]["total"] == 1
    assert payload["truncated"] is True
    assert payload["skills"][0]["name"] != "secret"


def test_skill_scan_cache_skips_rewalking_plugin_roots(monkeypatch, tmp_path) -> None:
    calls = 0
    monkeypatch.setattr(
        agent_skills,
        "harness_process_environment",
        lambda agent: {"HOME": str(tmp_path)},
    )

    def fake_roots(agent, environment):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(agent_skills, "skill_roots", fake_roots)
    agent_skills.clear_agent_skills_cache()

    first = agent_skills.agent_skills_payload("codex", force=True)
    second = agent_skills.agent_skills_payload("codex")

    assert calls == 1
    assert first == second


def test_skill_frontmatter_supports_block_descriptions(tmp_path) -> None:
    path = tmp_path / "skill" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(
        "---\nname: 'release-helper'\ndescription: >\n  Prepare releases\n  safely.\n---\nbody",
        encoding="utf-8",
    )

    metadata, complete = agent_skills.read_skill_frontmatter(path)

    assert complete is True
    assert metadata == {"name": "release-helper", "description": "Prepare releases safely."}


def test_skill_scan_stops_after_frontmatter(tmp_path) -> None:
    path = tmp_path / "skill" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(
        "---\nname: bounded\ndescription: Read metadata only\n---\n" + ("body\n" * 20_000),
        encoding="utf-8",
    )

    metadata, complete = agent_skills.read_skill_frontmatter(path)

    assert complete is True
    assert metadata == {"name": "bounded", "description": "Read metadata only"}


def test_claude_scan_includes_only_enabled_user_plugins(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    claude_home = home / ".claude"
    write_skill(claude_home / "skills" / "personal", "personal", "Personal")
    enabled_root = claude_home / "plugins" / "cache" / "official" / "enabled" / "1.0"
    disabled_root = claude_home / "plugins" / "cache" / "official" / "disabled" / "1.0"
    write_skill(enabled_root / "skills" / "hello", "hello", "Enabled plugin")
    nameless = enabled_root / "skills" / "directory-name" / "SKILL.md"
    nameless.parent.mkdir(parents=True)
    nameless.write_text("---\ndescription: Uses its directory name\n---\n", encoding="utf-8")
    write_skill(disabled_root / "skills" / "hidden", "hidden", "Disabled plugin")
    (enabled_root / ".claude-plugin").mkdir()
    (enabled_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "enabled-plugin"}), encoding="utf-8"
    )
    claude_home.mkdir(exist_ok=True)
    (claude_home / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"enabled@official": True, "disabled@official": False}}),
        encoding="utf-8",
    )
    plugins = claude_home / "plugins"
    (plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "enabled@official": [{"scope": "user", "installPath": str(enabled_root)}],
                    "disabled@official": [{"scope": "user", "installPath": str(disabled_root)}],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent_skills,
        "harness_process_environment",
        lambda agent: {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(claude_home)},
    )
    agent_skills.clear_agent_skills_cache()

    payload = agent_skills.agent_skills_payload("claude", force=True)

    names = {item["name"] for item in payload["skills"]}
    assert names == {"personal", "hello", "directory-name"}
    plugin = next(item for item in payload["skills"] if item["name"] == "hello")
    assert plugin["scope"] == "plugin"
    assert plugin["plugin"] == "enabled-plugin"
    fallback = next(item for item in payload["skills"] if item["name"] == "directory-name")
    assert fallback["valid"] is True


def test_claude_plugin_registry_cannot_escape_cache(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    claude_home = home / ".claude"
    outside = tmp_path / "outside"
    write_skill(outside / "skills" / "secret", "secret", "Outside cache")
    (claude_home / "plugins").mkdir(parents=True)
    (claude_home / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"bad@local": True}}), encoding="utf-8"
    )
    (claude_home / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"bad@local": [{"scope": "user", "installPath": str(outside)}]}}),
        encoding="utf-8",
    )
    (claude_home / "plugins" / "cache").mkdir()
    monkeypatch.setattr(
        agent_skills,
        "harness_process_environment",
        lambda agent: {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(claude_home)},
    )
    agent_skills.clear_agent_skills_cache()

    payload = agent_skills.agent_skills_payload("claude", force=True)

    assert all(item["name"] != "secret" for item in payload["skills"])


def test_remote_skill_payload_is_normalized_and_bounded(monkeypatch) -> None:
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[str] = []

    def fake_request(selected, method, path, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(path)
        return {
            "supported": True,
            "roots": [
                {
                    "id": "personal",
                    "source": "Personal",
                    "directory": "/private/should-not-pass",
                    "scope": "unexpected",
                    "kind": "unexpected",
                    "count": 9999,
                }
            ],
            "skills": [
                {
                    "id": "Test Skill",
                    "name": "Test\x00 Skill",
                    "description": "Detected remotely",
                    "scope": "personal",
                    "kind": "native",
                    "source": "Personal",
                    "valid": True,
                }
            ],
        }

    monkeypatch.setattr(hub, "request_json", fake_request)

    payload = hub.node_agent_skills_payload(node, "codex", refresh=True)

    assert calls == ["/api/agent-tools/codex/skills?refresh=true"]
    assert payload["node"] == "worker"
    assert payload["roots"][0]["directory"] == ""
    assert payload["roots"][0]["scope"] == "personal"
    assert payload["roots"][0]["count"] == agent_skills.SKILL_SCAN_MAX_FILES
    assert payload["skills"][0]["name"] == "Test Skill"


def test_remote_skill_scan_reports_legacy_node(monkeypatch) -> None:
    node = hub.NodeEntry(name="old", url="http://old:8081", mode="lan")

    def missing(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError("/skills", 404, "Not Found", {}, None)

    monkeypatch.setattr(hub, "request_json", missing)

    payload = hub.node_agent_skills_payload(node, "claude")

    assert payload["supported"] is False
    assert "Update StarAgent" in payload["error"]


def test_node_skill_endpoint_is_authenticated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    monkeypatch.setattr(
        node_app,
        "agent_skills_payload",
        lambda agent, force=False: agent_skills.unavailable_agent_skills(agent, "test"),
    )
    client = TestClient(node_app.create_app())

    assert client.get("/api/agent-tools/codex/skills").status_code == 401
    response = client.get(
        "/api/agent-tools/codex/skills?refresh=true",
        headers={"Authorization": "Bearer node-secret"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert (
        client.get(
            "/api/agent-tools/gemini/skills",
            headers={"Authorization": "Bearer node-secret"},
        ).status_code
        == 404
    )
    sessions = client.get("/api/sessions", headers={"Authorization": "Bearer node-secret"}).json()
    assert sessions["capabilities"]["agent_skills"] == 1


def test_dashboard_skill_route_keeps_node_scope(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    selected = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda name: selected)

    def fake_payload(node, agent, refresh=False):  # type: ignore[no-untyped-def]
        calls.append((node.name, agent, refresh))
        return {**agent_skills.unavailable_agent_skills(agent, "test"), "node": node.name}

    monkeypatch.setattr(dashboard_app, "node_agent_skills_payload", fake_payload)
    response = TestClient(dashboard_app.create_app()).get(
        "/api/nodes/worker/agent-tools/opencode/skills?refresh=true"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["node"] == "worker"
    assert calls == [("worker", "opencode", True)]
