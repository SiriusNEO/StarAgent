from __future__ import annotations

import json
import subprocess

from staragent import agent_auth, agent_tools


def test_codex_auth_reports_login_method_without_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_auth.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            "Logged in using ChatGPT\n",
            "",
        ),
    )

    auth = agent_auth.probe_codex_auth("/tools/codex")

    assert auth["status"] == "authenticated"
    assert auth["authenticated"] is True
    assert auth["method"] == "ChatGPT"
    assert auth["action"] == ""


def test_codex_auth_reports_signed_out_with_login_command(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_auth.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "Not logged in\n"),
    )

    auth = agent_auth.probe_codex_auth("/tools/codex")

    assert auth["status"] == "not_authenticated"
    assert auth["authenticated"] is False
    assert auth["action"] == "codex login"


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
            "action": "printenv OPENAI_API_KEY",
            "email": "must-not-leak@example.com",
            "secret": "drop-me",
            "provider_count": 9999,
        },
    )

    assert normalized["status"] == "authenticated"
    assert normalized["action"] == ""
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
