from __future__ import annotations

import json
import subprocess

import pytest

from staragent import agent_auth, agent_tools


def codex_diagnostics(
    *,
    provider: str = "openai",
    provider_config: dict[str, object] | None = None,
    account: object = None,
    requires_openai_auth: bool = True,
) -> dict[str, object]:
    config: dict[str, object] = {"model_provider": provider}
    if provider_config is not None:
        config["model_providers"] = {provider: provider_config}
    return {
        "account/read": {
            "account": account,
            "requiresOpenaiAuth": requires_openai_auth,
        },
        "config/read": {"config": config},
    }


def force_codex_login_status_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        agent_auth,
        "codex_app_server_requests",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unsupported")),
    )
    monkeypatch.setattr(agent_auth, "probe_codex_doctor_auth", lambda *args: None)


def test_codex_api_key_login_uses_stdin_without_exposing_the_key(monkeypatch) -> None:
    secret = "sk-test-private-value"
    call: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        call.update({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="Login successful\n", stderr="")

    monkeypatch.setattr(agent_auth, "auth_environment", lambda _agent: {"PATH": "/tools"})
    monkeypatch.setattr(agent_auth.shutil, "which", lambda *_args, **_kwargs: "/tools/codex")
    monkeypatch.setattr(agent_auth.subprocess, "run", fake_run)

    result = agent_auth.login_codex_with_api_key(secret)

    assert call["argv"] == ["/tools/codex", "login", "--with-api-key"]
    assert call["input"] == f"{secret}\n"
    assert secret not in " ".join(call["argv"])
    assert result == {
        "ok": True,
        "status": "authenticated",
        "credential_type": "api_key",
        "detail": "Codex accepted and stored the API key on this Node.",
    }


def test_codex_api_key_login_redacts_cli_error_output(monkeypatch) -> None:
    secret = "sk-test-must-not-leak"
    monkeypatch.setattr(agent_auth, "auth_environment", lambda _agent: {"PATH": "/tools"})
    monkeypatch.setattr(agent_auth.shutil, "which", lambda *_args, **_kwargs: "/tools/codex")
    monkeypatch.setattr(
        agent_auth.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr=f"Rejected credential {secret}\n",
        ),
    )

    result = agent_auth.login_codex_with_api_key(secret)

    assert result["ok"] is False
    assert secret not in str(result)
    assert "[REDACTED]" in result["detail"]


def test_logout_agent_uses_the_claude_allowlisted_command(monkeypatch) -> None:
    call: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        call.update({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="Logged out\n", stderr="")

    monkeypatch.setattr(agent_auth, "auth_environment", lambda _agent: {"PATH": "/tools"})
    monkeypatch.setattr(agent_auth.shutil, "which", lambda *_args, **_kwargs: "/tools/claude")
    monkeypatch.setattr(agent_auth.subprocess, "run", fake_run)

    result = agent_auth.logout_agent("claude")

    assert call["argv"] == ["/tools/claude", "auth", "logout"]
    assert call["stdin"] is subprocess.DEVNULL
    assert "shell" not in call
    assert result == {
        "ok": True,
        "status": "not_authenticated",
        "detail": "Claude Code credentials were removed from this Node.",
    }


def test_logout_agent_requires_an_explicit_supported_flow() -> None:
    with pytest.raises(ValueError, match="Interactive logout is required"):
        agent_auth.logout_agent("opencode")


def test_codex_auth_reports_login_method_without_identity(monkeypatch) -> None:
    force_codex_login_status_fallback(monkeypatch)

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["creationflags"] == 0x0800_0000
        return subprocess.CompletedProcess(args, 0, "Logged in using ChatGPT\n", "")

    monkeypatch.setattr(agent_auth.subprocess, "run", fake_run)
    monkeypatch.setattr(
        agent_auth,
        "background_process_kwargs",
        lambda: {"creationflags": 0x0800_0000},
    )

    auth = agent_auth.probe_codex_auth("/tools/codex")

    assert auth["status"] == "authenticated"
    assert auth["authenticated"] is True
    assert auth["method"] == "ChatGPT"
    assert auth["action"] == ""


def test_codex_auth_reports_signed_out_with_login_command(monkeypatch) -> None:
    force_codex_login_status_fallback(monkeypatch)
    monkeypatch.setattr(
        agent_auth.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "Not logged in\n"),
    )

    auth = agent_auth.probe_codex_auth("/tools/codex")

    assert auth["status"] == "not_authenticated"
    assert auth["authenticated"] is False
    assert auth["action"] == "codex login"


def test_codex_custom_provider_uses_configured_environment_credential(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    monkeypatch.setattr(
        agent_auth,
        "codex_app_server_requests",
        lambda *args, **kwargs: codex_diagnostics(
            provider="deepseek",
            provider_config={"name": "DeepSeek", "env_key": "DEEPSEEK_API_KEY"},
            requires_openai_auth=False,
        ),
    )

    auth = agent_auth.probe_codex_auth("/tools/codex")

    assert auth["status"] == "configured"
    assert auth["authenticated"] is None
    assert auth["provider"] == "DeepSeek"
    assert auth["credential_type"] == "environment"
    assert auth["credential_name"] == "DEEPSEEK_API_KEY"
    assert auth["action"] == ""
    assert "must-not-leak" not in json.dumps(auth)


def test_codex_custom_provider_reports_missing_environment_without_login_action(
    monkeypatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        agent_auth,
        "codex_app_server_requests",
        lambda *args, **kwargs: codex_diagnostics(
            provider="deepseek",
            provider_config={"name": "DeepSeek", "env_key": "DEEPSEEK_API_KEY"},
            requires_openai_auth=False,
        ),
    )

    auth = agent_auth.probe_codex_auth("/tools/codex")

    assert auth["status"] == "not_configured"
    assert auth["authenticated"] is None
    assert auth["provider"] == "DeepSeek"
    assert auth["credential_name"] == "DEEPSEEK_API_KEY"
    assert auth["action"] == ""


def test_codex_provider_without_auth_is_ready_without_openai_login(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_auth,
        "codex_app_server_requests",
        lambda *args, **kwargs: codex_diagnostics(
            provider="local-proxy",
            provider_config={"name": "Local proxy"},
            requires_openai_auth=False,
        ),
    )

    auth = agent_auth.probe_codex_auth("/tools/codex")

    assert auth["status"] == "configured"
    assert auth["provider"] == "Local proxy"
    assert auth["credential_type"] == "none"
    assert auth["action"] == ""


def test_codex_openai_auth_drops_account_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_auth,
        "codex_app_server_requests",
        lambda *args, **kwargs: codex_diagnostics(
            account={
                "type": "chatgpt",
                "email": "must-not-leak@example.com",
                "planType": "pro",
            },
        ),
    )

    auth = agent_auth.probe_codex_auth("/tools/codex")

    assert auth["status"] == "authenticated"
    assert auth["provider"] == "OpenAI"
    assert auth["credential_type"] == "chatgpt"
    assert "must-not-leak" not in json.dumps(auth)
    assert "planType" not in auth


def test_codex_doctor_fallback_understands_custom_provider_environment() -> None:
    auth = agent_auth.codex_auth_from_doctor(
        {
            "checks": {
                "config.load": {
                    "details": {"model provider": "deepseek"},
                },
                "auth.credentials": {
                    "status": "ok",
                    "details": {
                        "model provider requires OpenAI auth": "false",
                        "provider auth env var": "DEEPSEEK_API_KEY (present)",
                    },
                },
                "network.websocket_reachability": {
                    "details": {"provider name": "DeepSeek"},
                },
            }
        }
    )

    assert auth is not None
    assert auth["status"] == "configured"
    assert auth["provider"] == "DeepSeek"
    assert auth["credential_name"] == "DEEPSEEK_API_KEY"


def test_claude_auth_allowlists_status_and_drops_account_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_auth.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "oauth_token",
                    "email": "must-not-leak@example.com",
                    "subscriptionType": "max",
                }
            ),
            "",
        ),
    )

    auth = agent_auth.probe_claude_auth("/tools/claude")

    assert auth["status"] == "authenticated"
    assert auth["method"] == "oauth_token"
    assert "must-not-leak" not in json.dumps(auth)
    assert "subscriptionType" not in auth


def test_claude_auth_uses_signed_out_json_even_with_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_auth.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            1,
            json.dumps({"loggedIn": False, "authMethod": "none"}),
            "",
        ),
    )

    auth = agent_auth.probe_claude_auth("/tools/claude")

    assert auth["status"] == "not_authenticated"
    assert auth["authenticated"] is False
    assert auth["action"] == "claude auth login"


def test_claude_auth_prefers_effective_environment_credentials_without_exposing_values(
    monkeypatch,
) -> None:
    secret = "sk-ant-do-not-return"
    monkeypatch.setattr(
        agent_auth,
        "auth_environment",
        lambda _agent: {"ANTHROPIC_API_KEY": secret},
    )
    monkeypatch.setattr(
        agent_auth.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("environment auth should not invoke the CLI"),
    )

    auth = agent_auth.probe_claude_auth("/tools/claude")

    assert auth["status"] == "configured"
    assert auth["credential_type"] == "environment"
    assert auth["credential_name"] == "ANTHROPIC_API_KEY"
    assert auth["method"] == "Anthropic API key"
    assert secret not in json.dumps(auth)


def test_claude_auth_recognizes_cloud_provider_selection(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_auth,
        "auth_environment",
        lambda _agent: {"CLAUDE_CODE_USE_BEDROCK": "1"},
    )
    monkeypatch.setattr(
        agent_auth.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("cloud auth should not invoke the CLI"),
    )

    auth = agent_auth.probe_claude_auth("/tools/claude")

    assert auth["status"] == "configured"
    assert auth["provider"] == "Amazon Bedrock"
    assert auth["credential_name"] == "CLAUDE_CODE_USE_BEDROCK"


def test_opencode_auth_reports_provider_count_without_provider_names(monkeypatch) -> None:
    output = """Credentials ~/.local/share/opencode/auth.json
● Anthropic oauth
● OpenAI api
2 credentials
Environment
● OpenAI OPENAI_API_KEY
1 environment variable
"""
    monkeypatch.setattr(
        agent_auth.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, output, ""),
    )

    auth = agent_auth.probe_opencode_auth("/tools/opencode")

    assert auth["status"] == "configured"
    assert auth["provider_count"] == 3
    assert "Anthropic" not in json.dumps(auth)
    assert "OPENAI_API_KEY" not in json.dumps(auth)


def test_remote_auth_normalization_drops_unknown_fields_and_commands() -> None:
    normalized = agent_auth.normalize_agent_auth(
        "codex",
        {
            "status": "authenticated",
            "method": "ChatGPT",
            "provider": "OpenAI",
            "credential_type": "chatgpt",
            "credential_name": "SHOULD_BE_DROPPED",
            "action": "printenv OPENAI_API_KEY",
            "email": "must-not-leak@example.com",
            "secret": "drop-me",
            "provider_count": 9999,
        },
    )

    assert normalized["status"] == "authenticated"
    assert normalized["action"] == ""
    assert normalized["provider"] == "OpenAI"
    assert normalized["credential_type"] == "chatgpt"
    assert normalized["credential_name"] == ""
    assert normalized["provider_count"] == 100
    assert "email" not in normalized
    assert "secret" not in normalized


def test_agent_tool_status_includes_normalized_login_state() -> None:
    spec = agent_tools.agent_tool_spec("codex")
    assert spec is not None

    tool = agent_tools.tool_status(
        spec,
        status="available",
        auth={"status": "authenticated", "method": "ChatGPT", "secret": "drop-me"},
    )

    assert tool["auth"]["status"] == "authenticated"
    assert tool["auth"]["method"] == "ChatGPT"
    assert "secret" not in tool["auth"]
