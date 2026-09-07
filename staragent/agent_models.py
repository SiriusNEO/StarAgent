from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from staragent.agent_auth import codex_provider
from staragent.codex_app_server import codex_app_server_requests
from staragent.event_log import redact_log_text
from staragent.harness_config import (
    MAX_CONFIG_BYTES,
    harness_config_path,
    harness_config_spec,
    harness_process_environment,
)
from staragent.paths import state_dir
from staragent.state import atomic_write_json, locked_file, read_json
from staragent.text import strip_ansi
from staragent.windows import background_process_kwargs, windows_process_argv

MODEL_CATALOG_TIMEOUT_SECONDS = 10.0
MODEL_CATALOG_CACHE_SECONDS = 300.0
MAX_MODEL_ID_CHARS = 512
MAX_MODEL_LABEL_CHARS = 120
MAX_MODEL_DESCRIPTION_CHARS = 400
MAX_MODEL_CATALOG_ENTRIES = 400
MAX_REASONING_EFFORT_CHARS = 80
MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]*")
REASONING_EFFORT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]*")
COMMAND_HEAD_PATTERN = re.compile(
    r"^(?P<executable>\"[^\"]+\"|'[^']+'|\S+)(?P<rest>.*)$", re.DOTALL
)

HARNESS_COMMANDS = {
    "codex": "codex",
    "claude": "claude",
    "opencode": "opencode",
}
HARNESS_MODEL_DOCS = {
    "codex": "https://learn.chatgpt.com/docs/config-file/config-basic",
    "claude": "https://code.claude.com/docs/en/model-config",
    "opencode": "https://opencode.ai/docs/models/",
}
CODEX_FALLBACK_REASONING_EFFORTS = (
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)
CLAUDE_FALLBACK_REASONING_EFFORTS = (
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
OPENCODE_PROVIDER_VARIANTS = {
    "anthropic": ("high", "max"),
    "google": ("low", "high"),
    "openai": ("none", "minimal", "low", "medium", "high", "xhigh"),
}
CLAUDE_MODEL_ALIASES = (
    ("default", "Automatic", "Use the runtime default for this account."),
    ("best", "Best", "Use the most capable model available to this account."),
    ("fable", "Fable", "For the hardest and longest-running tasks."),
    ("sonnet", "Sonnet", "Balanced for everyday coding work."),
    ("opus", "Opus", "For complex reasoning and demanding work."),
    ("haiku", "Haiku", "Fast and efficient for simpler tasks."),
    ("sonnet[1m]", "Sonnet · 1M", "Sonnet with the extended context window."),
    ("opus[1m]", "Opus · 1M", "Opus with the extended context window."),
    ("opusplan", "Opus Plan", "Use Opus for planning and Sonnet for execution."),
)
KNOWN_HARNESS_SUBCOMMANDS = {
    "codex": {
        "app-server",
        "cloud",
        "completion",
        "debug",
        "exec",
        "features",
        "fork",
        "login",
        "logout",
        "mcp",
        "mcp-server",
        "resume",
        "sandbox",
    },
    "claude": {
        "agents",
        "auth",
        "auto-mode",
        "doctor",
        "install",
        "mcp",
        "plugin",
        "plugins",
        "project",
        "setup-token",
        "ultrareview",
        "update",
        "upgrade",
    },
    "opencode": {
        "agent",
        "attach",
        "auth",
        "completion",
        "debug",
        "github",
        "mcp",
        "models",
        "run",
        "serve",
        "session",
        "stats",
        "tui",
        "uninstall",
        "upgrade",
        "web",
    },
}
RESUME_OPTIONS = {
    "codex": {"resume", "fork"},
    "claude": {"--resume", "-r", "--continue", "-c"},
    "opencode": {"--continue", "-c", "--session", "-s"},
}

_MODEL_CATALOG_LOCK = threading.Lock()
_MODEL_CATALOG_CACHE: dict[tuple[str, str], tuple[float, dict[str, object]]] = {}


def harness_models_path() -> Path:
    return state_dir() / "harness-models.json"


def harness_launch_preferences(agent: str) -> tuple[str, str]:
    spec = harness_config_spec(agent)
    path = harness_models_path()
    with locked_file(path):
        payload = read_json(path, {})
    models = payload.get("models") if isinstance(payload, dict) else None
    efforts = payload.get("reasoning_efforts") if isinstance(payload, dict) else None
    try:
        model = validate_model_id(
            models.get(spec.name) if isinstance(models, dict) else "",
            allow_empty=True,
        )
    except ValueError:
        model = ""
    try:
        effort = validate_reasoning_effort(
            spec.name,
            efforts.get(spec.name) if isinstance(efforts, dict) else "",
            allow_empty=True,
        )
    except ValueError:
        effort = ""
    return model, effort


def harness_model_preference(agent: str) -> str:
    return harness_launch_preferences(agent)[0]


def harness_reasoning_effort_preference(agent: str) -> str:
    return harness_launch_preferences(agent)[1]


def save_harness_model_preference(
    agent: str,
    model: object,
    reasoning_effort: object = "",
) -> dict[str, object]:
    spec = harness_config_spec(agent)
    normalized = validate_model_id(model, allow_empty=True)
    normalized_effort = validate_reasoning_effort(
        spec.name,
        reasoning_effort,
        allow_empty=True,
    )
    path = harness_models_path()
    try:
        with locked_file(path):
            payload = read_json(path, {})
            current_models = payload.get("models") if isinstance(payload, dict) else None
            models = {
                name: cleaned
                for name, value in (
                    current_models.items() if isinstance(current_models, dict) else ()
                )
                if name in HARNESS_COMMANDS and (cleaned := clean_model_id(value))
            }
            current_efforts = (
                payload.get("reasoning_efforts") if isinstance(payload, dict) else None
            )
            efforts = {
                name: cleaned
                for name, value in (
                    current_efforts.items() if isinstance(current_efforts, dict) else ()
                )
                if name in HARNESS_COMMANDS and (cleaned := clean_reasoning_effort(name, value))
            }
            if normalized:
                models[spec.name] = normalized
            else:
                models.pop(spec.name, None)
            if normalized_effort:
                efforts[spec.name] = normalized_effort
            else:
                efforts.pop(spec.name, None)
            atomic_write_json(
                path,
                {"version": 2, "models": models, "reasoning_efforts": efforts},
                mode=0o600,
            )
    except OSError as exc:
        raise RuntimeError(f"Could not save the {spec.label} launch preferences: {exc}") from exc
    return agent_models_payload(spec.name)


def validate_model_id(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Model ID must be text.")
    model = value.strip()
    if not model and allow_empty:
        return ""
    if not model:
        raise ValueError("Model ID is required.")
    if len(model) > MAX_MODEL_ID_CHARS:
        raise ValueError(f"Model ID exceeds {MAX_MODEL_ID_CHARS} characters.")
    if not MODEL_ID_PATTERN.fullmatch(model):
        raise ValueError("Model ID contains unsupported characters.")
    return model


def validate_reasoning_effort(
    agent: str,
    value: object,
    *,
    allow_empty: bool = False,
) -> str:
    spec = harness_config_spec(agent)
    if not isinstance(value, str):
        raise ValueError("Reasoning effort must be text.")
    effort = value.strip()
    if spec.name != "opencode":
        effort = effort.lower()
    if not effort and allow_empty:
        return ""
    if not effort:
        raise ValueError("Reasoning effort is required.")
    if len(effort) > MAX_REASONING_EFFORT_CHARS:
        raise ValueError(f"Reasoning effort exceeds {MAX_REASONING_EFFORT_CHARS} characters.")
    if not REASONING_EFFORT_PATTERN.fullmatch(effort):
        raise ValueError("Reasoning effort contains unsupported characters.")
    return effort


def apply_harness_model_preference(command: str) -> str:
    """Add the Node's model and reasoning preferences to a new Harness launch."""
    normalized = command.strip()
    try:
        argv = shlex.split(normalized, posix=os.name != "nt")
    except ValueError:
        return normalized
    if not argv:
        return normalized
    executable = Path(argv[0].strip('"')).stem.lower()
    agent = executable if executable in HARNESS_COMMANDS else ""
    if not agent:
        return normalized
    options = argv[1:]
    if any(item in RESUME_OPTIONS[agent] for item in options):
        return normalized
    first_positional = next((item for item in options if not item.startswith("-")), "")
    if first_positional in KNOWN_HARNESS_SUBCOMMANDS[agent]:
        return normalized
    model, reasoning_effort = harness_launch_preferences(agent)
    has_model = any(
        item in {"--model", "-m"} or item.startswith(("--model=", "-m=")) for item in options
    ) or (agent == "codex" and codex_has_config_override(options, "model"))
    has_effort = harness_command_has_reasoning_effort(agent, options)
    injected: list[str] = []
    if model and not has_model:
        injected.extend(("--model", shell_model_argument(model)))
    if reasoning_effort and not has_effort:
        if agent == "codex":
            injected.extend(("--config", f"model_reasoning_effort={reasoning_effort}"))
        elif agent == "claude":
            injected.extend(("--effort", shell_model_argument(reasoning_effort)))
        else:
            injected.extend(("--variant", shell_model_argument(reasoning_effort)))
    if not injected:
        return normalized
    # Keep the user's command spelling intact and put this global option before prompts or
    # project paths. Quoting also protects Claude's bracketed context aliases from shell globbing.
    match = COMMAND_HEAD_PATTERN.match(normalized)
    if not match:
        return normalized
    return f"{match.group('executable')} {' '.join(injected)}{match.group('rest')}"


def harness_command_has_reasoning_effort(agent: str, options: list[str]) -> bool:
    if agent == "codex":
        return codex_has_config_override(options, "model_reasoning_effort")
    option = "--effort" if agent == "claude" else "--variant"
    return any(item == option or item.startswith(f"{option}=") for item in options)


def codex_has_config_override(options: list[str], key: str) -> bool:
    for index, item in enumerate(options):
        value = ""
        if item in {"-c", "--config"} and index + 1 < len(options):
            value = options[index + 1]
        elif item.startswith("--config=") or item.startswith("-c="):
            value = item.split("=", 1)[1]
        if value.split("=", 1)[0].strip() == key:
            return True
    return False


def shell_model_argument(model: str) -> str:
    if os.name == "nt":
        # Model IDs cannot contain quotes, so this is valid for cmd.exe and PowerShell.
        return f'"{model}"'
    return shlex.quote(model)


def agent_models_payload(agent: str, *, force: bool = False) -> dict[str, object]:
    spec = harness_config_spec(agent)
    environment = harness_process_environment(spec.name)
    executable = shutil.which(HARNESS_COMMANDS[spec.name], path=environment.get("PATH"))
    discovery = cached_model_discovery(
        spec.name,
        executable or "",
        environment,
        force=force,
    )
    models = normalize_model_entries(discovery.get("models"), agent=spec.name)
    selected_model, selected_reasoning_effort = harness_launch_preferences(spec.name)
    configured_model = clean_model_id(discovery.get("configured_model"))
    default_model = clean_model_id(discovery.get("default_model"))
    for model in (selected_model, configured_model, default_model):
        if model and not any(item["id"] == model for item in models):
            models.append(
                model_entry(
                    model,
                    agent=spec.name,
                    provider=model_provider_for_id(spec.name, model, discovery),
                    source="configuration",
                )
            )
    for entry in models:
        if not entry.get("reasoning_efforts"):
            entry["reasoning_efforts"] = fallback_reasoning_efforts(
                spec.name,
                str(entry["id"]),
                discovery,
            )
    effective_model = selected_model or configured_model or default_model
    if selected_model:
        effective_source = "staragent"
    elif configured_model:
        effective_source = str(discovery.get("configured_source") or "harness_config")
    elif default_model:
        effective_source = "harness_default"
    else:
        effective_source = "automatic"
    configured_reasoning_effort = clean_reasoning_effort(
        spec.name,
        discovery.get("configured_reasoning_effort"),
    )
    default_reasoning_effort = model_default_reasoning_effort(
        spec.name,
        models,
        effective_model,
    )
    if not default_reasoning_effort:
        default_reasoning_effort = clean_reasoning_effort(
            spec.name,
            discovery.get("default_reasoning_effort"),
        )
    reasoning_efforts = reasoning_efforts_for_model(
        spec.name,
        effective_model,
        models,
        discovery,
    )
    if selected_reasoning_effort and not any(
        item["id"] == selected_reasoning_effort for item in reasoning_efforts
    ):
        reasoning_efforts.append(reasoning_effort_entry(spec.name, selected_reasoning_effort))
    effective_reasoning_effort = (
        selected_reasoning_effort or configured_reasoning_effort or default_reasoning_effort
    )
    if selected_reasoning_effort:
        reasoning_effort_source = "staragent"
    elif configured_reasoning_effort:
        reasoning_effort_source = str(
            discovery.get("configured_reasoning_effort_source")
            or discovery.get("configured_source")
            or "harness_config"
        )
    elif default_reasoning_effort:
        reasoning_effort_source = "harness_default"
    else:
        reasoning_effort_source = "automatic"
    return {
        "supported": True,
        "agent": spec.name,
        "installed": bool(executable),
        "selected_model": selected_model,
        "configured_model": configured_model,
        "default_model": default_model,
        "effective_model": effective_model,
        "effective_source": effective_source,
        "selected_reasoning_effort": selected_reasoning_effort,
        "configured_reasoning_effort": configured_reasoning_effort,
        "default_reasoning_effort": default_reasoning_effort,
        "effective_reasoning_effort": effective_reasoning_effort,
        "reasoning_effort_source": reasoning_effort_source,
        "reasoning_effort_supported": bool(
            discovery.get("reasoning_effort_supported", reasoning_efforts)
        ),
        "reasoning_effort_kind": "variant" if spec.name == "opencode" else "effort",
        "reasoning_efforts": reasoning_efforts,
        "provider": clean_model_text(discovery.get("provider"), max_chars=80),
        "models": models[:MAX_MODEL_CATALOG_ENTRIES],
        "catalog_source": clean_model_text(discovery.get("catalog_source"), max_chars=80),
        "refresh_supported": bool(discovery.get("refresh_supported")),
        "docs_url": HARNESS_MODEL_DOCS[spec.name],
        "checked_at": utc_timestamp(),
        "error": clean_model_error(discovery.get("error")),
    }


def unavailable_agent_models(agent: str, error: object) -> dict[str, object]:
    spec = harness_config_spec(agent)
    return {
        "supported": False,
        "agent": spec.name,
        "installed": False,
        "selected_model": "",
        "configured_model": "",
        "default_model": "",
        "effective_model": "",
        "effective_source": "automatic",
        "selected_reasoning_effort": "",
        "configured_reasoning_effort": "",
        "default_reasoning_effort": "",
        "effective_reasoning_effort": "",
        "reasoning_effort_source": "automatic",
        "reasoning_effort_supported": False,
        "reasoning_effort_kind": "variant" if spec.name == "opencode" else "effort",
        "reasoning_efforts": [],
        "provider": "",
        "models": [],
        "catalog_source": "",
        "refresh_supported": False,
        "docs_url": HARNESS_MODEL_DOCS[spec.name],
        "checked_at": utc_timestamp(),
        "error": clean_model_error(error),
    }


def normalize_agent_models_payload(agent: str, payload: object) -> dict[str, object]:
    spec = harness_config_spec(agent)
    if not isinstance(payload, dict):
        return unavailable_agent_models(spec.name, "Node returned an invalid model response.")
    models = normalize_model_entries(payload.get("models"), agent=spec.name)
    selected = clean_model_id(payload.get("selected_model"))
    configured = clean_model_id(payload.get("configured_model"))
    default = clean_model_id(payload.get("default_model"))
    effective = selected or configured or default
    source = clean_model_text(payload.get("effective_source"), max_chars=40)
    if source not in {
        "staragent",
        "environment",
        "environment_default",
        "harness_config",
        "harness_default",
        "automatic",
    }:
        source = "staragent" if selected else "automatic"
    selected_effort = clean_reasoning_effort(spec.name, payload.get("selected_reasoning_effort"))
    configured_effort = clean_reasoning_effort(
        spec.name,
        payload.get("configured_reasoning_effort"),
    )
    default_effort = clean_reasoning_effort(
        spec.name,
        payload.get("default_reasoning_effort"),
    )
    effective_effort = selected_effort or configured_effort or default_effort
    effort_source = clean_model_text(payload.get("reasoning_effort_source"), max_chars=40)
    if effort_source not in {
        "staragent",
        "environment",
        "environment_default",
        "harness_config",
        "harness_default",
        "automatic",
    }:
        effort_source = "staragent" if selected_effort else "automatic"
    efforts = normalize_reasoning_effort_entries(
        spec.name,
        payload.get("reasoning_efforts"),
    )
    return {
        "supported": bool(payload.get("supported", True)),
        "agent": spec.name,
        "installed": bool(payload.get("installed")),
        "selected_model": selected,
        "configured_model": configured,
        "default_model": default,
        "effective_model": effective,
        "effective_source": source,
        "selected_reasoning_effort": selected_effort,
        "configured_reasoning_effort": configured_effort,
        "default_reasoning_effort": default_effort,
        "effective_reasoning_effort": effective_effort,
        "reasoning_effort_source": effort_source,
        "reasoning_effort_supported": bool(payload.get("reasoning_effort_supported")),
        "reasoning_effort_kind": "variant" if spec.name == "opencode" else "effort",
        "reasoning_efforts": efforts,
        "provider": clean_model_text(payload.get("provider"), max_chars=80),
        "models": models,
        "catalog_source": clean_model_text(payload.get("catalog_source"), max_chars=80),
        "refresh_supported": bool(payload.get("refresh_supported")),
        "docs_url": HARNESS_MODEL_DOCS[spec.name],
        "checked_at": clean_model_text(payload.get("checked_at"), max_chars=80),
        "error": clean_model_error(payload.get("error")),
    }


def clear_agent_models_cache(agent: str = "") -> None:
    normalized = str(agent or "").strip().lower()
    with _MODEL_CATALOG_LOCK:
        if not normalized:
            _MODEL_CATALOG_CACHE.clear()
            return
        for key in [key for key in _MODEL_CATALOG_CACHE if key[0] == normalized]:
            _MODEL_CATALOG_CACHE.pop(key, None)


def cached_model_discovery(
    agent: str,
    executable: str,
    environment: Mapping[str, str],
    *,
    force: bool,
) -> dict[str, object]:
    key = (agent, executable)
    now = time.monotonic()
    with _MODEL_CATALOG_LOCK:
        cached = _MODEL_CATALOG_CACHE.get(key)
        if cached and not force and now - cached[0] < MODEL_CATALOG_CACHE_SECONDS:
            return dict(cached[1])
    discovered = discover_models(agent, executable, environment, refresh=force)
    with _MODEL_CATALOG_LOCK:
        _MODEL_CATALOG_CACHE[key] = (time.monotonic(), dict(discovered))
    return discovered


def discover_models(
    agent: str,
    executable: str,
    environment: Mapping[str, str],
    *,
    refresh: bool,
) -> dict[str, object]:
    if agent == "codex":
        return discover_codex_models(executable, environment)
    if agent == "claude":
        return discover_claude_models(executable, environment)
    return discover_opencode_models(executable, environment, refresh=refresh)


def discover_codex_models(
    executable: str,
    environment: Mapping[str, str],
) -> dict[str, object]:
    fallback_config = read_config_object("codex", environment)
    provider_id, provider_name, _provider_config = codex_provider(fallback_config)
    discovered: dict[str, object] = {
        "models": [],
        "configured_model": codex_configured_model(fallback_config),
        "configured_source": "harness_config",
        "configured_reasoning_effort": codex_configured_reasoning_effort(fallback_config),
        "configured_reasoning_effort_source": "harness_config",
        "reasoning_effort_supported": True,
        "provider": provider_name or provider_id,
        "catalog_source": "configuration",
        "refresh_supported": False,
        "error": "",
    }
    if not executable:
        return discovered
    try:
        responses = codex_app_server_requests(
            executable,
            {
                "config/read": {"includeLayers": False},
                "model/list": {"limit": MAX_MODEL_CATALOG_ENTRIES, "includeHidden": False},
            },
            timeout=MODEL_CATALOG_TIMEOUT_SECONDS,
            env=environment,
        )
        config_result = responses.get("config/read", {})
        config = config_result.get("config") if isinstance(config_result, dict) else None
        if isinstance(config, dict):
            provider_id, provider_name, _provider_config = codex_provider(config)
            discovered["provider"] = provider_name or provider_id
            discovered["configured_model"] = codex_configured_model(config)
            discovered["configured_reasoning_effort"] = codex_configured_reasoning_effort(config)
        model_result = responses.get("model/list", {})
        raw_models = model_result.get("data") if isinstance(model_result, dict) else None
        models = []
        if isinstance(raw_models, list):
            for raw in raw_models[:MAX_MODEL_CATALOG_ENTRIES]:
                if not isinstance(raw, dict):
                    continue
                model_id = clean_model_id(raw.get("id") or raw.get("model"))
                if not model_id:
                    continue
                models.append(
                    model_entry(
                        model_id,
                        label=raw.get("displayName") or raw.get("name") or model_id,
                        provider=provider_name or provider_id,
                        description=raw.get("description"),
                        default=bool(raw.get("isDefault")),
                        source="codex-app-server",
                        reasoning_efforts=raw.get("supportedReasoningEfforts"),
                        default_reasoning_effort=raw.get("defaultReasoningEffort"),
                    )
                )
        discovered["models"] = models
        discovered["default_model"] = next(
            (str(item["id"]) for item in models if item.get("default")),
            "",
        )
        discovered["catalog_source"] = "codex-app-server"
        discovered["refresh_supported"] = True
    except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        discovered["error"] = f"Codex model discovery is unavailable: {exc}"
    return discovered


def codex_configured_model(config: Mapping[str, object]) -> str:
    managed = config.get("models")
    new_thread = managed.get("new_thread") if isinstance(managed, dict) else None
    new_thread_model = (
        clean_model_id(new_thread.get("model")) if isinstance(new_thread, dict) else ""
    )
    # Codex treats models.new_thread.model as the managed default for a new local
    # thread. It outranks the regular user/project model setting; an explicit CLI
    # --model (including StarAgent's Node preference) still wins at launch.
    return new_thread_model or clean_model_id(config.get("model"))


def codex_configured_reasoning_effort(config: Mapping[str, object]) -> str:
    managed = config.get("models")
    new_thread = managed.get("new_thread") if isinstance(managed, dict) else None
    new_thread_effort = (
        clean_reasoning_effort(
            "codex",
            new_thread.get("model_reasoning_effort") or new_thread.get("modelReasoningEffort"),
        )
        if isinstance(new_thread, dict)
        else ""
    )
    return new_thread_effort or clean_reasoning_effort(
        "codex",
        config.get("model_reasoning_effort") or config.get("modelReasoningEffort"),
    )


def discover_claude_models(
    executable: str,
    environment: Mapping[str, str],
) -> dict[str, object]:
    config = read_config_object("claude", environment)
    effort_values = (
        claude_reasoning_effort_values(executable, environment)
        if executable
        else CLAUDE_FALLBACK_REASONING_EFFORTS
    )
    effort_supported = bool(effort_values)
    efforts = [reasoning_effort_entry("claude", value) for value in effort_values]
    models = [
        model_entry(
            model_id,
            agent="claude",
            label=label,
            provider=claude_provider(environment),
            description=description,
            default=model_id == "default",
            source="claude-alias",
            reasoning_efforts=efforts,
        )
        for model_id, label, description in CLAUDE_MODEL_ALIASES
    ]
    available = config.get("availableModels")
    if isinstance(available, list):
        for value in available:
            model_id = clean_model_id(value)
            if model_id and not any(item["id"] == model_id for item in models):
                models.append(
                    model_entry(
                        model_id,
                        agent="claude",
                        provider=claude_provider(environment),
                        source="configuration",
                        reasoning_efforts=efforts,
                    )
                )
    configured_model = ""
    configured_source = "harness_config"
    if clean_model_id(environment.get("ANTHROPIC_MODEL")):
        configured_model = clean_model_id(environment.get("ANTHROPIC_MODEL"))
        configured_source = "environment"
    elif clean_model_id(config.get("model")):
        configured_model = clean_model_id(config.get("model"))
    elif clean_model_id(environment.get("ANTHROPIC_DEFAULT_MODEL")):
        configured_model = clean_model_id(environment.get("ANTHROPIC_DEFAULT_MODEL"))
        configured_source = "environment_default"
    configured_effort = clean_reasoning_effort(
        "claude",
        environment.get("CLAUDE_CODE_EFFORT_LEVEL"),
    )
    configured_effort_source = "environment"
    if configured_effort == "auto":
        configured_effort = ""
    if not configured_effort:
        configured_effort = claude_configured_reasoning_effort(config, configured_model)
        configured_effort_source = "harness_config"
    return {
        "models": models,
        "configured_model": configured_model,
        "configured_source": configured_source,
        "configured_reasoning_effort": configured_effort,
        "configured_reasoning_effort_source": configured_effort_source,
        "reasoning_effort_supported": effort_supported,
        "reasoning_efforts": efforts,
        "default_model": "default",
        "provider": claude_provider(environment),
        "catalog_source": "claude-aliases",
        "refresh_supported": False,
        "error": "",
    }


def claude_reasoning_effort_values(
    executable: str,
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    try:
        argv = [executable, "--help"]
        command = (
            windows_process_argv(argv, environment, require_executable=True)
            if os.name == "nt"
            else argv
        )
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=MODEL_CATALOG_TIMEOUT_SECONDS,
            env=dict(environment),
            **background_process_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    output = strip_ansi(f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0 or "--effort" not in output:
        return ()
    match = re.search(r"--effort\b[^\n]*\(([^)]*)\)", output)
    if match:
        values = tuple(
            effort
            for value in match.group(1).split(",")
            if (effort := clean_reasoning_effort("claude", value)) and effort != "auto"
        )
        if values:
            return values
    return CLAUDE_FALLBACK_REASONING_EFFORTS


def claude_configured_reasoning_effort(
    config: Mapping[str, object],
    model: str,
) -> str:
    model_settings = config.get("modelSettings")
    if isinstance(model_settings, dict) and model:
        exact = model_settings.get(model)
        if isinstance(exact, dict):
            effort = clean_reasoning_effort("claude", exact.get("effortLevel"))
            if effort and effort != "auto":
                return effort
    effort = clean_reasoning_effort("claude", config.get("effortLevel"))
    return "" if effort == "auto" else effort


def claude_provider(environment: Mapping[str, str]) -> str:
    if truthy_environment(environment.get("CLAUDE_CODE_USE_BEDROCK")):
        return "Amazon Bedrock"
    if truthy_environment(environment.get("CLAUDE_CODE_USE_VERTEX")):
        return "Google Vertex AI"
    if truthy_environment(environment.get("CLAUDE_CODE_USE_FOUNDRY")):
        return "Microsoft Foundry"
    if environment.get("ANTHROPIC_BASE_URL", "").strip():
        return "Custom gateway"
    return "Anthropic"


def discover_opencode_models(
    executable: str,
    environment: Mapping[str, str],
    *,
    refresh: bool,
) -> dict[str, object]:
    config = read_config_object("opencode", environment)
    configured_model = clean_model_id(config.get("model"))
    models = configured_opencode_models(config)
    error = ""
    catalog_source = "configuration"
    if executable:
        argv = [executable, "models"]
        if refresh:
            argv.append("--refresh")
        try:
            command = (
                windows_process_argv(argv, environment, require_executable=True)
                if os.name == "nt"
                else argv
            )
            result = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=MODEL_CATALOG_TIMEOUT_SECONDS,
                env=dict(environment),
                **background_process_kwargs(),
            )
            if result.returncode == 0:
                models.extend(parse_opencode_models(result.stdout))
                catalog_source = "opencode-cli"
            else:
                detail = clean_model_error(result.stderr or result.stdout)
                error = detail or f"OpenCode model discovery exited with code {result.returncode}."
        except subprocess.TimeoutExpired:
            error = "OpenCode model discovery timed out."
        except OSError as exc:
            error = f"OpenCode model discovery is unavailable: {exc}"
    return {
        "models": normalize_model_entries(models, agent="opencode"),
        "configured_model": configured_model,
        "configured_source": "harness_config",
        "configured_reasoning_effort": clean_reasoning_effort(
            "opencode",
            config.get("variant"),
        ),
        "configured_reasoning_effort_source": "harness_config",
        "reasoning_effort_supported": bool(executable),
        "default_model": "",
        "provider": provider_from_model_id(configured_model),
        "catalog_source": catalog_source,
        "refresh_supported": bool(executable),
        "error": error,
    }


def parse_opencode_models(output: object) -> list[dict[str, object]]:
    models = []
    for line in strip_ansi(str(output or "")).splitlines()[:MAX_MODEL_CATALOG_ENTRIES]:
        model_id = clean_model_id(line.strip())
        if not model_id or "/" not in model_id:
            continue
        models.append(
            model_entry(
                model_id,
                agent="opencode",
                provider=provider_from_model_id(model_id),
                source="opencode-cli",
            )
        )
    return models


def configured_opencode_models(config: Mapping[str, object]) -> list[dict[str, object]]:
    providers = config.get("provider")
    if not isinstance(providers, dict):
        return []
    models = []
    for raw_provider, raw_config in list(providers.items())[:MAX_MODEL_CATALOG_ENTRIES]:
        provider = clean_model_id(raw_provider)
        configured = raw_config.get("models") if isinstance(raw_config, dict) else None
        if not provider or not isinstance(configured, dict):
            continue
        for raw_model, details in configured.items():
            model = clean_model_id(raw_model)
            if not model:
                continue
            label = details.get("name") if isinstance(details, dict) else ""
            variants = details.get("variants") if isinstance(details, dict) else None
            models.append(
                model_entry(
                    f"{provider}/{model}",
                    agent="opencode",
                    label=label or model,
                    provider=provider,
                    source="configuration",
                    reasoning_efforts=(
                        [reasoning_effort_entry("opencode", variant) for variant in variants]
                        if isinstance(variants, dict)
                        else []
                    ),
                )
            )
            if len(models) >= MAX_MODEL_CATALOG_ENTRIES:
                return models
    return models


def read_config_object(agent: str, environment: Mapping[str, str]) -> dict[str, object]:
    if agent == "opencode":
        content_override = environment.get("OPENCODE_CONFIG_CONTENT", "").strip()
        if content_override:
            return parse_config_object(content_override, "jsonc")
    try:
        path, _source, config_format = harness_config_path(agent)
        if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            return {}
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return parse_config_object(content, config_format)


def parse_config_object(content: str, config_format: str) -> dict[str, object]:
    try:
        if config_format == "toml":
            payload = tomllib.loads(content) if content.strip() else {}
        else:
            normalized = strip_jsonc(content) if config_format == "jsonc" else content
            payload = json.loads(normalized) if normalized.strip() else {}
    except (tomllib.TOMLDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def strip_jsonc(content: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(content):
        char = content[index]
        following = content[index + 1] if index + 1 < len(content) else ""
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            result.extend("  ")
            index += 2
            while index < len(content) and content[index] not in "\r\n":
                result.append(" ")
                index += 1
            continue
        if char == "/" and following == "*":
            result.extend("  ")
            index += 2
            while index < len(content):
                if content[index : index + 2] == "*/":
                    result.extend("  ")
                    index += 2
                    break
                result.append("\n" if content[index] == "\n" else " ")
                index += 1
            continue
        result.append(char)
        index += 1
    return strip_json_trailing_commas("".join(result))


def strip_json_trailing_commas(content: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(content):
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            result.append(char)
            continue
        if char == ",":
            following = index + 1
            while following < len(content) and content[following].isspace():
                following += 1
            if following < len(content) and content[following] in "}]":
                continue
        result.append(char)
    return "".join(result)


def normalize_model_entries(
    value: object,
    *,
    agent: str = "codex",
) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    models: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for raw in value[:MAX_MODEL_CATALOG_ENTRIES]:
        if not isinstance(raw, dict):
            continue
        model_id = clean_model_id(raw.get("id"))
        if not model_id:
            continue
        item = model_entry(
            model_id,
            agent=agent,
            label=raw.get("label") or model_id,
            provider=raw.get("provider"),
            description=raw.get("description"),
            default=bool(raw.get("default")),
            source=raw.get("source"),
            reasoning_efforts=raw.get("reasoning_efforts"),
            default_reasoning_effort=raw.get("default_reasoning_effort"),
        )
        if model_id in models:
            existing = models[model_id]
            existing["default"] = bool(existing.get("default") or item.get("default"))
            if not existing.get("description") and item.get("description"):
                existing["description"] = item["description"]
            if not existing.get("reasoning_efforts") and item.get("reasoning_efforts"):
                existing["reasoning_efforts"] = item["reasoning_efforts"]
            if not existing.get("default_reasoning_effort") and item.get(
                "default_reasoning_effort"
            ):
                existing["default_reasoning_effort"] = item["default_reasoning_effort"]
            continue
        models[model_id] = item
        order.append(model_id)
    return [models[model_id] for model_id in order]


def model_entry(
    model_id: object,
    *,
    agent: str = "codex",
    label: object = "",
    provider: object = "",
    description: object = "",
    default: bool = False,
    source: object = "",
    reasoning_efforts: object = None,
    default_reasoning_effort: object = "",
) -> dict[str, object]:
    normalized_id = validate_model_id(str(model_id))
    normalized_efforts = normalize_reasoning_effort_entries(
        agent,
        reasoning_efforts,
    )
    normalized_default_effort = clean_reasoning_effort(
        agent,
        default_reasoning_effort,
    )
    return {
        "id": normalized_id,
        "label": clean_model_text(label or normalized_id, max_chars=MAX_MODEL_LABEL_CHARS),
        "provider": clean_model_text(provider, max_chars=80),
        "description": clean_model_text(description, max_chars=MAX_MODEL_DESCRIPTION_CHARS),
        "default": bool(default),
        "source": clean_model_text(source, max_chars=80),
        "reasoning_efforts": normalized_efforts,
        "default_reasoning_effort": normalized_default_effort,
    }


def reasoning_effort_entry(
    agent: str,
    effort: object,
    *,
    description: object = "",
) -> dict[str, str]:
    return {
        "id": validate_reasoning_effort(agent, effort),
        "description": clean_model_text(
            description,
            max_chars=MAX_MODEL_DESCRIPTION_CHARS,
        ),
    }


def normalize_reasoning_effort_entries(
    agent: str,
    value: object,
) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    entries: dict[str, dict[str, str]] = {}
    for raw in value[:40]:
        if isinstance(raw, dict):
            effort = clean_reasoning_effort(
                agent,
                raw.get("id") or raw.get("reasoningEffort"),
            )
            description = raw.get("description")
        else:
            effort = clean_reasoning_effort(agent, raw)
            description = ""
        if not effort:
            continue
        if effort not in entries:
            entries[effort] = reasoning_effort_entry(
                agent,
                effort,
                description=description,
            )
        elif description and not entries[effort]["description"]:
            entries[effort]["description"] = clean_model_text(
                description,
                max_chars=MAX_MODEL_DESCRIPTION_CHARS,
            )
    return list(entries.values())


def reasoning_efforts_for_model(
    agent: str,
    model: str,
    models: list[dict[str, object]],
    discovery: Mapping[str, object],
) -> list[dict[str, str]]:
    for entry in models:
        if entry.get("id") == model and entry.get("reasoning_efforts"):
            return normalize_reasoning_effort_entries(
                agent,
                entry.get("reasoning_efforts"),
            )
    discovered = normalize_reasoning_effort_entries(
        agent,
        discovery.get("reasoning_efforts"),
    )
    if discovered:
        return discovered
    return fallback_reasoning_efforts(agent, model, discovery)


def fallback_reasoning_efforts(
    agent: str,
    model: str,
    discovery: Mapping[str, object],
) -> list[dict[str, str]]:
    if agent == "codex":
        return [
            reasoning_effort_entry(agent, effort) for effort in CODEX_FALLBACK_REASONING_EFFORTS
        ]
    if agent == "claude" and discovery.get("reasoning_effort_supported"):
        return [
            reasoning_effort_entry(agent, effort) for effort in CLAUDE_FALLBACK_REASONING_EFFORTS
        ]
    if agent == "opencode":
        provider = provider_from_model_id(model)
        return [
            reasoning_effort_entry(agent, effort)
            for effort in OPENCODE_PROVIDER_VARIANTS.get(provider, ())
        ]
    return []


def model_default_reasoning_effort(
    agent: str,
    models: list[dict[str, object]],
    model: str,
) -> str:
    return next(
        (
            clean_reasoning_effort(agent, entry.get("default_reasoning_effort"))
            for entry in models
            if entry.get("id") == model
        ),
        "",
    )


def clean_model_id(value: object) -> str:
    try:
        return validate_model_id(value, allow_empty=True)
    except ValueError:
        return ""


def clean_reasoning_effort(agent: str, value: object) -> str:
    try:
        return validate_reasoning_effort(agent, value, allow_empty=True)
    except ValueError:
        return ""


def model_provider_for_id(
    agent: str,
    model: str,
    discovery: Mapping[str, object],
) -> str:
    if agent == "opencode":
        return provider_from_model_id(model)
    return clean_model_text(discovery.get("provider"), max_chars=80)


def provider_from_model_id(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def truthy_environment(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def clean_model_text(value: object, *, max_chars: int = 500) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return f"{text[:max_chars]}…" if len(text) > max_chars else text


def clean_model_error(value: object) -> str:
    return redact_log_text(value, max_chars=500)


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
