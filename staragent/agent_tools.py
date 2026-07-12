from __future__ import annotations

import copy
import os
import shlex
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from staragent.agent_auth import (
    normalize_agent_auth,
    probe_agent_auth,
    unavailable_agent_auth,
    unknown_agent_auth,
)
from staragent.agent_usage import (
    normalize_agent_usage,
    probe_agent_usage,
    unavailable_agent_usage,
    unknown_agent_usage,
)
from staragent.event_log import redact_log_text
from staragent.text import strip_ansi

AGENT_TOOL_CACHE_TTL_SECONDS = 60.0
AGENT_TOOL_PROBE_TIMEOUT_SECONDS = 3.0
AGENT_TOOL_UPDATE_TIMEOUT_SECONDS = 180.0
AGENT_TOOL_UPDATE_OUTPUT_MAX_CHARS = 4_000
AGENT_TOOL_STATUSES = {"available", "missing", "error", "unknown"}


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    label: str
    command: str
    npm_package: str
    install_command: str
    docs_url: str
    history_supported: bool = False
    version_args: tuple[str, ...] = ("--version",)


AGENT_TOOL_SPECS = (
    AgentToolSpec(
        "codex",
        "Codex",
        "codex",
        "@openai/codex",
        "npm install -g @openai/codex@latest",
        "https://github.com/openai/codex",
        history_supported=True,
    ),
    AgentToolSpec(
        "claude",
        "Claude Code",
        "claude",
        "@anthropic-ai/claude-code",
        "npm install -g @anthropic-ai/claude-code@latest",
        "https://docs.anthropic.com/en/docs/claude-code/getting-started",
        history_supported=True,
    ),
    AgentToolSpec(
        "opencode",
        "OpenCode",
        "opencode",
        "opencode-ai",
        "npm install -g opencode-ai@latest",
        "https://opencode.ai/docs",
    ),
)

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
_UPDATE_LOCKS = {spec.name: threading.Lock() for spec in AGENT_TOOL_SPECS}


class AgentToolUpdateBusyError(RuntimeError):
    pass


def agent_tools_payload(*, force: bool = False) -> dict[str, object]:
    cache_key = os.environ.get("PATH", "")
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and not force and now - cached[0] < AGENT_TOOL_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])
        with ThreadPoolExecutor(max_workers=len(AGENT_TOOL_SPECS)) as executor:
            tools = list(executor.map(probe_agent_tool, AGENT_TOOL_SPECS))
        payload: dict[str, object] = {
            "supported": True,
            "updates_supported": True,
            "scope": "executable",
            "checked_at": utc_timestamp(),
            "cache_ttl_seconds": int(AGENT_TOOL_CACHE_TTL_SECONDS),
            "tools": tools,
            "error": "",
        }
        _CACHE[cache_key] = (time.monotonic(), payload)
        return copy.deepcopy(payload)


def probe_agent_tool(spec: AgentToolSpec) -> dict[str, object]:
    executable = shutil.which(spec.command)
    if not executable:
        return tool_status(
            spec,
            status="missing",
            error=f"{spec.command} was not found in the Node service PATH.",
        )
    resolved_executable = os.path.realpath(executable)
    try:
        result = subprocess.run(
            [executable, *spec.version_args],
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=AGENT_TOOL_PROBE_TIMEOUT_SECONDS,
            env=probe_environment(),
        )
    except subprocess.TimeoutExpired:
        return tool_status(
            spec,
            status="error",
            executable=executable,
            resolved_executable=resolved_executable,
            error=f"Version check timed out after {AGENT_TOOL_PROBE_TIMEOUT_SECONDS:g}s.",
        )
    except OSError as exc:
        return tool_status(
            spec,
            status="error",
            executable=executable,
            resolved_executable=resolved_executable,
            error=clean_tool_text(exc),
        )
    output = first_output_line(result.stdout, result.stderr)
    if result.returncode != 0:
        detail = output or f"Version check exited with code {result.returncode}."
        return tool_status(
            spec,
            status="error",
            executable=executable,
            resolved_executable=resolved_executable,
            error=detail,
        )
    with ThreadPoolExecutor(max_workers=2) as executor:
        auth_future = executor.submit(probe_agent_auth, spec.name, executable)
        usage_future = executor.submit(probe_agent_usage, spec.name, executable)
        auth = auth_future.result()
        usage = usage_future.result()
    return tool_status(
        spec,
        status="available",
        executable=executable,
        resolved_executable=resolved_executable,
        version=output or "installed",
        auth=auth,
        usage=usage,
    )


def tool_status(
    spec: AgentToolSpec,
    *,
    status: str,
    executable: str = "",
    resolved_executable: str = "",
    install_method: str = "",
    version: str = "",
    error: str = "",
    auth: object = None,
    usage: object = None,
) -> dict[str, object]:
    normalized_status = status if status in AGENT_TOOL_STATUSES else "unknown"
    resolved = resolved_executable or executable
    method = normalize_install_method(
        install_method or detect_install_method(spec, executable, resolved)
    )
    if normalized_status == "unknown":
        method = "unknown"
    if auth is not None:
        normalized_auth = normalize_agent_auth(spec.name, auth)
    elif normalized_status in {"missing", "error"}:
        normalized_auth = unavailable_agent_auth(
            spec.name,
            "Login state is unavailable until this CLI installation is ready.",
        )
    else:
        normalized_auth = unknown_agent_auth(spec.name)
    if usage is not None:
        normalized_usage = normalize_agent_usage(spec.name, usage)
    elif normalized_status in {"missing", "error"}:
        normalized_usage = unavailable_agent_usage(
            spec.name,
            "Usage is unavailable until this CLI installation is ready.",
        )
    else:
        normalized_usage = unknown_agent_usage(spec.name)
    return {
        "name": spec.name,
        "label": spec.label,
        "command": spec.command,
        "status": normalized_status,
        "available": normalized_status == "available",
        "installed": normalized_status in {"available", "error"},
        "version": clean_tool_text(version),
        "executable": clean_tool_text(executable, max_chars=500),
        "resolved_executable": clean_tool_text(resolved, max_chars=500),
        "install_method": method,
        "update_command": update_command(spec, normalized_status, method),
        "update_action": ""
        if normalized_status == "unknown"
        else ("install" if normalized_status == "missing" else "update"),
        "update_note": update_note(normalized_status, method),
        "docs_url": spec.docs_url,
        "history_supported": spec.history_supported,
        "auth": normalized_auth,
        "usage": normalized_usage,
        "error": clean_tool_text(error, max_chars=500),
    }


def unknown_agent_tools_payload(
    error: str,
    *,
    supported: bool = False,
    stale: bool = False,
) -> dict[str, object]:
    return {
        "supported": supported,
        "updates_supported": False,
        "scope": "executable",
        "checked_at": "",
        "cache_ttl_seconds": int(AGENT_TOOL_CACHE_TTL_SECONDS),
        "tools": [tool_status(spec, status="unknown", error=error) for spec in AGENT_TOOL_SPECS],
        "error": clean_tool_text(error, max_chars=500),
        "stale": stale,
    }


def normalize_agent_tools_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return unknown_agent_tools_payload("Node returned an invalid agent tool payload.")
    raw_tools = payload.get("tools")
    by_name = (
        {str(item.get("name") or ""): item for item in raw_tools if isinstance(item, dict)}
        if isinstance(raw_tools, list)
        else {}
    )
    tools = []
    for spec in AGENT_TOOL_SPECS:
        raw = by_name.get(spec.name)
        if not raw:
            tools.append(tool_status(spec, status="unknown", error="No result reported."))
            continue
        status = str(raw.get("status") or "unknown").lower()
        tools.append(
            tool_status(
                spec,
                status=status,
                executable=str(raw.get("executable") or ""),
                resolved_executable=str(raw.get("resolved_executable") or ""),
                install_method=str(raw.get("install_method") or ""),
                version=str(raw.get("version") or ""),
                error=str(raw.get("error") or ""),
                auth=raw.get("auth"),
                usage=raw.get("usage"),
            )
        )
    return {
        "supported": bool(payload.get("supported", True)),
        "updates_supported": bool(payload.get("updates_supported", False)),
        "scope": "executable",
        "checked_at": clean_tool_text(payload.get("checked_at"), max_chars=80),
        "cache_ttl_seconds": int(AGENT_TOOL_CACHE_TTL_SECONDS),
        "tools": tools,
        "error": clean_tool_text(payload.get("error"), max_chars=500),
        "stale": bool(payload.get("stale", False)),
    }


def payload_with_node(
    payload: object,
    node_name: str,
    *,
    stale: bool = False,
    error: str = "",
) -> dict[str, object]:
    normalized = normalize_agent_tools_payload(payload)
    normalized["node"] = node_name
    normalized["stale"] = stale or bool(normalized.get("stale"))
    if error:
        normalized["error"] = clean_tool_text(error, max_chars=500)
    return normalized


def clear_agent_tools_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def update_agent_tool(name: str) -> dict[str, object]:
    spec = agent_tool_spec(name)
    if spec is None:
        raise ValueError(f"Unsupported Agent CLI: {name}")
    lock = _UPDATE_LOCKS[spec.name]
    if not lock.acquire(blocking=False):
        raise AgentToolUpdateBusyError(f"{spec.label} is already being updated.")
    try:
        before = probe_agent_tool(spec)
        command = str(before.get("update_command") or "")
        base_result: dict[str, object] = {
            "ok": False,
            "agent": spec.name,
            "label": spec.label,
            "command": command,
            "before_version": str(before.get("version") or ""),
            "after_version": str(before.get("version") or ""),
            "changed": False,
            "output": "",
            "error": "",
            "checked_at": utc_timestamp(),
        }
        if before.get("status") != "available":
            base_result["error"] = (
                f"{spec.label} is not ready in the Node service environment; "
                "install or repair it before updating."
            )
            return normalize_agent_update_result(spec.name, base_result)
        if not command or before.get("update_action") != "update":
            base_result["error"] = f"No supported update command is available for {spec.label}."
            return normalize_agent_update_result(spec.name, base_result)

        argv = update_argv(spec, before, command)
        returncode, output = run_agent_update_command(argv)
        base_result["output"] = output
        if returncode != 0:
            base_result["error"] = output or f"Update command exited with code {returncode}."
            return normalize_agent_update_result(spec.name, base_result)

        clear_agent_tools_cache()
        after = probe_agent_tool(spec)
        before_version = str(before.get("version") or "")
        after_version = str(after.get("version") or "")
        base_result.update(
            {
                "ok": after.get("status") == "available",
                "after_version": after_version,
                "changed": bool(after_version and after_version != before_version),
                "error": ""
                if after.get("status") == "available"
                else "The update command completed, but the CLI version check failed.",
                "checked_at": utc_timestamp(),
            }
        )
        return normalize_agent_update_result(spec.name, base_result)
    finally:
        lock.release()


def update_argv(spec: AgentToolSpec, tool: dict[str, object], command: str) -> list[str]:
    expected = update_command(spec, "available", str(tool.get("install_method") or "unknown"))
    if not expected or command != expected:
        raise ValueError(f"Unsupported update command for {spec.label}.")
    argv = shlex.split(expected)
    if argv and argv[0] == spec.command and tool.get("executable"):
        argv[0] = str(tool["executable"])
    return argv


def run_agent_update_command(argv: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=AGENT_TOOL_UPDATE_TIMEOUT_SECONDS,
            env=update_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        output = clean_update_output(exc.stdout, exc.stderr)
        detail = f"Update timed out after {AGENT_TOOL_UPDATE_TIMEOUT_SECONDS:g}s."
        return 124, f"{output}\n{detail}".strip()
    except OSError as exc:
        return 127, clean_update_output(exc)
    return result.returncode, clean_update_output(result.stdout, result.stderr)


def clean_update_output(*values: object) -> str:
    parts = []
    for value in values:
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        else:
            text = str(value or "")
        text = strip_ansi(text).strip()
        if text:
            parts.append(text)
    return redact_log_text("\n".join(parts), max_chars=AGENT_TOOL_UPDATE_OUTPUT_MAX_CHARS)


def normalize_agent_update_result(name: str, value: object) -> dict[str, object]:
    spec = agent_tool_spec(name)
    if spec is None:
        raise ValueError(f"Unsupported Agent CLI: {name}")
    payload = value if isinstance(value, dict) else {}
    command = clean_tool_text(payload.get("command"), max_chars=200)
    allowed_commands = {
        update_command(spec, "available", method)
        for method in ("npm", "homebrew", "standalone", "native", "install-script", "unknown")
    }
    allowed_commands.discard("")
    if command not in allowed_commands:
        command = ""
    return {
        "ok": bool(payload.get("ok")),
        "agent": spec.name,
        "label": spec.label,
        "command": command,
        "before_version": clean_tool_text(payload.get("before_version"), max_chars=120),
        "after_version": clean_tool_text(payload.get("after_version"), max_chars=120),
        "changed": bool(payload.get("changed")),
        "output": clean_update_output(payload.get("output")),
        "error": clean_update_output(payload.get("error")),
        "checked_at": clean_tool_text(payload.get("checked_at"), max_chars=80),
    }


def first_output_line(*values: str) -> str:
    for value in values:
        for line in strip_ansi(value or "").splitlines():
            if line.strip():
                return clean_tool_text(line)
    return ""


def clean_tool_text(value: Any, *, max_chars: int = 240) -> str:
    text = strip_ansi(str(value or "")).replace("\x00", "").strip()
    return f"{text[:max_chars]}…" if len(text) > max_chars else text


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def probe_environment() -> dict[str, str]:
    return {
        **os.environ,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def update_environment() -> dict[str, str]:
    return {
        **os.environ,
        "NO_COLOR": "1",
        "TERM": "dumb",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_update_notifier": "false",
    }


def agent_tool_spec(name: str) -> AgentToolSpec | None:
    normalized = str(name or "").strip().lower()
    return next((spec for spec in AGENT_TOOL_SPECS if spec.name == normalized), None)


def agent_catalog_payload() -> list[dict[str, object]]:
    return [
        {
            "name": spec.name,
            "label": spec.label,
            "command": spec.command,
            "install_command": spec.install_command,
            "docs_url": spec.docs_url,
            "history_supported": spec.history_supported,
        }
        for spec in AGENT_TOOL_SPECS
    ]


def detect_install_method(
    spec: AgentToolSpec,
    executable: str,
    resolved_executable: str,
) -> str:
    combined = f"{executable}\n{resolved_executable}".lower()
    package_path = spec.npm_package.lower().replace("@", "").replace("/", os.sep)
    normalized = combined.replace("@", "").replace("/", os.sep)
    if "node_modules" in normalized and package_path in normalized:
        return "npm"
    if any(fragment in combined for fragment in ("/cellar/", "/homebrew/", "/linuxbrew/")):
        return "homebrew"
    if spec.name == "codex" and ".codex/packages/standalone" in combined:
        return "standalone"
    if spec.name == "claude" and any(
        fragment in combined for fragment in ("/.claude/local/", "/.local/share/claude/")
    ):
        return "native"
    if spec.name == "opencode" and "/.opencode/bin/" in combined:
        return "install-script"
    return "unknown" if executable else "missing"


def normalize_install_method(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return (
        normalized
        if normalized in {"npm", "homebrew", "standalone", "native", "install-script", "missing"}
        else "unknown"
    )


def update_command(spec: AgentToolSpec, status: str, install_method: str) -> str:
    if status == "unknown":
        return ""
    if status == "missing":
        return spec.install_command
    if spec.name == "codex":
        if install_method == "npm":
            return "npm install -g @openai/codex@latest"
        if install_method == "homebrew":
            return "brew upgrade --cask codex"
        return "codex update"
    if spec.name == "claude":
        return "claude update"
    if spec.name == "opencode":
        return "opencode upgrade"
    return ""


def update_note(status: str, install_method: str) -> str:
    if status == "unknown":
        return "Availability has not been reported by this Node."
    if status == "missing":
        return "Install command; review permissions and package ownership before running."
    if install_method == "unknown":
        return "Installation source is unknown; verify the command before updating."
    return f"Detected {install_method} installation."
