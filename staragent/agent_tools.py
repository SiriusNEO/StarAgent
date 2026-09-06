from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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
from staragent.harness_config import harness_process_environment
from staragent.text import strip_ansi

AGENT_TOOL_CACHE_TTL_SECONDS = 60.0
AGENT_TOOL_PROBE_TIMEOUT_SECONDS = 3.0
AGENT_TOOL_UPDATE_TIMEOUT_SECONDS = 180.0
AGENT_TOOL_INSTALL_TIMEOUT_SECONDS = 300.0
AGENT_TOOL_UPDATE_OUTPUT_MAX_CHARS = 4_000
AGENT_TOOL_INSTALL_SCRIPT_MAX_BYTES = 1024 * 1024
AGENT_TOOL_STATUSES = {"available", "missing", "error", "unknown"}
AGENT_UPDATE_STATUSES = {"up_to_date", "update_available", "unknown"}
CODEX_UPDATE_CACHE_MAX_AGE_SECONDS = 48 * 60 * 60
CODEX_UPDATE_CACHE_MAX_BYTES = 16 * 1024
CLI_VERSION_PATTERN = re.compile(r"(?:^|[^A-Za-z0-9_.-])v?(\d+)\.(\d+)\.(\d+)(?![A-Za-z0-9_.-])")


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    label: str
    command: str
    npm_package: str
    install_command: str
    docs_url: str
    vendor: str
    description: str
    icon: str
    accent: str
    history_supported: bool = False
    version_args: tuple[str, ...] = ("--version",)


@dataclass(frozen=True)
class AgentInstallOption:
    id: str
    method: str
    source: str
    provider: str
    command: str
    argv: tuple[str, ...] = ()
    script_url: str = ""
    interpreter: str = ""
    recommended: bool = False
    china: bool = False
    source_url: str = ""

    @property
    def requirements(self) -> tuple[str, ...]:
        if self.argv:
            return (self.argv[0],)
        if self.interpreter:
            return (self.interpreter,)
        return ()


AGENT_TOOL_SPECS = (
    AgentToolSpec(
        name="codex",
        label="Codex",
        command="codex",
        npm_package="@openai/codex",
        install_command="npm install -g @openai/codex@latest",
        docs_url="https://github.com/openai/codex",
        vendor="OpenAI",
        description="A coding agent that works with you directly from the terminal.",
        icon="agent-icons/codex.svg",
        accent="#111111",
        history_supported=True,
    ),
    AgentToolSpec(
        name="claude",
        label="Claude Code",
        command="claude",
        npm_package="@anthropic-ai/claude-code",
        install_command="npm install -g @anthropic-ai/claude-code@latest",
        docs_url="https://code.claude.com/docs/en/setup",
        vendor="Anthropic",
        description="An agentic coding tool that understands your codebase and workflow.",
        icon="agent-icons/claude.svg",
        accent="#D97757",
        history_supported=True,
    ),
    AgentToolSpec(
        name="opencode",
        label="OpenCode",
        command="opencode",
        npm_package="opencode-ai",
        install_command="npm install -g opencode-ai@latest",
        docs_url="https://opencode.ai/docs",
        vendor="Anomaly",
        description="An open-source coding agent with a provider-flexible terminal experience.",
        icon="agent-icons/opencode.svg",
        accent="#5F5BF4",
    ),
)

NATIVE_INSTALLERS = {
    "codex": ("https://chatgpt.com/codex/install.sh", "sh"),
    "claude": ("https://claude.ai/install.sh", "bash"),
    "opencode": ("https://opencode.ai/install", "bash"),
}
INSTALL_SOURCE_URLS = {
    "npmmirror": "https://npmmirror.com/",
    "tencent": "https://cloud.tencent.com/document/product/213/8623",
}

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
_UPDATE_LOCKS = {spec.name: threading.Lock() for spec in AGENT_TOOL_SPECS}


class AgentToolUpdateBusyError(RuntimeError):
    pass


def agent_install_options(spec: AgentToolSpec) -> tuple[AgentInstallOption, ...]:
    script_url, interpreter = NATIVE_INSTALLERS[spec.name]
    script_command = f"curl -fsSL {script_url} | {interpreter}"
    package = f"{spec.npm_package}@latest"
    windows = os.name == "nt"
    return (
        AgentInstallOption(
            id="official-native",
            method="native",
            source="official",
            provider=spec.vendor,
            command=script_command,
            script_url=script_url,
            interpreter=interpreter,
            recommended=not windows,
            source_url=spec.docs_url,
        ),
        AgentInstallOption(
            id="official-npm",
            method="npm",
            source="official",
            provider="npmjs",
            command=(f"npm install -g {package} --registry=https://registry.npmjs.org"),
            argv=(
                "npm",
                "install",
                "-g",
                package,
                "--registry=https://registry.npmjs.org",
            ),
            recommended=windows,
            source_url=f"https://www.npmjs.com/package/{spec.npm_package}",
        ),
        AgentInstallOption(
            id="npmmirror",
            method="npm",
            source="mirror",
            provider="npmmirror",
            command=(f"npm install -g {package} --registry=https://registry.npmmirror.com"),
            argv=(
                "npm",
                "install",
                "-g",
                package,
                "--registry=https://registry.npmmirror.com",
            ),
            china=True,
            source_url=INSTALL_SOURCE_URLS["npmmirror"],
        ),
        AgentInstallOption(
            id="tencent-mirror",
            method="npm",
            source="mirror",
            provider="Tencent Cloud",
            command=(f"npm install -g {package} --registry=https://mirrors.cloud.tencent.com/npm/"),
            argv=(
                "npm",
                "install",
                "-g",
                package,
                "--registry=https://mirrors.cloud.tencent.com/npm/",
            ),
            china=True,
            source_url=INSTALL_SOURCE_URLS["tencent"],
        ),
    )


def agent_install_option(spec: AgentToolSpec, option_id: str) -> AgentInstallOption:
    normalized = str(option_id or "").strip().lower()
    option = next((item for item in agent_install_options(spec) if item.id == normalized), None)
    if option is None:
        raise ValueError(f"Unsupported install option for {spec.label}: {option_id}")
    return option


def install_option_payload(
    option: AgentInstallOption,
    reported: object = None,
) -> dict[str, object]:
    raw = reported if isinstance(reported, dict) else None
    if raw is None:
        missing = [name for name in option.requirements if not shutil.which(name)]
        available = not missing
    else:
        allowed_requirements = set(option.requirements)
        raw_missing = raw.get("missing_requirements")
        missing = (
            [str(name) for name in raw_missing if str(name) in allowed_requirements]
            if isinstance(raw_missing, list)
            else list(option.requirements)
        )
        available = bool(raw.get("available")) and not missing
    return {
        "id": option.id,
        "method": option.method,
        "source": option.source,
        "provider": option.provider,
        "command": option.command,
        "recommended": option.recommended,
        "china": option.china,
        "source_url": option.source_url,
        "available": available,
        "missing_requirements": missing,
    }


def install_options_payload(
    spec: AgentToolSpec,
    reported: object = None,
) -> list[dict[str, object]]:
    by_id = (
        {str(item.get("id") or ""): item for item in reported if isinstance(item, dict)}
        if isinstance(reported, list)
        else None
    )
    return [
        install_option_payload(option, None if by_id is None else by_id.get(option.id, {}))
        for option in agent_install_options(spec)
    ]


def agent_tools_payload(*, force: bool = False) -> dict[str, object]:
    cache_key = "\0".join(os.environ.get(name, "") for name in ("PATH", "HOME", "CODEX_HOME"))
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
            "installs_supported": True,
            "scope": "executable",
            "checked_at": utc_timestamp(),
            "cache_ttl_seconds": int(AGENT_TOOL_CACHE_TTL_SECONDS),
            "tools": tools,
            "error": "",
        }
        _CACHE[cache_key] = (time.monotonic(), payload)
        return copy.deepcopy(payload)


def probe_agent_tool(spec: AgentToolSpec) -> dict[str, object]:
    environment = probe_environment(spec.name)
    executable = shutil.which(spec.command, path=environment.get("PATH"))
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
            env=environment,
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
        update=probe_agent_update_status(spec, output),
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
    update: object = None,
    auth: object = None,
    usage: object = None,
    install_options: object = None,
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
            "Access state is unavailable until this CLI installation is ready.",
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
    normalized_update = normalize_agent_update_status(update)
    update_is_current = (
        normalized_status == "available" and normalized_update["status"] == "up_to_date"
    )
    managed_update_command = (
        "" if update_is_current else update_command(spec, normalized_status, method)
    )
    if update_is_current:
        update_action = "current"
    elif normalized_status == "unknown":
        update_action = ""
    else:
        update_action = "install" if normalized_status == "missing" else "update"
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
        "update_command": managed_update_command,
        "update_action": update_action,
        "update_note": update_note(normalized_status, method),
        "update": normalized_update,
        "install_options": install_options_payload(spec, install_options),
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
        "installs_supported": False,
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
                update=raw.get("update"),
                auth=raw.get("auth"),
                usage=raw.get("usage"),
                install_options=raw.get("install_options", []),
            )
        )
    return {
        "supported": bool(payload.get("supported", True)),
        "updates_supported": bool(payload.get("updates_supported", False)),
        "installs_supported": bool(payload.get("installs_supported", False)),
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


def install_agent_tool(name: str, option_id: str) -> dict[str, object]:
    spec = agent_tool_spec(name)
    if spec is None:
        raise ValueError(f"Unsupported Agent CLI: {name}")
    option = agent_install_option(spec, option_id)
    lock = _UPDATE_LOCKS[spec.name]
    if not lock.acquire(blocking=False):
        raise AgentToolUpdateBusyError(f"{spec.label} is already being maintained.")
    try:
        before = probe_agent_tool(spec)
        result: dict[str, object] = {
            "ok": False,
            "agent": spec.name,
            "label": spec.label,
            "option": option.id,
            "source": option.source,
            "provider": option.provider,
            "command": option.command,
            "before_status": str(before.get("status") or "unknown"),
            "after_status": str(before.get("status") or "unknown"),
            "after_version": str(before.get("version") or ""),
            "changed": False,
            "output": "",
            "error": "",
            "checked_at": utc_timestamp(),
        }
        if before.get("status") == "available":
            result["ok"] = True
            return normalize_agent_install_result(spec.name, result)

        missing = [name for name in option.requirements if not shutil.which(name)]
        if missing:
            result["error"] = f"Required command not found: {', '.join(missing)}"
            return normalize_agent_install_result(spec.name, result)

        returncode, output = run_agent_install_option(option)
        result["output"] = output
        if returncode != 0:
            result["error"] = output or f"Install command exited with code {returncode}."
            return normalize_agent_install_result(spec.name, result)

        clear_agent_tools_cache()
        after = probe_agent_tool(spec)
        installed = after.get("status") == "available"
        result.update(
            {
                "ok": installed,
                "after_status": str(after.get("status") or "unknown"),
                "after_version": str(after.get("version") or ""),
                "changed": installed,
                "error": ""
                if installed
                else (
                    "The installer completed, but the CLI is still missing from the "
                    "Node service PATH. Reload the service or review the install output."
                ),
                "checked_at": utc_timestamp(),
            }
        )
        return normalize_agent_install_result(spec.name, result)
    finally:
        lock.release()


def run_agent_install_option(option: AgentInstallOption) -> tuple[int, str]:
    if option.argv:
        return run_agent_command(option.argv, timeout=AGENT_TOOL_INSTALL_TIMEOUT_SECONDS)
    if not option.script_url or not option.interpreter:
        return 2, "Install option is not executable."
    try:
        script = download_install_script(option.script_url)
    except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
        return 1, clean_update_output(exc)
    try:
        with tempfile.TemporaryDirectory(prefix="staragent-install-") as directory:
            path = Path(directory) / "install.sh"
            path.write_bytes(script)
            path.chmod(0o700)
            return run_agent_command(
                (option.interpreter, str(path)),
                timeout=AGENT_TOOL_INSTALL_TIMEOUT_SECONDS,
            )
    except OSError as exc:
        return 1, clean_update_output(exc)


def download_install_script(url: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "chatgpt.com",
        "claude.ai",
        "opencode.ai",
    }:
        raise ValueError("Installer URL is not allowlisted.")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "StarAgent harness installer"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        content_length = safe_int(response.headers.get("Content-Length"))
        if content_length > AGENT_TOOL_INSTALL_SCRIPT_MAX_BYTES:
            raise RuntimeError("Installer script is too large.")
        script = response.read(AGENT_TOOL_INSTALL_SCRIPT_MAX_BYTES + 1)
    if len(script) > AGENT_TOOL_INSTALL_SCRIPT_MAX_BYTES:
        raise RuntimeError("Installer script is too large.")
    stripped = script.lstrip()
    if not stripped or stripped[:16].lower().startswith((b"<!doctype", b"<html")):
        raise RuntimeError("Installer endpoint did not return a shell script.")
    return script


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
        update = before.get("update")
        if isinstance(update, dict) and update.get("status") == "up_to_date":
            base_result["ok"] = True
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
    return run_agent_command(argv, timeout=AGENT_TOOL_UPDATE_TIMEOUT_SECONDS)


def run_agent_command(
    argv: tuple[str, ...] | list[str],
    *,
    timeout: float,
) -> tuple[int, str]:
    command = list(argv)
    try:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=update_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        output = clean_update_output(exc.stdout, exc.stderr)
        detail = f"Command timed out after {timeout:g}s."
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


def normalize_agent_install_result(name: str, value: object) -> dict[str, object]:
    spec = agent_tool_spec(name)
    if spec is None:
        raise ValueError(f"Unsupported Agent CLI: {name}")
    payload = value if isinstance(value, dict) else {}
    options = {option.id: option for option in agent_install_options(spec)}
    option = options.get(str(payload.get("option") or ""))
    command = clean_tool_text(payload.get("command"), max_chars=300)
    valid_option = option is not None and command == option.command
    if not valid_option:
        command = ""
    error = clean_update_output(payload.get("error"))
    if payload.get("ok") and not valid_option and not error:
        error = "Node returned an invalid installation result."
    return {
        "ok": bool(payload.get("ok")) and valid_option,
        "agent": spec.name,
        "label": spec.label,
        "option": option.id if option else "",
        "source": option.source if option else "",
        "provider": option.provider if option else "",
        "command": command,
        "before_status": normalize_tool_status(payload.get("before_status")),
        "after_status": normalize_tool_status(payload.get("after_status")),
        "after_version": clean_tool_text(payload.get("after_version"), max_chars=120),
        "changed": bool(payload.get("changed")),
        "output": clean_update_output(payload.get("output")),
        "error": error,
        "checked_at": clean_tool_text(payload.get("checked_at"), max_chars=80),
    }


def normalize_tool_status(value: object) -> str:
    status = str(value or "unknown").lower()
    return status if status in AGENT_TOOL_STATUSES else "unknown"


def first_output_line(*values: str) -> str:
    for value in values:
        for line in strip_ansi(value or "").splitlines():
            if line.strip():
                return clean_tool_text(line)
    return ""


def clean_tool_text(value: Any, *, max_chars: int = 240) -> str:
    text = strip_ansi(str(value or "")).replace("\x00", "").strip()
    return f"{text[:max_chars]}…" if len(text) > max_chars else text


def safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_agent_update_status(value: object) -> dict[str, str]:
    payload = value if isinstance(value, dict) else {}
    status = str(payload.get("status") or "unknown")
    if status not in AGENT_UPDATE_STATUSES:
        status = "unknown"
    return {
        "status": status,
        "current_version": clean_tool_text(payload.get("current_version"), max_chars=80),
        "latest_version": clean_tool_text(payload.get("latest_version"), max_chars=80),
        "checked_at": clean_tool_text(payload.get("checked_at"), max_chars=80),
        "source": "codex_version_cache" if payload.get("source") == "codex_version_cache" else "",
    }


def probe_agent_update_status(spec: AgentToolSpec, installed_version: str) -> dict[str, str]:
    if spec.name == "codex":
        return probe_codex_update_status(installed_version)
    return normalize_agent_update_status(None)


def probe_codex_update_status(
    installed_version: str,
    *,
    cache_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    path = cache_path or codex_update_cache_path()
    try:
        if path.stat().st_size > CODEX_UPDATE_CACHE_MAX_BYTES:
            return normalize_agent_update_status(None)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return normalize_agent_update_status(None)
    if not isinstance(payload, dict):
        return normalize_agent_update_status(None)

    latest_text = clean_tool_text(payload.get("latest_version"), max_chars=80)
    checked_at = clean_tool_text(payload.get("last_checked_at"), max_chars=80)
    current = numeric_cli_version(installed_version)
    latest = numeric_cli_version(latest_text)
    status = "unknown"
    if current and latest and update_cache_is_fresh(checked_at, now=now):
        status = "update_available" if latest > current else "up_to_date"
    return normalize_agent_update_status(
        {
            "status": status,
            "current_version": version_text(current),
            "latest_version": version_text(latest) or latest_text,
            "checked_at": checked_at,
            "source": "codex_version_cache",
        }
    )


def codex_update_cache_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    return (
        Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    ) / "version.json"


def numeric_cli_version(value: object) -> tuple[int, int, int] | None:
    match = CLI_VERSION_PATTERN.search(str(value or ""))
    return tuple(int(part) for part in match.groups()) if match else None


def version_text(value: tuple[int, int, int] | None) -> str:
    return ".".join(str(part) for part in value) if value else ""


def update_cache_is_fresh(value: str, *, now: datetime | None = None) -> bool:
    try:
        checked_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    age = ((now or datetime.now(UTC)) - checked_at.astimezone(UTC)).total_seconds()
    return -300 <= age <= CODEX_UPDATE_CACHE_MAX_AGE_SECONDS


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def probe_environment(agent: str = "") -> dict[str, str]:
    environment = harness_process_environment(agent) if agent else os.environ.copy()
    home = Path(environment.get("HOME") or Path.home()).expanduser()
    search_path = environment.get("PATH", "")
    path_entries = [entry for entry in search_path.split(os.pathsep) if entry]
    for directory in (
        home / ".local" / "bin",
        home / ".opencode" / "bin",
        home / ".claude" / "local",
    ):
        value = str(directory)
        if value not in path_entries:
            path_entries.append(value)
    return {
        **environment,
        "PATH": os.pathsep.join(path_entries),
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
            "vendor": spec.vendor,
            "description": spec.description,
            "icon": spec.icon,
            "accent": spec.accent,
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
