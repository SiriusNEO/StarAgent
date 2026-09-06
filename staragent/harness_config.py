from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from staragent.paths import state_dir
from staragent.state import atomic_write_json, atomic_write_text, locked_file, read_json

MAX_CONFIG_BYTES = 256 * 1024
MAX_ENVIRONMENT_VARIABLES = 64
MAX_ENVIRONMENT_NAME_CHARS = 80
MAX_ENVIRONMENT_VALUE_BYTES = 16 * 1024
MAX_ENVIRONMENT_TOTAL_BYTES = 64 * 1024
ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SECRET_NAME_PATTERN = re.compile(
    r"(?:API[_-]?KEY|(?:^|[_-])KEY$|TOKEN|SECRET|PASSWORD|PASSCODE|CREDENTIAL|AUTH)",
    flags=re.IGNORECASE,
)
ENVIRONMENT_REFERENCE_PATTERN = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
RESERVED_ENVIRONMENT_NAMES = {
    "STARAGENT_HARNESS",
    "STARAGENT_HARNESS_COMMAND",
    "TMUX",
    "TMUX_PANE",
}

HARNESS_ENVIRONMENT_HINTS = {
    "codex": (
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "DEEPSEEK_API_KEY",
    ),
    "claude": (
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ),
    "opencode": (
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ),
}


@dataclass(frozen=True)
class HarnessConfigSpec:
    name: str
    label: str
    format: str
    docs_url: str


HARNESS_CONFIG_SPECS = {
    "codex": HarnessConfigSpec(
        name="codex",
        label="Codex",
        format="toml",
        docs_url="https://learn.chatgpt.com/docs/config-file/config-basic",
    ),
    "claude": HarnessConfigSpec(
        name="claude",
        label="Claude Code",
        format="json",
        docs_url="https://code.claude.com/docs/en/settings",
    ),
    "opencode": HarnessConfigSpec(
        name="opencode",
        label="OpenCode",
        format="json",
        docs_url="https://opencode.ai/docs/config/",
    ),
}


def harness_config_spec(agent: str) -> HarnessConfigSpec:
    normalized = str(agent or "").strip().lower()
    try:
        return HARNESS_CONFIG_SPECS[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported Agent CLI: {agent}") from exc


def harness_environment_path() -> Path:
    return state_dir() / "harness-environment.json"


def managed_harness_environment(agent: str) -> dict[str, str]:
    spec = harness_config_spec(agent)
    path = harness_environment_path()
    with locked_file(path):
        payload = read_json(path, {})
    harnesses = payload.get("harnesses") if isinstance(payload, dict) else None
    raw = harnesses.get(spec.name) if isinstance(harnesses, dict) else None
    try:
        return validate_harness_environment(raw if isinstance(raw, dict) else {})
    except ValueError:
        # A hand-edited state file must never inject malformed process environment.
        return {}


def harness_process_environment(
    agent: str,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    environment.update(managed_harness_environment(agent))
    return environment


def save_harness_environment(agent: str, variables: object) -> dict[str, object]:
    spec = harness_config_spec(agent)
    normalized = validate_harness_environment(variables)
    path = harness_environment_path()
    with locked_file(path):
        current = read_json(path, {})
        harnesses = current.get("harnesses") if isinstance(current, dict) else None
        updated = dict(harnesses) if isinstance(harnesses, dict) else {}
        if normalized:
            updated[spec.name] = normalized
        else:
            updated.pop(spec.name, None)
        atomic_write_json(path, {"version": 1, "harnesses": updated}, mode=0o600)
    return harness_configuration_payload(spec.name)


def validate_harness_environment(variables: object) -> dict[str, str]:
    if not isinstance(variables, dict):
        raise ValueError("Environment variables must be an object of name/value pairs.")
    if len(variables) > MAX_ENVIRONMENT_VARIABLES:
        raise ValueError(
            f"At most {MAX_ENVIRONMENT_VARIABLES} environment variables can be configured."
        )
    normalized: dict[str, str] = {}
    total_bytes = 0
    for raw_name, raw_value in variables.items():
        name = str(raw_name or "").strip()
        if len(name) > MAX_ENVIRONMENT_NAME_CHARS or not ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid environment variable name: {name or '(empty)'}")
        if name in RESERVED_ENVIRONMENT_NAMES or name.startswith("STARAGENT_"):
            raise ValueError(f"Environment variable is reserved by StarAgent: {name}")
        if not isinstance(raw_value, str):
            raise ValueError(f"Environment variable values must be strings: {name}")
        if "\x00" in raw_value:
            raise ValueError(f"Environment variable contains a NUL byte: {name}")
        value_bytes = len(raw_value.encode("utf-8"))
        if value_bytes > MAX_ENVIRONMENT_VALUE_BYTES:
            raise ValueError(
                f"Environment variable {name} exceeds {MAX_ENVIRONMENT_VALUE_BYTES} bytes."
            )
        total_bytes += len(name.encode("utf-8")) + value_bytes
        if total_bytes > MAX_ENVIRONMENT_TOTAL_BYTES:
            raise ValueError(
                f"Harness environment exceeds {MAX_ENVIRONMENT_TOTAL_BYTES} bytes in total."
            )
        normalized[name] = raw_value
    return dict(sorted(normalized.items()))


def inherited_harness_environment(
    agent: str,
    *,
    managed: dict[str, str],
    config_content: str,
    config_format: str,
) -> list[dict[str, object]]:
    """Describe relevant service variables without returning inherited values."""
    spec = harness_config_spec(agent)
    names = set(HARNESS_ENVIRONMENT_HINTS[spec.name])
    names.update(managed)
    names.update(config_environment_references(config_content, config_format))
    inherited = []
    for name in sorted(names):
        if name not in os.environ:
            continue
        inherited.append(
            {
                "name": name,
                "secret": bool(SECRET_NAME_PATTERN.search(name)),
                "configured": bool(os.environ.get(name)),
                "overridden": name in managed,
            }
        )
    return inherited


def config_environment_references(content: str, config_format: str) -> set[str]:
    names = set(ENVIRONMENT_REFERENCE_PATTERN.findall(content or ""))
    try:
        if config_format == "toml":
            parsed: object = tomllib.loads(content) if content.strip() else {}
        elif config_format == "json":
            parsed = json.loads(content) if content.strip() else {}
        else:
            parsed = {}
    except (tomllib.TOMLDecodeError, json.JSONDecodeError):
        parsed = {}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in {"env_key", "envKey"} and isinstance(nested, str):
                    name = nested.strip()
                    if ENVIRONMENT_NAME_PATTERN.fullmatch(name):
                        names.add(name)
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(parsed)
    return names


def normalize_inherited_environment(
    value: object,
    managed: dict[str, str],
) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    inherited: dict[str, dict[str, object]] = {}
    for item in value[:MAX_ENVIRONMENT_VARIABLES]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            continue
        inherited[name] = {
            "name": name,
            "secret": bool(SECRET_NAME_PATTERN.search(name)),
            "configured": bool(item.get("configured", True)),
            "overridden": name in managed,
        }
    return [inherited[name] for name in sorted(inherited)]


def harness_config_path(agent: str) -> tuple[Path, str, str]:
    spec = harness_config_spec(agent)
    environment = harness_process_environment(spec.name)
    home_value = environment.get("HOME", "").strip()
    home = Path(home_value).expanduser() if home_value else Path.home()
    if spec.name == "codex":
        configured = environment.get("CODEX_HOME", "").strip()
        directory = Path(configured).expanduser() if configured else home / ".codex"
        return directory / "config.toml", "CODEX_HOME" if configured else "default", "toml"
    if spec.name == "claude":
        configured = environment.get("CLAUDE_CONFIG_DIR", "").strip()
        directory = Path(configured).expanduser() if configured else home / ".claude"
        return directory / "settings.json", "CLAUDE_CONFIG_DIR" if configured else "default", "json"

    configured = environment.get("OPENCODE_CONFIG", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        suffix = path.suffix.lower()
        return path, "OPENCODE_CONFIG", "jsonc" if suffix == ".jsonc" else "json"
    xdg_home = environment.get("XDG_CONFIG_HOME", "").strip()
    directory = (Path(xdg_home).expanduser() if xdg_home else home / ".config") / "opencode"
    json_path = directory / "opencode.json"
    jsonc_path = directory / "opencode.jsonc"
    if json_path.exists() or not jsonc_path.exists():
        return json_path, "XDG_CONFIG_HOME" if xdg_home else "default", "json"
    return jsonc_path, "XDG_CONFIG_HOME" if xdg_home else "default", "jsonc"


def harness_configuration_payload(agent: str) -> dict[str, object]:
    spec = harness_config_spec(agent)
    path, source, config_format = harness_config_path(spec.name)
    config = read_harness_config(path, config_format)
    variables = managed_harness_environment(spec.name)
    content = config.get("content") if isinstance(config.get("content"), str) else ""
    return {
        "supported": True,
        "agent": spec.name,
        "config": {
            **config,
            "path": str(path),
            "source": source,
            "format": config_format,
            "docs_url": spec.docs_url,
            "max_bytes": MAX_CONFIG_BYTES,
        },
        "environment": {
            "variables": [
                {
                    "name": name,
                    "value": value,
                    "secret": bool(SECRET_NAME_PATTERN.search(name)),
                }
                for name, value in variables.items()
            ],
            "inherited": inherited_harness_environment(
                spec.name,
                managed=variables,
                config_content=content,
                config_format=config_format,
            ),
            "path": str(harness_environment_path()),
            "max_variables": MAX_ENVIRONMENT_VARIABLES,
            "max_value_bytes": MAX_ENVIRONMENT_VALUE_BYTES,
            "max_total_bytes": MAX_ENVIRONMENT_TOTAL_BYTES,
        },
        "checked_at": utc_timestamp(),
        "error": "",
    }


def unavailable_harness_configuration(agent: str, error: object) -> dict[str, object]:
    spec = harness_config_spec(agent)
    return {
        "supported": False,
        "agent": spec.name,
        "config": {
            "path": "",
            "source": "",
            "format": spec.format,
            "docs_url": spec.docs_url,
            "exists": False,
            "content": "",
            "size": 0,
            "modified_at": "",
            "editable": False,
            "error": clean_public_text(error),
            "max_bytes": MAX_CONFIG_BYTES,
        },
        "environment": {
            "variables": [],
            "inherited": [],
            "path": "",
            "max_variables": MAX_ENVIRONMENT_VARIABLES,
            "max_value_bytes": MAX_ENVIRONMENT_VALUE_BYTES,
            "max_total_bytes": MAX_ENVIRONMENT_TOTAL_BYTES,
        },
        "checked_at": utc_timestamp(),
        "error": clean_public_text(error),
    }


def normalize_harness_configuration(agent: str, payload: object) -> dict[str, object]:
    spec = harness_config_spec(agent)
    if not isinstance(payload, dict):
        return unavailable_harness_configuration(spec.name, "Node returned an invalid response.")
    raw_config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    raw_environment = (
        payload.get("environment") if isinstance(payload.get("environment"), dict) else {}
    )
    raw_variables = raw_environment.get("variables")
    variables: dict[str, str] = {}
    if isinstance(raw_variables, list):
        for item in raw_variables[:MAX_ENVIRONMENT_VARIABLES]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            value = item.get("value")
            if not isinstance(value, str):
                continue
            try:
                variables.update(validate_harness_environment({name: value}))
            except ValueError:
                continue
    inherited = normalize_inherited_environment(raw_environment.get("inherited"), variables)
    content = raw_config.get("content")
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CONFIG_BYTES:
        content = ""
    config_format = str(raw_config.get("format") or spec.format).lower()
    if config_format not in {"toml", "json", "jsonc"}:
        config_format = spec.format
    normalized = {
        "supported": bool(payload.get("supported", True)),
        "agent": spec.name,
        "config": {
            "path": clean_public_text(raw_config.get("path"), max_chars=1000),
            "source": clean_public_text(raw_config.get("source"), max_chars=80),
            "format": config_format,
            "docs_url": spec.docs_url,
            "exists": bool(raw_config.get("exists")),
            "content": content,
            "size": max(0, safe_int(raw_config.get("size"))),
            "modified_at": clean_public_text(raw_config.get("modified_at"), max_chars=80),
            "editable": bool(raw_config.get("editable", True)),
            "error": clean_public_text(raw_config.get("error")),
            "max_bytes": MAX_CONFIG_BYTES,
        },
        "environment": {
            "variables": [
                {
                    "name": name,
                    "value": value,
                    "secret": bool(SECRET_NAME_PATTERN.search(name)),
                }
                for name, value in sorted(variables.items())
            ],
            "inherited": inherited,
            "path": clean_public_text(raw_environment.get("path"), max_chars=1000),
            "max_variables": MAX_ENVIRONMENT_VARIABLES,
            "max_value_bytes": MAX_ENVIRONMENT_VALUE_BYTES,
            "max_total_bytes": MAX_ENVIRONMENT_TOTAL_BYTES,
        },
        "checked_at": clean_public_text(payload.get("checked_at"), max_chars=80),
        "error": clean_public_text(payload.get("error")),
    }
    if not normalized["supported"]:
        normalized["config"]["editable"] = False
    return normalized


def read_harness_config(path: Path, config_format: str) -> dict[str, object]:
    if not path.exists():
        return {
            "exists": False,
            "content": default_config_content(config_format),
            "size": 0,
            "modified_at": "",
            "editable": True,
            "error": "",
        }
    try:
        size = path.stat().st_size
    except OSError as exc:
        return config_file_error(exc)
    if size > MAX_CONFIG_BYTES:
        return config_file_error(
            f"Configuration file is too large to edit safely ({size} bytes).",
            exists=True,
            size=size,
        )
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return config_file_error(exc, exists=True, size=size)
    return {
        "exists": True,
        "content": content,
        "size": size,
        "modified_at": file_modified_at(path),
        "editable": True,
        "error": "",
    }


def save_harness_config(agent: str, content: object) -> dict[str, object]:
    spec = harness_config_spec(agent)
    if not isinstance(content, str):
        raise ValueError("Configuration content must be text.")
    size = len(content.encode("utf-8"))
    if size > MAX_CONFIG_BYTES:
        raise ValueError(f"Configuration file exceeds {MAX_CONFIG_BYTES} bytes.")
    if "\x00" in content:
        raise ValueError("Configuration file cannot contain NUL bytes.")
    path, _source, config_format = harness_config_path(spec.name)
    validate_config_syntax(content, config_format)
    destination = path.resolve(strict=False) if path.is_symlink() else path
    try:
        atomic_write_text(destination, content, mode=0o600)
    except OSError as exc:
        raise RuntimeError(f"Could not save {spec.label} configuration: {exc}") from exc
    return harness_configuration_payload(spec.name)


def validate_config_syntax(content: str, config_format: str) -> None:
    if not content.strip():
        return
    if config_format == "toml":
        try:
            tomllib.loads(content)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid TOML: {exc}") from exc
        return
    if config_format == "json":
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("Harness configuration must be a JSON object.")


def default_config_content(config_format: str) -> str:
    if config_format == "toml":
        return ""
    return "{}\n"


def config_file_error(
    error: object,
    *,
    exists: bool = False,
    size: int = 0,
) -> dict[str, object]:
    return {
        "exists": exists,
        "content": "",
        "size": max(0, size),
        "modified_at": "",
        "editable": False,
        "error": clean_public_text(error),
    }


def file_modified_at(path: Path) -> str:
    try:
        return (
            datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OSError, OverflowError, ValueError):
        return ""


def clean_public_text(value: Any, *, max_chars: int = 500) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return f"{text[:max_chars]}…" if len(text) > max_chars else text


def safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
