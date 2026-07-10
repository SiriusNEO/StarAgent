from __future__ import annotations

import json
import subprocess

from staragent import agent_tools, agent_usage


def test_codex_usage_reports_remaining_windows_and_reset_count(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_usage,
        "codex_app_server_request",
        lambda executable, method, timeout: {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 21,
                        "windowDurationMins": 300,
                        "resetsAt": 1_783_701_149,
                    },
                    "secondary": {
                        "usedPercent": 9,
                        "windowDurationMins": 10_080,
                        "resetsAt": 1_784_251_747,
                    },
                    "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                    "planType": "pro",
                }
            },
            "rateLimitResetCredits": {
                "availableCount": 4,
                "credits": [{"id": "must-not-leak"}],
            },
        },
    )

    usage = agent_usage.probe_codex_usage("/tools/codex")

    assert usage["status"] == "available"
    assert usage["reset_credits"] == 4
    bucket = usage["buckets"][0]
    assert bucket["plan"] == "pro"
    assert bucket["windows"][0]["label"] == "5 hours"
    assert bucket["windows"][0]["remaining_percent"] == 79.0
    assert bucket["windows"][1]["label"] == "Weekly"
    assert bucket["windows"][1]["remaining_percent"] == 91.0
    assert "must-not-leak" not in json.dumps(usage)


def test_claude_usage_is_manual_and_checks_auth_without_a_model_request(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {"loggedIn": True, "authMethod": "oauth_token", "email": "hidden@example.com"}
            ),
            "",
        )

    monkeypatch.setattr(agent_usage.subprocess, "run", fake_run)

    usage = agent_usage.probe_claude_usage("/tools/claude")

    assert calls == [["/tools/claude", "auth", "status", "--json"]]
    assert usage["status"] == "manual"
    assert usage["authenticated"] is True
    assert usage["auth_method"] == "oauth_token"
    assert usage["action"] == "/status"
    assert "hidden@example.com" not in json.dumps(usage)


def test_remote_usage_normalization_allowlists_and_clamps_values() -> None:
    normalized = agent_tools.normalize_agent_tools_payload(
        {
            "tools": [
                {
                    "name": "codex",
                    "status": "available",
                    "usage": {
                        "status": "available",
                        "source": "codex-app-server",
                        "secret": "drop-me",
                        "buckets": [
                            {
                                "id": "codex",
                                "label": "Codex",
                                "plan": "pro",
                                "windows": [
                                    {
                                        "kind": "primary",
                                        "label": "5 hours",
                                        "used_percent": 140,
                                        "remaining_percent": 99,
                                        "window_minutes": 300,
                                        "resets_at": "2026-07-10T12:00:00Z",
                                    }
                                ],
                            }
                        ],
                    },
                }
            ]
        }
    )

    codex = next(item for item in normalized["tools"] if item["name"] == "codex")
    usage = codex["usage"]
    window = usage["buckets"][0]["windows"][0]
    assert window["used_percent"] == 100.0
    assert window["remaining_percent"] == 0.0
    assert "secret" not in usage


def test_missing_supported_cli_reports_usage_unavailable() -> None:
    spec = agent_tools.agent_tool_spec("codex")
    assert spec is not None

    tool = agent_tools.tool_status(spec, status="missing")

    assert tool["usage"]["status"] == "unavailable"
