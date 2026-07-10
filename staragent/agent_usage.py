from __future__ import annotations

import json
import os
import select
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from staragent.text import strip_ansi

AGENT_USAGE_STATUSES = {"available", "manual", "unavailable", "error", "unknown", "unsupported"}
CODEX_USAGE_TIMEOUT_SECONDS = 4.0
CLAUDE_AUTH_TIMEOUT_SECONDS = 3.0
MAX_USAGE_BUCKETS = 8
MAX_USAGE_WINDOWS = 3


def probe_agent_usage(agent: str, executable: str) -> dict[str, object]:
    if agent == "codex":
        return probe_codex_usage(executable)
    if agent == "claude":
        return probe_claude_usage(executable)
    return unsupported_agent_usage(agent)


def probe_codex_usage(executable: str) -> dict[str, object]:
    try:
        result = codex_app_server_request(
            executable,
            "account/rateLimits/read",
            timeout=CODEX_USAGE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return agent_usage_status(
            "codex",
            status="error",
            source="codex-app-server",
            message=f"Codex usage check timed out after {CODEX_USAGE_TIMEOUT_SECONDS:g}s.",
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return agent_usage_status(
            "codex",
            status="error",
            source="codex-app-server",
            message=clean_usage_text(exc),
        )

    buckets = codex_usage_buckets(result)
    if not buckets:
        return agent_usage_status(
            "codex",
            status="unavailable",
            source="codex-app-server",
            message="Codex did not report account rate limits. Check its login state.",
        )
    reset_credits = result.get("rateLimitResetCredits")
    available_resets = (
        safe_int(reset_credits.get("availableCount")) if isinstance(reset_credits, dict) else 0
    )
    return agent_usage_status(
        "codex",
        status="available",
        source="codex-app-server",
        buckets=buckets,
        reset_credits=available_resets,
        message="Read from Codex account rate limits; no model request was sent.",
    )


def probe_claude_usage(executable: str) -> dict[str, object]:
    authenticated: bool | None = None
    auth_method = ""
    detail = ""
    try:
        result = subprocess.run(
            [executable, "auth", "status", "--json"],
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=CLAUDE_AUTH_TIMEOUT_SECONDS,
            env=usage_environment(),
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            if isinstance(payload, dict):
                authenticated = bool(payload.get("loggedIn"))
                auth_method = clean_usage_text(payload.get("authMethod"), max_chars=80)
        else:
            detail = first_usage_line(result.stderr, result.stdout)
    except subprocess.TimeoutExpired:
        detail = f"Claude authentication check timed out after {CLAUDE_AUTH_TIMEOUT_SECONDS:g}s."
    except (OSError, json.JSONDecodeError) as exc:
        detail = clean_usage_text(exc)

    message = "Claude Code exposes remaining allocation interactively through /status."
    if detail:
        message = f"{message} Authentication check: {detail}"
    return agent_usage_status(
        "claude",
        status="manual",
        source="claude-status",
        authenticated=authenticated,
        auth_method=auth_method,
        action="/status",
        message=message,
    )


def codex_app_server_request(
    executable: str,
    method: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    process = subprocess.Popen(
        [executable, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=usage_environment(),
    )
    deadline = time.monotonic() + timeout
    try:
        send_app_server_message(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "staragent",
                        "title": "StarAgent",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        initialized = read_app_server_response(process, 1, deadline)
        raise_for_app_server_error(initialized)
        send_app_server_message(process, {"method": "initialized"})
        send_app_server_message(process, {"id": 2, "method": method, "params": None})
        response = read_app_server_response(process, 2, deadline)
        raise_for_app_server_error(response)
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("Codex returned an invalid usage response.")
        return result
    finally:
        if process.stdin is not None:
            with suppress(OSError):
                process.stdin.close()
        if process.poll() is None:
            with suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    process.kill()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1)


def send_app_server_message(process: subprocess.Popen[str], payload: dict[str, object]) -> None:
    if process.stdin is None:
        raise RuntimeError("Codex app-server stdin is unavailable.")
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()


def read_app_server_response(
    process: subprocess.Popen[str],
    request_id: int,
    deadline: float,
) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("Codex app-server stdout is unavailable.")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Codex app-server request {request_id} timed out.")
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready:
            raise TimeoutError(f"Codex app-server request {request_id} timed out.")
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f"Codex app-server exited before request {request_id} completed.")
        payload = json.loads(line)
        if isinstance(payload, dict) and payload.get("id") == request_id:
            return payload


def raise_for_app_server_error(response: dict[str, Any]) -> None:
    error = response.get("error")
    if not error:
        return
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or "unknown error"
    else:
        message = error
    raise RuntimeError(f"Codex usage check failed: {clean_usage_text(message)}")


def codex_usage_buckets(result: dict[str, Any]) -> list[dict[str, object]]:
    by_id = result.get("rateLimitsByLimitId")
    snapshots: list[tuple[str, object]] = []
    if isinstance(by_id, dict) and by_id:
        snapshots.extend((str(key), value) for key, value in by_id.items())
    elif isinstance(result.get("rateLimits"), dict):
        snapshot = result["rateLimits"]
        snapshots.append((str(snapshot.get("limitId") or "codex"), snapshot))
    snapshots.sort(key=lambda item: (item[0] != "codex", item[0]))
    return [
        bucket
        for key, value in snapshots[:MAX_USAGE_BUCKETS]
        if (bucket := normalize_codex_bucket(key, value)) is not None
    ]


def normalize_codex_bucket(key: str, value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    limit_id = clean_usage_text(value.get("limitId") or key, max_chars=80)
    limit_name = clean_usage_text(value.get("limitName"), max_chars=120)
    windows = []
    for kind in ("primary", "secondary"):
        window = normalize_rate_window(value.get(kind), kind)
        if window:
            windows.append(window)
    if not windows:
        return None
    credits = normalize_credits(value.get("credits"))
    individual_limit = normalize_individual_limit(value.get("individualLimit"))
    return {
        "id": limit_id,
        "label": limit_name or ("Codex" if limit_id == "codex" else limit_id),
        "plan": clean_usage_text(value.get("planType"), max_chars=80),
        "windows": windows[:MAX_USAGE_WINDOWS],
        "credits": credits,
        "individual_limit": individual_limit,
        "reached": clean_usage_text(value.get("rateLimitReachedType"), max_chars=120),
    }


def normalize_rate_window(value: object, kind: str) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    used = clamp_percent(value.get("usedPercent"))
    minutes = max(0, safe_int(value.get("windowDurationMins")))
    resets_at = unix_timestamp_iso(value.get("resetsAt"))
    return {
        "kind": kind,
        "label": usage_window_label(minutes, kind),
        "used_percent": used,
        "remaining_percent": round(max(0.0, 100.0 - used), 1),
        "window_minutes": minutes,
        "resets_at": resets_at,
    }


def normalize_credits(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        "has_credits": bool(value.get("hasCredits")),
        "unlimited": bool(value.get("unlimited")),
        "balance": clean_usage_text(value.get("balance"), max_chars=80),
    }


def normalize_individual_limit(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        "limit": clean_usage_text(value.get("limit"), max_chars=80),
        "used": clean_usage_text(value.get("used"), max_chars=80),
        "remaining_percent": clamp_percent(value.get("remainingPercent")),
        "resets_at": unix_timestamp_iso(value.get("resetsAt")),
    }


def normalize_agent_usage(agent: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return unknown_agent_usage(agent)
    status = str(value.get("status") or "unknown").strip().lower()
    if status not in AGENT_USAGE_STATUSES:
        status = "unknown"
    raw_buckets = value.get("buckets")
    buckets = []
    if isinstance(raw_buckets, list):
        for raw in raw_buckets[:MAX_USAGE_BUCKETS]:
            bucket = normalize_public_bucket(raw)
            if bucket:
                buckets.append(bucket)
    authenticated = value.get("authenticated")
    return {
        "status": status,
        "source": clean_usage_text(value.get("source"), max_chars=80),
        "checked_at": clean_usage_text(value.get("checked_at"), max_chars=80),
        "buckets": buckets,
        "reset_credits": max(0, safe_int(value.get("reset_credits"))),
        "authenticated": authenticated if isinstance(authenticated, bool) else None,
        "auth_method": clean_usage_text(value.get("auth_method"), max_chars=80),
        "action": "/status" if value.get("action") == "/status" else "",
        "message": clean_usage_text(value.get("message"), max_chars=500),
    }


def normalize_public_bucket(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    raw_windows = value.get("windows")
    windows = []
    if isinstance(raw_windows, list):
        for raw in raw_windows[:MAX_USAGE_WINDOWS]:
            if not isinstance(raw, dict):
                continue
            minutes = max(0, safe_int(raw.get("window_minutes")))
            kind = clean_usage_text(raw.get("kind"), max_chars=30)
            used = clamp_percent(raw.get("used_percent"))
            windows.append(
                {
                    "kind": kind,
                    "label": clean_usage_text(raw.get("label"), max_chars=80)
                    or usage_window_label(minutes, kind),
                    "used_percent": used,
                    "remaining_percent": round(max(0.0, 100.0 - used), 1),
                    "window_minutes": minutes,
                    "resets_at": clean_usage_text(raw.get("resets_at"), max_chars=80),
                }
            )
    if not windows:
        return None
    return {
        "id": clean_usage_text(value.get("id"), max_chars=80),
        "label": clean_usage_text(value.get("label"), max_chars=120),
        "plan": clean_usage_text(value.get("plan"), max_chars=80),
        "windows": windows,
        "credits": normalize_public_credits(value.get("credits")),
        "individual_limit": normalize_public_individual_limit(value.get("individual_limit")),
        "reached": clean_usage_text(value.get("reached"), max_chars=120),
    }


def normalize_public_credits(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        "has_credits": bool(value.get("has_credits")),
        "unlimited": bool(value.get("unlimited")),
        "balance": clean_usage_text(value.get("balance"), max_chars=80),
    }


def normalize_public_individual_limit(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        "limit": clean_usage_text(value.get("limit"), max_chars=80),
        "used": clean_usage_text(value.get("used"), max_chars=80),
        "remaining_percent": clamp_percent(value.get("remaining_percent")),
        "resets_at": clean_usage_text(value.get("resets_at"), max_chars=80),
    }


def agent_usage_status(
    agent: str,
    *,
    status: str,
    source: str = "",
    buckets: list[dict[str, object]] | None = None,
    reset_credits: int = 0,
    authenticated: bool | None = None,
    auth_method: str = "",
    action: str = "",
    message: str = "",
) -> dict[str, object]:
    return normalize_agent_usage(
        agent,
        {
            "status": status,
            "source": source,
            "checked_at": utc_timestamp(),
            "buckets": buckets or [],
            "reset_credits": reset_credits,
            "authenticated": authenticated,
            "auth_method": auth_method,
            "action": action,
            "message": message,
        },
    )


def unknown_agent_usage(
    agent: str, message: str = "Usage data was not reported by this Node."
) -> dict[str, object]:
    if agent not in {"codex", "claude"}:
        return unsupported_agent_usage(agent)
    return agent_usage_status(agent, status="unknown", message=message)


def unavailable_agent_usage(agent: str, message: str) -> dict[str, object]:
    if agent not in {"codex", "claude"}:
        return unsupported_agent_usage(agent)
    return agent_usage_status(agent, status="unavailable", message=message)


def unsupported_agent_usage(agent: str) -> dict[str, object]:
    return agent_usage_status(
        agent,
        status="unsupported",
        message="Remaining usage is not available from this CLI.",
    )


def usage_window_label(minutes: int, kind: str = "") -> str:
    if minutes == 300:
        return "5 hours"
    if minutes == 10080:
        return "Weekly"
    if minutes and minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day" if days == 1 else f"{days} days"
    if minutes and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if minutes:
        return f"{minutes} min"
    return "Primary" if kind == "primary" else "Secondary"


def unix_timestamp_iso(value: object) -> str:
    timestamp = safe_int(value)
    if timestamp <= 0:
        return ""
    try:
        return (
            datetime.fromtimestamp(timestamp, tz=UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return ""


def clamp_percent(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(min(100.0, max(0.0, number)), 1)


def safe_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def first_usage_line(*values: str) -> str:
    for value in values:
        for line in strip_ansi(value or "").splitlines():
            if line.strip():
                return clean_usage_text(line)
    return ""


def clean_usage_text(value: Any, *, max_chars: int = 240) -> str:
    text = strip_ansi(str(value or "")).replace("\x00", "").strip()
    return f"{text[:max_chars]}…" if len(text) > max_chars else text


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def usage_environment() -> dict[str, str]:
    return {
        **os.environ,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }
