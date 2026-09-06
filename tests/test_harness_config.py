from __future__ import annotations

import stat
from pathlib import Path

import pytest

from staragent import harness_config


@pytest.fixture
def isolated_harness_home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path / "state"))
    for names in harness_config.HARNESS_ENVIRONMENT_HINTS.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    return home


def test_codex_config_is_validated_and_written_privately(isolated_harness_home: Path) -> None:
    payload = harness_config.save_harness_config("codex", 'model = "gpt-5"\n')
    path = isolated_harness_home / ".codex" / "config.toml"

    assert path.read_text(encoding="utf-8") == 'model = "gpt-5"\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert payload["config"]["path"] == str(path)
    assert payload["config"]["exists"] is True

    with pytest.raises(ValueError, match="Invalid TOML"):
        harness_config.save_harness_config("codex", "model = [")
    assert path.read_text(encoding="utf-8") == 'model = "gpt-5"\n'


def test_managed_environment_is_scoped_and_marks_secrets(
    isolated_harness_home: Path,
) -> None:
    payload = harness_config.save_harness_environment(
        "codex",
        {
            "DEEPSEEK_API_KEY": "secret-value",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
        },
    )

    variables = {item["name"]: item for item in payload["environment"]["variables"]}
    assert variables["DEEPSEEK_API_KEY"]["secret"] is True
    assert variables["OPENAI_BASE_URL"]["secret"] is False
    assert harness_config.harness_process_environment("codex", {"KEEP": "yes"}) == {
        "DEEPSEEK_API_KEY": "secret-value",
        "KEEP": "yes",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
    }

    state_path = harness_config.harness_environment_path()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("name", ["TMUX", "STARAGENT_AUTH_TOKEN", "NOT-A-NAME"])
def test_managed_environment_rejects_reserved_or_invalid_names(
    isolated_harness_home: Path,
    name: str,
) -> None:
    with pytest.raises(ValueError):
        harness_config.save_harness_environment("codex", {name: "value"})


def test_managed_codex_home_selects_the_config_file(
    isolated_harness_home: Path,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "custom-codex"
    harness_config.save_harness_environment("codex", {"CODEX_HOME": str(codex_home)})

    payload = harness_config.harness_configuration_payload("codex")

    assert payload["config"]["path"] == str(codex_home / "config.toml")
    assert payload["config"]["source"] == "CODEX_HOME"


def test_payload_reports_inherited_environment_without_exposing_values(
    isolated_harness_home: Path,
    monkeypatch,
) -> None:
    harness_config.save_harness_config(
        "codex",
        'model_provider = "private"\n\n[model_providers.private]\nenv_key = "PRIVATE_MODEL_KEY"\n',
    )
    monkeypatch.setenv("PRIVATE_MODEL_KEY", "do-not-return-this")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")

    payload = harness_config.harness_configuration_payload("codex")

    inherited = {item["name"]: item for item in payload["environment"]["inherited"]}
    assert inherited["PRIVATE_MODEL_KEY"] == {
        "name": "PRIVATE_MODEL_KEY",
        "secret": True,
        "configured": True,
        "overridden": False,
    }
    assert inherited["OPENAI_BASE_URL"]["secret"] is False
    assert all("value" not in item for item in inherited.values())
    assert payload["environment"]["path"] == str(harness_config.harness_environment_path())


def test_managed_environment_reports_when_it_overrides_service_environment(
    isolated_harness_home: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://service.invalid/v1")

    payload = harness_config.save_harness_environment(
        "codex",
        {"OPENAI_BASE_URL": "https://managed.invalid/v1"},
    )

    assert payload["environment"]["variables"][0]["value"] == ("https://managed.invalid/v1")
    inherited = payload["environment"]["inherited"]
    assert inherited == [
        {
            "name": "OPENAI_BASE_URL",
            "secret": False,
            "configured": True,
            "overridden": True,
        }
    ]


def test_remote_inherited_environment_is_allowlisted_and_never_carries_values() -> None:
    payload = harness_config.normalize_harness_configuration(
        "codex",
        {
            "supported": True,
            "config": {"format": "toml"},
            "environment": {
                "variables": [{"name": "MANAGED", "value": "safe-to-edit"}],
                "inherited": [
                    {
                        "name": "PRIVATE_TOKEN",
                        "value": "must-be-dropped",
                        "configured": True,
                    },
                    {"name": "NOT-A-NAME", "configured": True},
                ],
                "path": "/home/user/.staragent/harness-environment.json",
            },
        },
    )

    assert payload["environment"]["inherited"] == [
        {
            "name": "PRIVATE_TOKEN",
            "secret": True,
            "configured": True,
            "overridden": False,
        }
    ]
    assert payload["environment"]["path"] == ("/home/user/.staragent/harness-environment.json")
