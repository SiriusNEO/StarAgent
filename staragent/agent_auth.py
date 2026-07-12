from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from typing import Any

from staragent.text import strip_ansi

AGENT_AUTH_TIMEOUT_SECONDS = 3.0
AGENT_AUTH_STATUSES = {
    "authenticated",
    "not_authenticated",
    "configured",
    "not_configured",
    "unavailable",
    "error",
    "unknown",
}
AUTH_ACTIONS = {
    "codex": "codex login",
    "claude": "claude auth login",
    "opencode": "opencode auth login",
}


def probe_agent_auth(agent: str, executable: str) -> dict[str, object]:
    if agent == "codex":
        return probe_codex_auth(executable)
    if agent == "claude":
        return probe_claude_auth(executable)
    if agent == "opencode":
        return probe_opencode_auth(executable)
    return unknown_agent_auth(agent, "Login detection is not supported for this CLI.")


def probe_codex_auth(executable: str) -> dict[str, object]:
    try:
        result = subprocess.run(
            [executable, "login", "status"],
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=AGENT_AUTH_TIMEOUT_SECONDS,
            env=auth_environment(),
        )
    except subprocess.TimeoutExpired:
        return auth_error("codex", "Codex login check timed out.")
    except OSError as exc:
        return auth_error("codex", exc)

    output = first_auth_line(result.stdout, result.stderr)
    if result.returncode == 0:
        method = ""
        match = re.search(r"logged\s+in\s+using\s+(.+)$", output, flags=re.IGNORECASE)
        if match:
            method = clean_auth_text(match.group(1), max_chars=80)
        return agent_auth_status(
            "codex",
            status="authenticated",
            source="codex-login-status",
            method=method,
            detail="Codex reports an active login.",
        )
    if "not logged in" in output.lower():
        return agent_auth_status(
            "codex",
            status="not_authenticated",
            source="codex-login-status",
            action=AUTH_ACTIONS["codex"],
            detail="Run Codex login to authenticate this service account.",
        )
    return auth_error(
        "codex",
        output or f"Codex login check exited with code {result.returncode}.",
        source="codex-login-status",
    )


def probe_claude_auth(executable: str) -> dict[str, object]:
    try:
        result = subprocess.run(
            [executable, "auth", "status", "--json"],
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=AGENT_AUTH_TIMEOUT_SECONDS,
            env=auth_environment(),
        )
    except subprocess.TimeoutExpired:
        return auth_error("claude", "Claude authentication check timed out.")
    except OSError as exc:
        return auth_error("claude", exc)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        logged_in = payload.get("loggedIn")
        if isinstance(logged_in, bool):
            return agent_auth_status(
                "claude",
                status="authenticated" if logged_in else "not_authenticated",
                source="claude-auth-status",
                method=clean_auth_text(payload.get("authMethod"), max_chars=80),
                action="" if logged_in else AUTH_ACTIONS["claude"],
                detail=(
                    "Claude Code reports an active login."
                    if logged_in
                    else "Run Claude authentication to sign in this service account."
                ),
            )
    output = first_auth_line(result.stderr, result.stdout)
    return auth_error(
        "claude",
        output or f"Claude authentication check exited with code {result.returncode}.",
        source="claude-auth-status",
    )


def probe_opencode_auth(executable: str) -> dict[str, object]:
    try:
        result = subprocess.run(
            [executable, "auth", "list"],
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=AGENT_AUTH_TIMEOUT_SECONDS,
            env=auth_environment(),
        )
    except subprocess.TimeoutExpired:
        return auth_error("opencode", "OpenCode authentication check timed out.")
    except OSError as exc:
        return auth_error("opencode", exc)

    output = strip_ansi(f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0:
        return auth_error(
            "opencode",
            first_auth_line(result.stderr, result.stdout)
            or f"OpenCode authentication check exited with code {result.returncode}.",
            source="opencode-auth-list",
        )
    counts = [
        int(value)
        for value in re.findall(
            r"\b(\d+)\s+(?:credentials?|environment\s+variables?)\b",
            output,
            flags=re.IGNORECASE,
        )
    ]
    if counts:
        provider_count = sum(counts)
        if provider_count:
            noun = "credential source" if provider_count == 1 else "credential sources"
            return agent_auth_status(
                "opencode",
                status="configured",
                source="opencode-auth-list",
                method="Provider credentials",
                provider_count=provider_count,
                detail=f"OpenCode reports configured {noun}.",
            )
        return agent_auth_status(
            "opencode",
            status="not_configured",
            source="opencode-auth-list",
            action=AUTH_ACTIONS["opencode"],
            detail="OpenCode reports no configured provider credentials.",
        )
    if "no credentials" in output.lower():
        return agent_auth_status(
            "opencode",
            status="not_configured",
            source="opencode-auth-list",
            action=AUTH_ACTIONS["opencode"],
            detail="OpenCode reports no configured provider credentials.",
        )
    return unknown_agent_auth(
        "opencode",
        "OpenCode did not report a credential count.",
        source="opencode-auth-list",
    )


def agent_auth_status(
    agent: str,
    *,
    status: str,
    source: str = "",
    method: str = "",
    action: str = "",
    detail: str = "",
    provider_count: int = 0,
) -> dict[str, object]:
    return normalize_agent_auth(
        agent,
        {
            "status": status,
            "source": source,
            "checked_at": utc_timestamp(),
            "method": method,
            "action": action,
            "detail": detail,
            "provider_count": provider_count,
        },
    )


def normalize_agent_auth(agent: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return unknown_agent_auth(agent)
    status = str(value.get("status") or "unknown").strip().lower()
    if status not in AGENT_AUTH_STATUSES:
        status = "unknown"
    allowed_action = AUTH_ACTIONS.get(agent, "")
    action = clean_auth_text(value.get("action"), max_chars=100)
    if action != allowed_action:
        action = ""
    authenticated: bool | None = None
    if status == "authenticated":
        authenticated = True
    elif status == "not_authenticated":
        authenticated = False
    return {
        "status": status,
        "authenticated": authenticated,
        "source": clean_auth_text(value.get("source"), max_chars=80),
        "checked_at": clean_auth_text(value.get("checked_at"), max_chars=80),
        "method": clean_auth_text(value.get("method"), max_chars=80),
        "action": action,
        "detail": clean_auth_text(value.get("detail"), max_chars=300),
        "provider_count": max(0, min(100, safe_int(value.get("provider_count")))),
    }


def unknown_agent_auth(
    agent: str,
    detail: str = (
        "Login state was not reported by this Node; update StarAgent there if it remains unknown."
    ),
    *,
    source: str = "",
) -> dict[str, object]:
    return agent_auth_status(agent, status="unknown", source=source, detail=detail)


def unavailable_agent_auth(agent: str, detail: str) -> dict[str, object]:
    return agent_auth_status(agent, status="unavailable", detail=detail)


def auth_error(agent: str, detail: object, *, source: str = "") -> dict[str, object]:
    return agent_auth_status(agent, status="error", source=source, detail=clean_auth_text(detail))


def auth_environment() -> dict[str, str]:
    return {
        **os.environ,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def first_auth_line(*values: str) -> str:
    lines = []
    for value in values:
        lines.extend(line.strip() for line in strip_ansi(value or "").splitlines() if line.strip())
    preferred = [line for line in lines if not line.lower().startswith("warning:")]
    return clean_auth_text((preferred or lines)[-1] if lines else "")


def clean_auth_text(value: Any, *, max_chars: int = 240) -> str:
    text = " ".join(strip_ansi(str(value or "")).replace("\x00", "").split())
    return f"{text[:max_chars]}…" if len(text) > max_chars else text


def safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
