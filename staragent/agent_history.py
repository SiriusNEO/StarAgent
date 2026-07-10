from __future__ import annotations

import json
import os
import re
import shlex
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from staragent.agent_tools import clean_tool_text

HISTORY_CACHE_TTL_SECONDS = 30.0
MAX_HISTORY_RESULTS = 100
MAX_HISTORY_FILES_PER_AGENT = 160
MAX_HISTORY_INDEX_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
HISTORY_AGENTS = ("codex", "claude")
HISTORY_PRIVACY_NOTE = (
    "Metadata and short prompt previews only; source history files are not modified."
)
SESSION_ID_PATTERN = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)

_HISTORY_CACHE_LOCK = threading.Lock()
_HISTORY_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, object]]]] = {}


def agent_history_payload(
    *,
    agent: str = "",
    limit: int = 50,
    force: bool = False,
) -> dict[str, object]:
    normalized_agent = str(agent or "").strip().lower()
    if normalized_agent and normalized_agent not in HISTORY_AGENTS:
        raise ValueError(f"History scanning is not supported for: {normalized_agent}")
    limit = max(1, min(int(limit), MAX_HISTORY_RESULTS))
    selected_agents = (normalized_agent,) if normalized_agent else HISTORY_AGENTS
    sessions: list[dict[str, object]] = []
    for name in selected_agents:
        sessions.extend(cached_agent_history(name, force=force))
    sessions.sort(key=lambda item: float(item.get("updated_epoch") or 0), reverse=True)
    public_sessions = [public_history_entry(item) for item in sessions[:limit]]
    return {
        "supported": True,
        "agents": list(HISTORY_AGENTS),
        "sessions": public_sessions,
        "scanned_at": utc_timestamp(),
        "cache_ttl_seconds": int(HISTORY_CACHE_TTL_SECONDS),
        "truncated": len(sessions) > limit,
        "privacy": HISTORY_PRIVACY_NOTE,
        "error": "",
    }


def unavailable_agent_history_payload(error: str) -> dict[str, object]:
    return {
        "supported": False,
        "agents": list(HISTORY_AGENTS),
        "sessions": [],
        "scanned_at": "",
        "cache_ttl_seconds": int(HISTORY_CACHE_TTL_SECONDS),
        "truncated": False,
        "privacy": HISTORY_PRIVACY_NOTE,
        "error": clean_tool_text(error, max_chars=500),
    }


def normalize_agent_history_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return unavailable_agent_history_payload("Node returned an invalid history payload.")
    raw_sessions = payload.get("sessions")
    sessions = []
    if isinstance(raw_sessions, list):
        for raw in raw_sessions[:MAX_HISTORY_RESULTS]:
            normalized = normalize_history_entry(raw)
            if normalized:
                sessions.append(normalized)
    return {
        "supported": bool(payload.get("supported", True)),
        "agents": list(HISTORY_AGENTS),
        "sessions": sessions,
        "scanned_at": clean_tool_text(payload.get("scanned_at"), max_chars=80),
        "cache_ttl_seconds": int(HISTORY_CACHE_TTL_SECONDS),
        "truncated": bool(payload.get("truncated", False)),
        "privacy": HISTORY_PRIVACY_NOTE,
        "error": clean_tool_text(payload.get("error"), max_chars=500),
    }


def history_payload_with_node(payload: object, node_name: str) -> dict[str, object]:
    normalized = normalize_agent_history_payload(payload)
    normalized["node"] = clean_tool_text(node_name, max_chars=80)
    return normalized


def normalize_history_entry(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    agent = clean_tool_text(value.get("agent"), max_chars=20).lower()
    session_id = clean_tool_text(value.get("id"), max_chars=100)
    if agent not in HISTORY_AGENTS or not session_id:
        return None
    try:
        prompt_count = max(0, int(value.get("prompt_count") or 0))
    except (TypeError, ValueError):
        prompt_count = 0
    try:
        size_bytes = max(0, int(value.get("size_bytes") or 0))
    except (TypeError, ValueError):
        size_bytes = 0
    return {
        "id": session_id,
        "agent": agent,
        "label": clean_tool_text(value.get("label"), max_chars=80),
        "title": clean_preview(value.get("title")),
        "cwd": clean_tool_text(value.get("cwd"), max_chars=1000),
        "created_at": clean_tool_text(value.get("created_at"), max_chars=80),
        "updated_at": clean_tool_text(value.get("updated_at"), max_chars=80),
        "cli_version": clean_tool_text(value.get("cli_version"), max_chars=100),
        "git_branch": clean_tool_text(value.get("git_branch"), max_chars=200),
        "prompt_count": prompt_count,
        "size_bytes": size_bytes,
        "resume_command": clean_tool_text(value.get("resume_command"), max_chars=2000),
    }


def cached_agent_history(agent: str, *, force: bool = False) -> list[dict[str, object]]:
    cache_key = (str(Path.home()), agent)
    now = time.monotonic()
    with _HISTORY_CACHE_LOCK:
        cached = _HISTORY_CACHE.get(cache_key)
        if cached and not force and now - cached[0] < HISTORY_CACHE_TTL_SECONDS:
            return [dict(item) for item in cached[1]]
    sessions = scan_codex_history() if agent == "codex" else scan_claude_history()
    with _HISTORY_CACHE_LOCK:
        _HISTORY_CACHE[cache_key] = (time.monotonic(), sessions)
    return [dict(item) for item in sessions]


def scan_codex_history() -> list[dict[str, object]]:
    prompt_index = codex_prompt_index()
    sessions = []
    for path, stat in recent_history_files(codex_sessions_root()):
        metadata = first_json_object(path)
        payload = metadata.get("payload") if isinstance(metadata, dict) else None
        if not isinstance(payload, dict):
            payload = {}
        session_id = clean_tool_text(
            payload.get("id") or payload.get("session_id") or session_id_from_filename(path),
            max_chars=100,
        )
        if not session_id:
            continue
        prompt = prompt_index.get(session_id, {})
        cwd = clean_tool_text(payload.get("cwd"), max_chars=1000)
        git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
        title = clean_preview(prompt.get("title")) or f"Codex conversation {session_id[:8]}"
        sessions.append(
            {
                "id": session_id,
                "agent": "codex",
                "label": "Codex",
                "title": title,
                "cwd": cwd,
                "created_at": normalized_timestamp(payload.get("timestamp"), stat.st_mtime),
                "updated_at": epoch_timestamp(stat.st_mtime),
                "updated_epoch": stat.st_mtime,
                "cli_version": clean_tool_text(payload.get("cli_version"), max_chars=100),
                "git_branch": clean_tool_text(git.get("branch"), max_chars=200),
                "prompt_count": int(prompt.get("count") or 0),
                "size_bytes": stat.st_size,
                "resume_command": codex_resume_command(session_id, cwd),
            }
        )
    return sessions


def scan_claude_history() -> list[dict[str, object]]:
    prompt_index = claude_prompt_index()
    sessions = []
    for path, stat in recent_history_files(claude_projects_root()):
        metadata = claude_session_metadata(path)
        session_id = clean_tool_text(
            metadata.get("sessionId") or session_id_from_filename(path), max_chars=100
        )
        if not session_id:
            continue
        prompt = prompt_index.get(session_id, {})
        cwd = clean_tool_text(metadata.get("cwd") or prompt.get("project"), max_chars=1000)
        title = clean_preview(prompt.get("title")) or f"Claude conversation {session_id[:8]}"
        sessions.append(
            {
                "id": session_id,
                "agent": "claude",
                "label": "Claude Code",
                "title": title,
                "cwd": cwd,
                "created_at": normalized_timestamp(metadata.get("timestamp"), stat.st_mtime),
                "updated_at": epoch_timestamp(stat.st_mtime),
                "updated_epoch": stat.st_mtime,
                "cli_version": clean_tool_text(metadata.get("version"), max_chars=100),
                "git_branch": clean_tool_text(metadata.get("gitBranch"), max_chars=200),
                "prompt_count": int(prompt.get("count") or 0),
                "size_bytes": stat.st_size,
                "resume_command": claude_resume_command(session_id, cwd),
            }
        )
    return sessions


def codex_prompt_index() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in recent_jsonl_objects(codex_history_index_path()):
        session_id = clean_tool_text(item.get("session_id"), max_chars=100)
        text = clean_preview(item.get("text"))
        if not session_id:
            continue
        entry = result.setdefault(session_id, {"title": "", "count": 0})
        entry["count"] = int(entry["count"]) + 1
        if text:
            entry["title"] = text
    return result


def claude_prompt_index() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in recent_jsonl_objects(claude_history_index_path()):
        session_id = clean_tool_text(item.get("sessionId"), max_chars=100)
        text = clean_preview(item.get("display"))
        if not session_id:
            continue
        entry = result.setdefault(session_id, {"title": "", "count": 0, "project": ""})
        entry["count"] = int(entry["count"]) + 1
        if text:
            entry["title"] = text
        project = clean_tool_text(item.get("project"), max_chars=1000)
        if project:
            entry["project"] = project
    return result


def recent_history_files(root: Path) -> list[tuple[Path, os.stat_result]]:
    if not root.is_dir():
        return []
    files: list[tuple[Path, os.stat_result]] = []
    try:
        candidates = root.glob("**/*.jsonl")
        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                files.append((path, stat))
    except OSError:
        return []
    files.sort(key=lambda item: item[1].st_mtime, reverse=True)
    return files[:MAX_HISTORY_FILES_PER_AGENT]


def first_json_object(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            raw = handle.readline(MAX_METADATA_BYTES)
    except OSError:
        return {}
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def claude_session_metadata(path: Path) -> dict[str, object]:
    read_bytes = 0
    metadata: dict[str, object] = {}
    try:
        with path.open("rb") as handle:
            for _ in range(256):
                raw = handle.readline()
                if not raw:
                    break
                read_bytes += len(raw)
                if read_bytes > MAX_METADATA_BYTES:
                    break
                try:
                    value = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, dict):
                    continue
                for key in ("sessionId", "cwd", "timestamp", "version", "gitBranch"):
                    if value.get(key) and not metadata.get(key):
                        metadata[key] = value[key]
                if metadata.get("sessionId") and metadata.get("cwd"):
                    break
    except OSError:
        return {}
    return metadata


def recent_jsonl_objects(path: Path) -> list[dict[str, object]]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            offset = max(0, size - MAX_HISTORY_INDEX_BYTES)
            handle.seek(offset)
            data = handle.read(MAX_HISTORY_INDEX_BYTES)
    except OSError:
        return []
    lines = data.splitlines()
    if offset and lines:
        lines = lines[1:]
    result = []
    for raw in lines:
        try:
            value = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def public_history_entry(item: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in item.items() if key != "updated_epoch"}


def codex_resume_command(session_id: str, cwd: str) -> str:
    command = "codex resume"
    if cwd:
        command += f" -C {shlex.quote(cwd)}"
    return f"{command} {shlex.quote(session_id)}"


def claude_resume_command(session_id: str, cwd: str) -> str:
    command = f"claude --resume {shlex.quote(session_id)}"
    return f"cd {shlex.quote(cwd)} && {command}" if cwd else command


def clean_preview(value: object) -> str:
    return clean_tool_text(" ".join(str(value or "").split()), max_chars=160)


def session_id_from_filename(path: Path) -> str:
    stem = path.stem
    match = SESSION_ID_PATTERN.search(stem)
    return match.group(1) if match else stem


def normalized_timestamp(value: object, fallback_epoch: float) -> str:
    if isinstance(value, (int, float)):
        epoch = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return epoch_timestamp(epoch)
    text = clean_tool_text(value, max_chars=80)
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        except ValueError:
            pass
    return epoch_timestamp(fallback_epoch)


def epoch_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def codex_sessions_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def codex_history_index_path() -> Path:
    return Path.home() / ".codex" / "history.jsonl"


def claude_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def claude_history_index_path() -> Path:
    return Path.home() / ".claude" / "history.jsonl"


def clear_agent_history_cache() -> None:
    with _HISTORY_CACHE_LOCK:
        _HISTORY_CACHE.clear()
