from __future__ import annotations

import fcntl
import json
import os
import re
import time
import urllib.parse
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from staragent.paths import state_dir
from staragent.state import atomic_write_json, locked_file, read_json

LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3
NODE_OUTBOX_MAX_BYTES = 1024 * 1024
NODE_OUTBOX_BACKUPS = 1
MAX_EVENT_MESSAGE_CHARS = 8_000
MAX_EVENT_DETAIL_ITEMS = 50
MAX_INGESTED_EVENT_IDS = 5_000
LOG_LEVELS = {"debug", "info", "warning", "error", "critical"}

ANSI_PATTERN = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*")
URL_CREDENTIAL_PATTERN = re.compile(r"(?i)(https?://)([^/@\s]+)@")
SENSITIVE_PAIR_PATTERN = re.compile(
    r"(?i)(\b(?:access[_-]?token|auth(?:orization)?|api[_-]?key|password|secret|token)"
    r"\b\s*[=:]\s*)([^\s&;,\"']+)"
)
SENSITIVE_JSON_PATTERN = re.compile(
    r'(?i)([\"\'](?:access[_-]?token|auth(?:orization)?|api[_-]?key|password|secret|token)'
    r'[\"\']\s*:\s*[\"\'])(.*?)([\"\'])'
)
SENSITIVE_DETAIL_KEYS = ("auth", "password", "secret", "token", "api_key", "apikey")


def log_root() -> Path:
    return state_dir() / "logs"


def hub_log_path() -> Path:
    return log_root() / "hub.jsonl"


def node_logs_dir() -> Path:
    return log_root() / "nodes"


def node_log_path(node_name: str) -> Path:
    return node_logs_dir() / f"{urllib.parse.quote(node_name, safe='._-')}.jsonl"


def node_outbox_path() -> Path:
    return state_dir() / "log-outbox" / "node.jsonl"


def log_cursors_path() -> Path:
    return log_root() / "cursors.json"


def append_hub_event(
    level: str,
    event: str,
    message: str,
    *,
    source: str = "hub",
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload = event_payload(level, event, message, source=source, details=details)
    with suppress(OSError, TypeError, ValueError):
        append_event(hub_log_path(), payload)
    return payload


def append_node_event(
    node_name: str,
    level: str,
    event: str,
    message: str,
    *,
    source: str = "hub",
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload = event_payload(level, event, message, source=source, details=details)
    with suppress(OSError, TypeError, ValueError):
        append_event(node_log_path(node_name), payload)
    return payload


def append_node_outbox_event(
    level: str,
    event: str,
    message: str,
    *,
    source: str = "node",
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload = event_payload(level, event, message, source=source, details=details)
    with suppress(OSError, TypeError, ValueError):
        append_event(
            node_outbox_path(),
            payload,
            max_bytes=NODE_OUTBOX_MAX_BYTES,
            backups=NODE_OUTBOX_BACKUPS,
        )
    return payload


def event_payload(
    level: str,
    event: str,
    message: str,
    *,
    source: str,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    normalized_level = str(level or "info").lower()
    if normalized_level not in LOG_LEVELS:
        normalized_level = "info"
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "id": f"{time.time_ns():020d}-{os.getpid():x}-{uuid.uuid4().hex[:8]}",
        "timestamp": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": normalized_level,
        "source": redact_log_text(source, max_chars=160),
        "event": redact_log_text(event, max_chars=160),
        "message": redact_log_text(message),
    }
    sanitized_details = sanitize_log_value(details or {})
    if sanitized_details:
        payload["details"] = sanitized_details
    return payload


def append_event(
    path: Path,
    payload: Mapping[str, object],
    *,
    max_bytes: int = LOG_FILE_MAX_BYTES,
    backups: int = LOG_FILE_BACKUPS,
) -> None:
    normalized = normalize_received_event(payload)
    line = (json.dumps(normalized, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with interprocess_path_lock(path):
        rotate_log_if_needed(path, len(line), max_bytes=max_bytes, backups=backups)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)


def normalize_received_event(payload: Mapping[str, object]) -> dict[str, object]:
    level = str(payload.get("level") or "info").lower()
    if level not in LOG_LEVELS:
        level = "info"
    timestamp = str(payload.get("timestamp") or "")
    if not timestamp:
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    event_id = str(payload.get("id") or "")
    if not event_id:
        event_id = f"{time.time_ns():020d}-{os.getpid():x}-{uuid.uuid4().hex[:8]}"
    normalized: dict[str, object] = {
        "id": redact_log_text(event_id, max_chars=160),
        "timestamp": redact_log_text(timestamp, max_chars=80),
        "level": level,
        "source": redact_log_text(str(payload.get("source") or "unknown"), max_chars=160),
        "event": redact_log_text(str(payload.get("event") or "log"), max_chars=160),
        "message": redact_log_text(str(payload.get("message") or "")),
    }
    details = sanitize_log_value(payload.get("details") or {})
    if details:
        normalized["details"] = details
    return normalized


def read_hub_events(
    *, limit: int = 200, level: str = "", query: str = ""
) -> list[dict[str, object]]:
    return read_events(hub_log_path(), limit=limit, level=level, query=query)


def read_node_events(
    node_name: str, *, limit: int = 200, level: str = "", query: str = ""
) -> list[dict[str, object]]:
    return read_events(node_log_path(node_name), limit=limit, level=level, query=query)


def read_events(
    path: Path,
    *,
    limit: int = 200,
    level: str = "",
    query: str = "",
    newest_first: bool = True,
    max_limit: int = 500,
) -> list[dict[str, object]]:
    limit = max(1, min(int(limit), max_limit))
    normalized_level = level.lower().strip()
    if normalized_level not in LOG_LEVELS:
        normalized_level = ""
    normalized_query = query.casefold().strip()[:200]
    scan_limit = min(max(limit * 10, 500), 5_000) if normalized_level or normalized_query else limit
    events: list[dict[str, object]] = []
    remaining = scan_limit
    for candidate in log_paths_newest_first(path):
        if remaining <= 0:
            break
        lines = tail_lines(candidate, remaining)
        remaining -= len(lines)
        parsed: list[dict[str, object]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(item, dict):
                parsed.append(normalize_received_event(item))
        events = parsed + events
    if normalized_level:
        events = [item for item in events if item.get("level") == normalized_level]
    if normalized_query:
        events = [item for item in events if event_matches_query(item, normalized_query)]
    events = events[-limit:]
    return list(reversed(events)) if newest_first else events


def node_outbox_payload(after: str = "", limit: int = 250) -> dict[str, object]:
    limit = max(1, min(int(limit), 500))
    events = read_events(
        node_outbox_path(),
        limit=5_000,
        newest_first=False,
        max_limit=5_000,
    )
    start = 0
    cursor_reset = False
    if after:
        matching_index = next(
            (index for index, item in enumerate(events) if str(item.get("id") or "") == after),
            None,
        )
        if matching_index is None:
            cursor_reset = True
            start = 0
        else:
            start = matching_index + 1
    selected = events[start : start + limit]
    next_cursor = str(selected[-1].get("id") or "") if selected else after
    return {
        "events": selected,
        "next_cursor": next_cursor,
        "has_more": start + len(selected) < len(events),
        "cursor_reset": cursor_reset,
    }


def node_ingest_cursor(node_name: str) -> str:
    path = log_cursors_path()
    with locked_file(path):
        data = read_json(path, {})
        record = cursor_record(data, node_name)
        return str(record.get("cursor") or "")


def ingest_node_events(node_name: str, events: Sequence[object]) -> int:
    normalized = [
        normalize_received_event(item)
        for item in events
        if isinstance(item, Mapping)
    ]
    if not normalized:
        return 0
    cursors_path = log_cursors_path()
    with locked_file(cursors_path):
        data = read_json(cursors_path, {})
        if not isinstance(data, dict):
            data = {}
        nodes = data.setdefault("nodes", {})
        if not isinstance(nodes, dict):
            nodes = {}
            data["nodes"] = nodes
        record = cursor_record(data, node_name)
        seen_list = [str(value) for value in record.get("seen", []) if value]
        seen = set(seen_list)
        ingested = 0
        for item in normalized:
            event_id = str(item.get("id") or "")
            if not event_id or event_id in seen:
                continue
            append_event(node_log_path(node_name), item)
            seen.add(event_id)
            seen_list.append(event_id)
            record["cursor"] = event_id
            ingested += 1
        record["seen"] = seen_list[-MAX_INGESTED_EVENT_IDS:]
        nodes[node_name] = record
        atomic_write_json(cursors_path, data)
        return ingested


def archived_node_names() -> list[str]:
    directory = node_logs_dir()
    try:
        paths = directory.glob("*.jsonl")
    except OSError:
        return []
    return sorted(
        {
            urllib.parse.unquote(path.name.removesuffix(".jsonl"))
            for path in paths
            if path.is_file()
        }
    )


def infer_process_output_level(line: str) -> str:
    upper = line.upper()
    if "CRITICAL" in upper:
        return "critical"
    if "TRACEBACK" in upper or "ERROR" in upper or "EXCEPTION" in upper:
        return "error"
    if "WARNING" in upper or "WARN" in upper:
        return "warning"
    if "DEBUG" in upper:
        return "debug"
    return "info"


def redact_log_text(value: object, *, max_chars: int = MAX_EVENT_MESSAGE_CHARS) -> str:
    text = ANSI_PATTERN.sub("", str(value or ""))
    text = BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = URL_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]@", text)
    text = SENSITIVE_PAIR_PATTERN.sub(r"\1[REDACTED]", text)
    text = SENSITIVE_JSON_PATTERN.sub(r"\1[REDACTED]\3", text)
    if len(text) > max_chars:
        return f"{text[:max_chars]}…"
    return text


def sanitize_log_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if any(fragment in key.casefold() for fragment in SENSITIVE_DETAIL_KEYS):
        return "[REDACTED]"
    if depth >= 5:
        return "[truncated]"
    if isinstance(value, Mapping):
        return {
            redact_log_text(item_key, max_chars=160): sanitize_log_value(
                item_value, key=str(item_key), depth=depth + 1
            )
            for item_key, item_value in list(value.items())[:MAX_EVENT_DETAIL_ITEMS]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            sanitize_log_value(item, depth=depth + 1)
            for item in list(value)[:MAX_EVENT_DETAIL_ITEMS]
        ]
    if isinstance(value, str):
        return redact_log_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_log_text(value)


def event_matches_query(item: Mapping[str, object], query: str) -> bool:
    haystack = " ".join(
        (
            str(item.get("source") or ""),
            str(item.get("event") or ""),
            str(item.get("message") or ""),
            json.dumps(item.get("details") or {}, ensure_ascii=False),
        )
    ).casefold()
    return query in haystack


def cursor_record(data: object, node_name: str) -> dict[str, object]:
    if not isinstance(data, dict):
        return {}
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        return {}
    record = nodes.get(node_name)
    return dict(record) if isinstance(record, dict) else {}


def log_paths_newest_first(path: Path) -> Iterator[Path]:
    if path.exists():
        yield path
    for index in range(1, LOG_FILE_BACKUPS + 1):
        candidate = path.with_name(f"{path.name}.{index}")
        if candidate.exists():
            yield candidate


def tail_lines(path: Path, limit: int) -> list[str]:
    if limit <= 0:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            chunks: list[bytes] = []
            line_count = 0
            while position > 0 and line_count <= limit:
                chunk_size = min(64 * 1024, position)
                position -= chunk_size
                handle.seek(position)
                chunk = handle.read(chunk_size)
                chunks.append(chunk)
                line_count += chunk.count(b"\n")
    except OSError:
        return []
    data = b"".join(reversed(chunks))
    return [line.decode("utf-8", errors="replace") for line in data.splitlines()[-limit:]]


def rotate_log_if_needed(path: Path, incoming_bytes: int, *, max_bytes: int, backups: int) -> None:
    try:
        current_size = path.stat().st_size
    except OSError:
        current_size = 0
    if current_size == 0 or current_size + incoming_bytes <= max_bytes:
        return
    for index in range(backups, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        destination = path.with_name(f"{path.name}.{index}")
        if source.exists():
            os.replace(source, destination)


@contextmanager
def interprocess_path_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(lock_path), lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
