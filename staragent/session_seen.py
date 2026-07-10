from __future__ import annotations

from pathlib import Path

from staragent.paths import state_dir
from staragent.state import atomic_write_json, locked_file, read_json

SESSION_SEEN_PATH = state_dir() / "session_seen.json"
MAX_SEEN_COMPLETIONS = 1000
_SEEN_CACHE: dict[str, str] | None = None
_SEEN_CACHE_PATH: Path | None = None


def completion_seen(node: str, session: str, revision: str) -> bool:
    if not revision:
        return False
    with locked_file(SESSION_SEEN_PATH):
        return _seen_completions_unlocked().get(session_key(node, session)) == revision


def mark_completion_seen(node: str, session: str, revision: str) -> None:
    if not revision:
        return
    with locked_file(SESSION_SEEN_PATH):
        seen = _seen_completions_unlocked()
        key = session_key(node, session)
        seen.pop(key, None)
        seen[key] = revision
        while len(seen) > MAX_SEEN_COMPLETIONS:
            seen.pop(next(iter(seen)), None)
        atomic_write_json(SESSION_SEEN_PATH, {"seen": seen})


def session_key(node: str, session: str) -> str:
    return f"{node}/{session}"


def _seen_completions_unlocked() -> dict[str, str]:
    global _SEEN_CACHE, _SEEN_CACHE_PATH
    if _SEEN_CACHE is not None and _SEEN_CACHE_PATH == SESSION_SEEN_PATH:
        return _SEEN_CACHE
    payload = read_json(SESSION_SEEN_PATH, {})
    values = payload.get("seen", {}) if isinstance(payload, dict) else {}
    if not isinstance(values, dict):
        values = {}
    _SEEN_CACHE = {
        str(key): str(value)
        for key, value in values.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }
    _SEEN_CACHE_PATH = SESSION_SEEN_PATH
    return _SEEN_CACHE
