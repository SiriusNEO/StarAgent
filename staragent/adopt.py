from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from staragent.paths import state_dir
from staragent.state import atomic_write_json, locked_file, read_json

ADOPTIONS_PATH = state_dir() / "adopted_sessions.json"


@dataclass(frozen=True)
class AdoptedSession:
    name: str
    target: str
    cli: str
    cwd: str
    pane_pid: int = 0
    cli_pid: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "target": self.target,
            "cli": self.cli,
            "cwd": self.cwd,
            "pane_pid": self.pane_pid,
            "cli_pid": self.cli_pid,
        }


def load_adoptions() -> dict[str, AdoptedSession]:
    with locked_file(ADOPTIONS_PATH):
        return _load_adoptions_unlocked()


def _load_adoptions_unlocked() -> dict[str, AdoptedSession]:
    if not ADOPTIONS_PATH.exists():
        return {}
    data = read_json(ADOPTIONS_PATH, {})
    entries = data.get("sessions", data)
    if not isinstance(entries, dict):
        return {}
    result: dict[str, AdoptedSession] = {}
    for name, raw in entries.items():
        if not isinstance(raw, dict):
            continue
        adopted = AdoptedSession(
            name=str(raw.get("name") or name),
            target=str(raw.get("target") or name),
            cli=str(raw.get("cli") or "unknown"),
            cwd=str(raw.get("cwd") or ""),
            pane_pid=safe_int(raw.get("pane_pid")),
            cli_pid=safe_int(raw.get("cli_pid")),
        )
        result[adopted.name] = adopted
    return result


def _save_adoptions_unlocked(adoptions: dict[str, AdoptedSession]) -> None:
    payload = {"sessions": {name: item.as_dict() for name, item in sorted(adoptions.items())}}
    atomic_write_json(ADOPTIONS_PATH, payload)


def adopt_existing_session(name: str) -> AdoptedSession:
    candidates = {item.name: item for item in discover_adoptable_sessions()}
    if name not in candidates:
        raise ValueError(f"no adoptable tmux session found: {name}")
    with locked_file(ADOPTIONS_PATH):
        adoptions = _load_adoptions_unlocked()
        adoptions[name] = candidates[name]
        _save_adoptions_unlocked(adoptions)
    return candidates[name]


def adopted_session(name: str) -> AdoptedSession | None:
    return load_adoptions().get(name)


def discover_adoptable_sessions() -> list[AdoptedSession]:
    rows = list_tmux_panes()
    results: dict[str, AdoptedSession] = {}
    for row in rows:
        session_name = str(row.get("session_name") or "")
        if not session_name or session_name == "staragent-hub":
            continue
        pane_pid = safe_int(row.get("pane_pid"))
        command = str(row.get("current_command") or "")
        cli, cli_pid = infer_cli_from_pane(command, pane_pid)
        if cli == "unknown":
            continue
        cwd = str(row.get("current_path") or read_process_cwd(cli_pid) or "")
        if session_name not in results:
            results[session_name] = AdoptedSession(
                name=session_name,
                target=session_name,
                cli=cli,
                cwd=cwd,
                pane_pid=pane_pid,
                cli_pid=cli_pid,
            )
    return sorted(results.values(), key=lambda item: item.name)


def list_tmux_panes() -> list[dict[str, object]]:
    command = [
        "tmux",
        "list-panes",
        "-a",
        "-F",
        "#{session_name}\t#{window_index}.#{pane_index}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}",
    ]
    try:
        result = subprocess.run(command, check=False, text=True, capture_output=True)
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split("\t", 4)
        if len(fields) != 5:
            continue
        session_name, pane_index, pane_pid, current_command, current_path = fields
        rows.append(
            {
                "session_name": session_name,
                "pane_index": pane_index,
                "pane_pid": safe_int(pane_pid),
                "current_command": current_command,
                "current_path": current_path,
            }
        )
    return rows


def infer_cli_from_pane(
    current_command: str,
    pane_pid: int,
    process_tree: dict[int, list[tuple[int, str]]] | None = None,
) -> tuple[str, int]:
    direct = normalize_cli_name(current_command)
    if direct != "unknown":
        return direct, pane_pid
    candidates = (
        descendants(pane_pid, max_depth=3)
        if process_tree is None
        else descendants_from_process_tree(pane_pid, process_tree, max_depth=3)
    )
    for pid, command in candidates:
        cli = normalize_cli_name(command)
        if cli != "unknown":
            return cli, pid
    return "unknown", 0


def normalize_cli_name(command: str) -> str:
    base = Path(command).name.lower()
    if base in {"codex", "codex-cli"}:
        return "codex"
    if base in {"claude", "claude-code"}:
        return "claude"
    if base == "gemini":
        return "gemini"
    if base == "opencode":
        return "opencode"
    return "unknown"


def descendants(root_pid: int, max_depth: int = 3) -> list[tuple[int, str]]:
    if root_pid <= 0:
        return []
    return descendants_from_process_tree(root_pid, process_children(), max_depth=max_depth)


def process_children() -> dict[int, list[tuple[int, str]]]:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm="],
            check=False,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return {}
    children: dict[int, list[tuple[int, str]]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid = safe_int(parts[0])
        ppid = safe_int(parts[1])
        command = parts[2]
        children.setdefault(ppid, []).append((pid, command))
    return children


def descendants_from_process_tree(
    root_pid: int,
    children: dict[int, list[tuple[int, str]]],
    max_depth: int = 3,
) -> list[tuple[int, str]]:
    if root_pid <= 0:
        return []

    found: list[tuple[int, str]] = []
    frontier = [(root_pid, 0)]
    while frontier:
        pid, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for child_pid, command in children.get(pid, []):
            found.append((child_pid, command))
            frontier.append((child_pid, depth + 1))
    return found


def read_process_cwd(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        return str(Path(f"/proc/{pid}/cwd").resolve())
    except OSError:
        return ""


def safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
