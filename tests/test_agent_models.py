from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from staragent import agent_models, harness_terminal, hub, runtime
from staragent.dashboard import app as dashboard_app
from staragent.node import app as node_app


@pytest.fixture
def isolated_models(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path / "state"))
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_DEFAULT_MODEL",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
        "CODEX_HOME",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(agent_models.shutil, "which", lambda *_args, **_kwargs: None)
    agent_models.clear_agent_models_cache()
    return home


def test_model_preferences_are_private_node_scoped_state(isolated_models: Path) -> None:
    payload = agent_models.save_harness_model_preference("codex", "gpt-5.5", "high")

    assert payload["selected_model"] == "gpt-5.5"
    assert payload["effective_model"] == "gpt-5.5"
    assert payload["selected_reasoning_effort"] == "high"
    assert payload["effective_reasoning_effort"] == "high"
    assert agent_models.harness_model_preference("codex") == "gpt-5.5"
    assert agent_models.harness_reasoning_effort_preference("codex") == "high"
    path = agent_models.harness_models_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 2,
        "models": {"codex": "gpt-5.5"},
        "reasoning_efforts": {"codex": "high"},
    }

    cleared = agent_models.save_harness_model_preference("codex", "")

    assert cleared["selected_model"] == ""
    assert cleared["selected_reasoning_effort"] == ""
    assert agent_models.harness_model_preference("codex") == ""


@pytest.mark.parametrize(
    "model",
    ("$(touch-pwned)", "model name", "--model", "model;shutdown", "model\nnext"),
)
def test_model_preference_rejects_shell_syntax(
    isolated_models: Path,
    model: str,
) -> None:
    with pytest.raises(ValueError, match="Model ID"):
        agent_models.save_harness_model_preference("codex", model)


@pytest.mark.parametrize(
    "effort",
    ("high;shutdown", "high effort", "$(touch-pwned)", "high\nnext"),
)
def test_reasoning_preference_rejects_shell_syntax(
    isolated_models: Path,
    effort: str,
) -> None:
    with pytest.raises(ValueError, match="Reasoning effort"):
        agent_models.save_harness_model_preference("codex", "", effort)


def test_v1_model_preference_is_read_and_migrated(isolated_models: Path) -> None:
    path = agent_models.harness_models_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "models": {"codex": "legacy-model"}}),
        encoding="utf-8",
    )

    assert agent_models.harness_launch_preferences("codex") == ("legacy-model", "")

    agent_models.save_harness_model_preference("codex", "legacy-model", "medium")

    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated == {
        "version": 2,
        "models": {"codex": "legacy-model"},
        "reasoning_efforts": {"codex": "medium"},
    }


def test_effort_normalization_preserves_opencode_variant_ids(isolated_models: Path) -> None:
    assert agent_models.validate_reasoning_effort("codex", "HIGH") == "high"
    assert agent_models.validate_reasoning_effort("claude", "XHIGH") == "xhigh"
    assert agent_models.validate_reasoning_effort("opencode", "DeepReview") == "DeepReview"


def test_model_preference_applies_only_to_new_interactive_launches(
    isolated_models: Path,
) -> None:
    agent_models.save_harness_model_preference("codex", "private/deepseek-v3", "high")
    agent_models.save_harness_model_preference("claude", "sonnet[1m]", "medium")
    agent_models.save_harness_model_preference(
        "opencode",
        "deepseek/deepseek-chat",
        "thinking",
    )

    assert agent_models.apply_harness_model_preference("codex --yolo") == (
        "codex --model private/deepseek-v3 --config model_reasoning_effort=high --yolo"
    )
    assert agent_models.apply_harness_model_preference("claude") == (
        "claude --model 'sonnet[1m]' --effort medium"
    )
    assert agent_models.apply_harness_model_preference("opencode /workspace") == (
        "opencode --model deepseek/deepseek-chat --variant thinking /workspace"
    )
    assert agent_models.apply_harness_model_preference("codex --model explicit") == (
        "codex --config model_reasoning_effort=high --model explicit"
    )
    assert agent_models.apply_harness_model_preference("opencode -m=explicit/model") == (
        "opencode --variant thinking -m=explicit/model"
    )
    assert agent_models.apply_harness_model_preference('"/opt/Codex Bin/codex" --yolo') == (
        '"/opt/Codex Bin/codex" --model private/deepseek-v3 '
        "--config model_reasoning_effort=high --yolo"
    )
    assert (
        agent_models.apply_harness_model_preference(
            "codex --config model_reasoning_effort=low --yolo"
        )
        == "codex --model private/deepseek-v3 --config model_reasoning_effort=low --yolo"
    )
    assert (
        agent_models.apply_harness_model_preference("codex -c model=explicit --yolo")
        == "codex --config model_reasoning_effort=high -c model=explicit --yolo"
    )
    assert (
        agent_models.apply_harness_model_preference("claude --effort=low")
        == "claude --model 'sonnet[1m]' --effort=low"
    )
    assert agent_models.apply_harness_model_preference("codex resume session-id") == (
        "codex resume session-id"
    )
    assert agent_models.apply_harness_model_preference("claude --resume session-id") == (
        "claude --resume session-id"
    )
    assert agent_models.apply_harness_model_preference("opencode auth login") == (
        "opencode auth login"
    )
    assert agent_models.apply_harness_model_preference("bash") == "bash"


def test_harness_test_terminal_uses_the_node_model_preference(
    isolated_models: Path,
    monkeypatch,
) -> None:
    agent_models.save_harness_model_preference("codex", "gpt-5.5", "high")
    monkeypatch.setattr(
        harness_terminal.shutil,
        "which",
        lambda command: "/bin/bash" if command == "bash" else None,
    )

    argv = harness_terminal.harness_terminal_argv("codex")

    assert argv[:2] == ["/bin/bash", "-lc"]
    assert "\ncodex --model gpt-5.5 --config model_reasoning_effort=high\n" in argv[2]


def test_new_native_session_uses_model_preference_but_resume_does_not(
    isolated_models: Path,
    monkeypatch,
    tmp_path,
) -> None:
    agent_models.save_harness_model_preference("codex", "gpt-5.5", "high")
    created = []

    class Registry:
        def create(self, name, cwd, command, **kwargs):  # type: ignore[no-untyped-def]
            created.append((name, cwd, command, kwargs))

    monkeypatch.setattr(runtime, "native_sessions_enabled", lambda: True)
    monkeypatch.setattr(runtime, "native_registry", Registry)
    monkeypatch.setattr(runtime, "tmux_session_exists", lambda _name: False)

    runtime.start_tmux_worker("fresh", str(tmp_path), "codex --yolo")
    runtime.start_tmux_worker(
        "resumed",
        str(tmp_path),
        "codex resume 11111111-2222-4333-8444-555555555555",
    )

    assert created[0][2] == ("codex --model gpt-5.5 --config model_reasoning_effort=high --yolo")
    assert created[1][2] == "codex resume 11111111-2222-4333-8444-555555555555"


def test_codex_catalog_comes_from_app_server_and_keeps_provider(
    isolated_models: Path,
    monkeypatch,
) -> None:
    (isolated_models / ".codex").mkdir(parents=True)
    (isolated_models / ".codex" / "config.toml").write_text(
        'model = "deepseek-chat"\nmodel_provider = "deepseek"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent_models.shutil,
        "which",
        lambda *_args, **_kwargs: "/tools/codex",
    )
    calls = []

    def fake_requests(executable, requests, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((executable, requests, kwargs))
        return {
            "config/read": {
                "config": {
                    "model": "deepseek-chat",
                    "model_provider": "deepseek",
                    "model_providers": {"deepseek": {"name": "DeepSeek"}},
                }
            },
            "model/list": {
                "data": [
                    {
                        "id": "deepseek-chat",
                        "displayName": "DeepSeek Chat",
                        "description": "Provider-discovered model",
                        "isDefault": True,
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [
                            {
                                "reasoningEffort": "low",
                                "description": "Fast",
                            },
                            {
                                "reasoningEffort": "high",
                                "description": "Deep",
                            },
                        ],
                    }
                ]
            },
        }

    monkeypatch.setattr(agent_models, "codex_app_server_requests", fake_requests)

    payload = agent_models.agent_models_payload("codex", force=True)

    assert payload["provider"] == "DeepSeek"
    assert payload["configured_model"] == "deepseek-chat"
    assert payload["default_model"] == "deepseek-chat"
    assert payload["catalog_source"] == "codex-app-server"
    assert payload["default_reasoning_effort"] == "medium"
    assert payload["effective_reasoning_effort"] == "medium"
    assert [item["id"] for item in payload["reasoning_efforts"]] == ["low", "high"]
    assert payload["models"] == [
        {
            "id": "deepseek-chat",
            "label": "DeepSeek Chat",
            "provider": "DeepSeek",
            "description": "Provider-discovered model",
            "default": True,
            "source": "codex-app-server",
            "reasoning_efforts": [
                {"id": "low", "description": "Fast"},
                {"id": "high", "description": "Deep"},
            ],
            "default_reasoning_effort": "medium",
        }
    ]
    assert calls[0][0] == "/tools/codex"
    assert set(calls[0][1]) == {"config/read", "model/list"}


def test_codex_managed_new_thread_model_precedes_regular_default(
    isolated_models: Path,
) -> None:
    assert (
        agent_models.codex_configured_model(
            {
                "model": "user-default",
                "models": {"new_thread": {"model": "managed-new-thread"}},
            }
        )
        == "managed-new-thread"
    )


def test_codex_managed_reasoning_precedes_regular_default(isolated_models: Path) -> None:
    assert (
        agent_models.codex_configured_reasoning_effort(
            {
                "model_reasoning_effort": "medium",
                "models": {"new_thread": {"model_reasoning_effort": "xhigh"}},
            }
        )
        == "xhigh"
    )


def test_claude_catalog_merges_aliases_allowlist_and_environment(
    isolated_models: Path,
    monkeypatch,
) -> None:
    (isolated_models / ".claude").mkdir(parents=True)
    (isolated_models / ".claude" / "settings.json").write_text(
        json.dumps({"model": "opus", "availableModels": ["private-sonnet"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ANTHROPIC_MODEL", "gateway-sonnet")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.invalid")
    monkeypatch.setattr(
        agent_models.shutil,
        "which",
        lambda *_args, **_kwargs: "/tools/claude",
    )
    monkeypatch.setattr(
        agent_models.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout="--model <model> --effort <level>",
            stderr="",
        ),
    )
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "high")

    payload = agent_models.agent_models_payload("claude", force=True)

    assert payload["configured_model"] == "gateway-sonnet"
    assert payload["effective_source"] == "environment"
    assert payload["provider"] == "Custom gateway"
    assert payload["configured_reasoning_effort"] == "high"
    assert payload["effective_reasoning_effort"] == "high"
    assert payload["reasoning_effort_supported"] is True
    assert {item["id"] for item in payload["models"]} >= {
        "default",
        "sonnet",
        "opus",
        "private-sonnet",
        "gateway-sonnet",
    }


def test_claude_effort_choices_follow_the_installed_cli(
    isolated_models: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        agent_models.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout="--effort <level>  Effort (low, high, ultracode)",
            stderr="",
        ),
    )

    assert agent_models.claude_reasoning_effort_values("/tools/claude", {}) == (
        "low",
        "high",
        "ultracode",
    )


def test_opencode_catalog_uses_fixed_argv_and_reads_jsonc(
    isolated_models: Path,
    monkeypatch,
) -> None:
    config = isolated_models / ".config" / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
        {
          // Keep this comment intact; StarAgent only reads this file.
          "model": "deepseek/deepseek-chat",
          "note": "commas inside strings stay: ,}",
          "provider": {
            "private": {
              "models": {
                "coder-v2": {
                  "name": "Coder V2",
                  "variants": {"fast": {}, "DeepReview": {"reasoningEffort": "high"},},
                },
              },
            },
          },
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent_models.shutil,
        "which",
        lambda *_args, **_kwargs: "/tools/opencode",
    )
    calls = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="anthropic/claude-sonnet\ndeepseek/deepseek-chat\n",
            stderr="",
        )

    monkeypatch.setattr(agent_models.subprocess, "run", fake_run)

    payload = agent_models.agent_models_payload("opencode", force=True)

    assert calls[0][0] == ["/tools/opencode", "models", "--refresh"]
    assert "shell" not in calls[0][1]
    assert payload["configured_model"] == "deepseek/deepseek-chat"
    assert {item["id"] for item in payload["models"]} == {
        "private/coder-v2",
        "anthropic/claude-sonnet",
        "deepseek/deepseek-chat",
    }
    configured = next(item for item in payload["models"] if item["id"] == "private/coder-v2")
    assert [item["id"] for item in configured["reasoning_efforts"]] == [
        "fast",
        "DeepReview",
    ]
    assert config.read_text(encoding="utf-8").find("Keep this comment intact") > 0


def test_remote_model_payload_is_allowlisted_and_node_scoped(monkeypatch) -> None:
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls = []

    def fake_request(selected, method, path, body=None, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((selected.name, method, path, body, kwargs))
        return {
            "supported": True,
            "agent": "wrong-agent",
            "installed": True,
            "selected_model": "private/model",
            "selected_reasoning_effort": "high",
            "reasoning_effort_supported": True,
            "reasoning_efforts": [
                {"id": "high", "description": "deep"},
                {"id": "high;unsafe", "description": "drop"},
            ],
            "models": [
                {
                    "id": "private/model",
                    "label": "Private",
                    "provider": "private",
                    "description": "ok",
                    "default": True,
                    "reasoning_efforts": [{"id": "high"}, {"id": "$(unsafe)"}],
                    "unexpected": "drop-me",
                },
                {"id": "$(unsafe)", "label": "unsafe"},
            ],
            "docs_url": "https://malicious.invalid",
        }

    monkeypatch.setattr(hub, "request_json", fake_request)

    payload = hub.node_harness_models_payload(node, "codex", refresh=True)

    assert calls[0][0:4] == (
        "worker",
        "GET",
        "/api/agent-tools/codex/models?refresh=true",
        None,
    )
    assert payload["node"] == "worker"
    assert payload["agent"] == "codex"
    assert payload["docs_url"].startswith("https://learn.chatgpt.com/")
    assert payload["selected_reasoning_effort"] == "high"
    assert payload["reasoning_efforts"] == [{"id": "high", "description": "deep"}]
    assert payload["models"] == [
        {
            "id": "private/model",
            "label": "Private",
            "provider": "private",
            "description": "ok",
            "default": True,
            "source": "",
            "reasoning_efforts": [{"id": "high", "description": ""}],
            "default_reasoning_effort": "",
        }
    ]


def test_hub_saves_model_preference_on_only_the_selected_node(monkeypatch) -> None:
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls = []

    def fake_request(selected, method, path, body, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((selected.name, method, path, body, kwargs))
        return {
            "supported": True,
            "agent": "codex",
            "installed": True,
            "selected_model": body["model"],
            "selected_reasoning_effort": body["reasoning_effort"],
            "reasoning_effort_supported": True,
            "reasoning_efforts": [{"id": body["reasoning_effort"]}],
            "models": [{"id": body["model"]}],
        }

    monkeypatch.setattr(hub, "request_json", fake_request)

    payload = hub.node_save_harness_model_preference(
        node,
        "codex",
        "private/model",
        "high",
    )

    assert calls[0][0:4] == (
        "worker",
        "PUT",
        "/api/agent-tools/codex/models/preference",
        {"model": "private/model", "reasoning_effort": "high"},
    )
    assert payload["node"] == "worker"
    assert payload["selected_model"] == "private/model"
    assert payload["selected_reasoning_effort"] == "high"


def test_hub_rejects_unsafe_remote_model_before_forwarding(monkeypatch) -> None:
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    monkeypatch.setattr(
        hub,
        "request_json",
        lambda *_args, **_kwargs: pytest.fail("unsafe model reached the Remote Node"),
    )

    with pytest.raises(ValueError, match="Model ID"):
        hub.node_save_harness_model_preference(node, "codex", "model; shutdown")


def test_hub_rejects_unsafe_remote_reasoning_before_forwarding(monkeypatch) -> None:
    node = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    monkeypatch.setattr(
        hub,
        "request_json",
        lambda *_args, **_kwargs: pytest.fail("unsafe effort reached the Remote Node"),
    )

    with pytest.raises(ValueError, match="Reasoning effort"):
        hub.node_save_harness_model_preference(node, "codex", "", "high; shutdown")


def test_node_model_routes_are_authenticated_and_no_store(monkeypatch) -> None:
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    calls = []
    payload = {
        "supported": True,
        "agent": "codex",
        "installed": True,
        "selected_model": "",
        "configured_model": "",
        "default_model": "gpt-default",
        "effective_model": "gpt-default",
        "effective_source": "harness_default",
        "selected_reasoning_effort": "",
        "effective_reasoning_effort": "medium",
        "reasoning_effort_supported": True,
        "reasoning_efforts": [{"id": "medium"}, {"id": "high"}],
        "provider": "OpenAI",
        "models": [{"id": "gpt-default"}],
        "catalog_source": "codex-app-server",
        "refresh_supported": True,
        "docs_url": "https://example.invalid",
        "checked_at": "2026-01-01T00:00:00Z",
        "error": "",
    }
    monkeypatch.setattr(
        node_app,
        "agent_models_payload",
        lambda agent, force=False: calls.append(("get", agent, force)) or payload,
    )
    monkeypatch.setattr(
        node_app,
        "save_harness_model_preference",
        lambda agent, model, reasoning_effort: (
            calls.append(("put", agent, model, reasoning_effort))
            or {
                **payload,
                "selected_model": model,
                "effective_model": model,
                "selected_reasoning_effort": reasoning_effort,
                "effective_reasoning_effort": reasoning_effort,
            }
        ),
    )
    monkeypatch.setattr(node_app, "append_node_outbox_event", lambda *_args, **_kwargs: None)
    client = TestClient(node_app.create_app())

    unauthorized = client.get("/api/agent-tools/codex/models")
    loaded = client.get(
        "/api/agent-tools/codex/models?refresh=true",
        headers={"Authorization": "Bearer node-secret"},
    )
    saved = client.put(
        "/api/agent-tools/codex/models/preference",
        headers={"Authorization": "Bearer node-secret"},
        json={"model": "gpt-selected", "reasoning_effort": "high"},
    )

    assert unauthorized.status_code == 401
    assert loaded.status_code == 200
    assert loaded.headers["cache-control"] == "no-store"
    assert saved.status_code == 200
    assert saved.headers["cache-control"] == "no-store"
    assert saved.json()["selected_model"] == "gpt-selected"
    assert saved.json()["selected_reasoning_effort"] == "high"
    assert calls == [
        ("get", "codex", True),
        ("put", "codex", "gpt-selected", "high"),
    ]


def test_dashboard_model_routes_target_only_the_requested_node(monkeypatch) -> None:
    worker = hub.NodeEntry(name="worker", url="http://worker:8081", mode="lan")
    calls = []
    payload = {
        "supported": True,
        "agent": "claude",
        "node": "worker",
        "selected_model": "sonnet",
        "selected_reasoning_effort": "high",
        "models": [{"id": "sonnet"}],
    }
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda node_id: worker)
    monkeypatch.setattr(
        dashboard_app,
        "node_harness_models_payload",
        lambda node, agent, refresh=False: (
            calls.append(("get", node.name, agent, refresh)) or payload
        ),
    )
    monkeypatch.setattr(
        dashboard_app,
        "node_save_harness_model_preference",
        lambda node, agent, model, effort: (
            calls.append(("put", node.name, agent, model, effort)) or payload
        ),
    )
    monkeypatch.setattr(dashboard_app, "append_hub_event", lambda *_args, **_kwargs: None)
    client = TestClient(dashboard_app.create_app())

    loaded = client.get("/api/nodes/worker/agent-tools/claude/models?refresh=true")
    saved = client.put(
        "/api/nodes/worker/agent-tools/claude/models/preference",
        json={"model": "sonnet", "reasoning_effort": "high"},
    )

    assert loaded.status_code == 200
    assert loaded.headers["cache-control"] == "no-store"
    assert saved.status_code == 200
    assert saved.headers["cache-control"] == "no-store"
    assert calls == [
        ("get", "worker", "claude", True),
        ("put", "worker", "claude", "sonnet", "high"),
    ]


def test_dashboard_local_model_preference_round_trip(
    isolated_models: Path,
    monkeypatch,
) -> None:
    local = hub.NodeEntry(name="local", mode="local")
    monkeypatch.setattr(dashboard_app, "auth_enabled", lambda: False)
    monkeypatch.setattr(dashboard_app, "node_by_name", lambda node_id: local)
    monkeypatch.setattr(dashboard_app, "append_hub_event", lambda *_args, **_kwargs: None)
    client = TestClient(dashboard_app.create_app())

    saved = client.put(
        "/api/nodes/local/agent-tools/codex/models/preference",
        json={"model": "private/deepseek-v3", "reasoning_effort": "high"},
    )
    loaded = client.get("/api/nodes/local/agent-tools/codex/models")

    assert saved.status_code == 200
    assert saved.json()["selected_model"] == "private/deepseek-v3"
    assert saved.json()["selected_reasoning_effort"] == "high"
    assert loaded.status_code == 200
    assert loaded.json()["selected_model"] == "private/deepseek-v3"
    assert loaded.json()["effective_model"] == "private/deepseek-v3"
    assert loaded.json()["effective_reasoning_effort"] == "high"


def test_node_rejects_invalid_reasoning_preference(
    isolated_models: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    client = TestClient(node_app.create_app())

    response = client.put(
        "/api/agent-tools/codex/models/preference",
        headers={"Authorization": "Bearer node-secret"},
        json={"model": "gpt-safe", "reasoning_effort": "high; unsafe"},
    )

    assert response.status_code == 400
    assert "Reasoning effort" in response.json()["detail"]
