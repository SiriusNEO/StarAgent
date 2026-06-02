from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from staragent.adopt import adopted_session, infer_cli_from_pane
from staragent.models import SessionStatus

ATTENTION_PATTERNS = (
    re.compile(r"\b(continue|proceed)\?", re.IGNORECASE),
    re.compile(r"\b(y/n|yes/no)\b", re.IGNORECASE),
    re.compile(r"needs? (input|attention|decision)", re.IGNORECASE),
    re.compile(r"what do you want to do", re.IGNORECASE),
    re.compile(r"是否|需要.*决策|请选择"),
)

SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
SYSTEM_SESSION_NAMES = {"staragent-hub", "staragent-node", "staragent-tailscaled"}


def tmux_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    return env


def run_tmux(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], env=tmux_env(), **kwargs)


def discover_local_tmux_statuses(lines: int = 80) -> dict[str, SessionStatus]:
    sessions = list_tmux_sessions()
    if not sessions:
        return {}

    node = socket.gethostname()
    statuses: dict[str, SessionStatus] = {}
    for session in sessions:
        pane = tmux_active_pane(session["name"])
        output = capture_tmux_pane(session["name"], lines=lines)
        current_command = str(pane.get("current_command") or "")
        adopted = adopted_session(session["name"])
        detected_cli, _ = infer_cli_from_pane(current_command, int(pane.get("pane_pid") or 0))
        session_type = infer_session_type(session["name"], output, current_command)
        needs_attention = looks_like_attention(output)
        status = classify_tmux_status(session, needs_attention)
        updated = datetime.fromtimestamp(session["activity"], tz=UTC).astimezone()
        current_path = (
            adopted.cwd if adopted and adopted.cwd else str(pane.get("current_path") or "")
        )
        branch = git_branch(current_path)
        changed_files = git_changed_files(current_path)
        statuses[session["name"]] = SessionStatus.from_dict(
            {
                "name": session["name"],
                "agent": adopted.cli
                if adopted and adopted.cli
                else infer_agent(session["name"], current_command, detected_cli),
                "node": node,
                "repo": current_path,
                "branch": branch,
                "task": f"Adopted {adopted.cli} tmux session"
                if adopted
                else tmux_task(session, pane),
                "status": status,
                "summary": tmux_summary(session, pane, output),
                "next_step": "",
                "needs_attention": needs_attention,
                "question": attention_line(output) if needs_attention else "",
                "changed_files": changed_files,
                "recent_output": output,
                "source": "adopted" if adopted else "tmux",
                "session_type": session_type,
                "last_updated": updated.isoformat(),
            }
        )
    return statuses


def list_tmux_sessions() -> list[dict[str, int | str]]:
    command = [
        "tmux",
        "list-sessions",
        "-F",
        "#{session_name}\t#{session_windows}\t#{session_attached}\t#{session_activity}\t#{session_created}",
    ]
    try:
        result = run_tmux(command[1:], check=False, text=True, capture_output=True)
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []

    sessions = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 5:
            continue
        name, windows, attached, activity, created = fields
        sessions.append(
            {
                "name": name,
                "windows": safe_int(windows),
                "attached": safe_int(attached),
                "activity": safe_int(activity),
                "created": safe_int(created),
            }
        )
    return sessions


def capture_tmux_pane(session: str, lines: int = 80) -> str:
    command = ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"]
    result = run_tmux(command[1:], check=False, text=True, capture_output=True)
    if result.returncode != 0:
        return ""
    return strip_ansi(result.stdout).strip()


def capture_tmux_pane_ansi(session: str, lines: int = 80) -> str:
    command = ["tmux", "capture-pane", "-t", session, "-p", "-e", "-S", f"-{lines}"]
    result = run_tmux(command[1:], check=False, text=True, capture_output=True)
    if result.returncode != 0:
        return ""
    return result.stdout.rstrip()


def tmux_session_exists(session: str) -> bool:
    try:
        result = run_tmux(["has-session", "-t", session], check=False, capture_output=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def send_tmux_message(session: str, text: str) -> None:
    if not text.strip():
        raise ValueError("Message is empty")
    if not tmux_session_exists(session):
        raise ValueError(f"tmux session not found: {session}")

    result = run_tmux(
        ["send-keys", "-t", session, "-l", text],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "tmux send-keys failed"
        raise RuntimeError(detail)
    time.sleep(0.08)
    result = run_tmux(
        ["send-keys", "-t", session, "C-m"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "tmux enter failed"
        raise RuntimeError(detail)


def send_tmux_input(session: str, data: str) -> None:
    if not data:
        return
    if not tmux_session_exists(session):
        raise ValueError(f"tmux session not found: {session}")

    literal: list[str] = []
    for token in terminal_input_tokens(data):
        if token.startswith("literal:"):
            literal.append(token.removeprefix("literal:"))
            continue
        flush_literal(session, literal)
        send_tmux_key(session, token)
    flush_literal(session, literal)


def terminal_input_tokens(data: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    special = {
        "\r": "Enter",
        "\n": "Enter",
        "\x7f": "BSpace",
        "\b": "BSpace",
        "\t": "Tab",
        "\x03": "C-c",
    }
    escape_sequences = {
        "\x1b[A": "Up",
        "\x1b[B": "Down",
        "\x1b[C": "Right",
        "\x1b[D": "Left",
        "\x1b[3~": "Delete",
        "\x1b[H": "Home",
        "\x1b[F": "End",
    }
    while index < len(data):
        matched = False
        for sequence, key in escape_sequences.items():
            if data.startswith(sequence, index):
                tokens.append(key)
                index += len(sequence)
                matched = True
                break
        if matched:
            continue
        char = data[index]
        tokens.append(special.get(char, f"literal:{char}"))
        index += 1
    return tokens


def flush_literal(session: str, literal: list[str]) -> None:
    if not literal:
        return
    text = "".join(literal)
    literal.clear()
    result = run_tmux(
        ["send-keys", "-t", session, "-l", text],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "tmux send-keys literal failed"
        raise RuntimeError(detail)


def send_tmux_key(session: str, key: str) -> None:
    result = run_tmux(
        ["send-keys", "-t", session, key],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"tmux send-keys {key} failed"
        raise RuntimeError(detail)


def start_tmux_worker(name: str, cwd: str, command: str) -> None:
    name = name.strip()
    command = command.strip()
    cwd_path = Path(cwd).expanduser().resolve()
    if not SESSION_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Worker name may only contain letters, numbers, dot, underscore, colon, or dash"
        )
    if not cwd_path.is_dir():
        raise ValueError(f"Working directory does not exist: {cwd}")
    if not command:
        raise ValueError("Command is empty")
    if tmux_session_exists(name):
        raise ValueError(f"tmux session already exists: {name}")

    result = run_tmux(
        ["new-session", "-d", "-s", name, "-c", str(cwd_path), command],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "tmux new-session failed"
        raise RuntimeError(detail)


def ensure_tmux_session(name: str, cwd: str, command: str) -> None:
    name = name.strip()
    command = command.strip()
    cwd_path = Path(cwd).expanduser().resolve()
    if not SESSION_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Session name may only contain letters, numbers, dot, underscore, colon, or dash"
        )
    if not cwd_path.is_dir():
        raise ValueError(f"Working directory does not exist: {cwd}")
    if not command:
        raise ValueError("Command is empty")
    if tmux_session_exists(name):
        return
    result = run_tmux(
        ["new-session", "-d", "-s", name, "-c", str(cwd_path), command],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "tmux new-session failed"
        raise RuntimeError(detail)


def wait_for_tmux_session(name: str, interval: float = 2.0) -> None:
    while tmux_session_exists(name):
        time.sleep(interval)


def kill_tmux_session(session: str) -> None:
    if not tmux_session_exists(session):
        raise ValueError(f"tmux session not found: {session}")
    result = run_tmux(
        ["kill-session", "-t", session],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "tmux kill-session failed"
        raise RuntimeError(detail)


def kill_tmux_session_if_exists(session: str) -> None:
    if not tmux_session_exists(session):
        return
    kill_tmux_session(session)


def tmux_active_pane(session: str) -> dict[str, str | int]:
    command = [
        "tmux",
        "display-message",
        "-p",
        "-t",
        session,
        "#{pane_current_command}\t#{pane_current_path}\t#{pane_pid}\t#{window_name}",
    ]
    result = run_tmux(command[1:], check=False, text=True, capture_output=True)
    if result.returncode != 0:
        return {}
    fields = result.stdout.rstrip("\n").split("\t")
    if len(fields) != 4:
        return {}
    current_command, current_path, pane_pid, window_name = fields
    return {
        "current_command": current_command,
        "current_path": current_path,
        "pane_pid": safe_int(pane_pid),
        "window_name": window_name,
    }


def classify_tmux_status(session: dict[str, int | str], needs_attention: bool) -> str:
    if needs_attention:
        return "attention"
    if int(session["attached"]):
        return "attached"
    activity = datetime.fromtimestamp(int(session["activity"]), tz=UTC)
    age = datetime.now(UTC) - activity
    if age.total_seconds() < 15 * 60:
        return "active"
    return "idle"


def infer_agent(name: str, current_command: str = "", detected_cli: str = "") -> str:
    if is_staragent_system_session(name, "", current_command):
        return name
    if detected_cli and detected_cli != "unknown":
        return detected_cli
    direct_cli = infer_cli_from_pane(current_command, 0)[0]
    if direct_cli != "unknown":
        return direct_cli
    return "unknown"


def infer_session_type(name: str, output: str, current_command: str = "") -> str:
    if is_staragent_system_session(name, output, current_command):
        return "system"
    return "agent"


def is_staragent_system_session(name: str, output: str = "", current_command: str = "") -> bool:
    return name in SYSTEM_SESSION_NAMES


def tmux_task(session: dict[str, int | str], pane: dict[str, str | int]) -> str:
    name = str(session["name"])
    if name == "staragent-hub":
        return "StarAgent hub dashboard"
    if name == "staragent-node":
        return "StarAgent remote node"
    if name == "staragent-tailscaled":
        return "Tailscale userspace daemon"
    command = pane.get("current_command")
    path = pane.get("current_path")
    if command and path:
        return f"{command} in {Path(str(path)).name or path}"
    if command:
        return str(command)
    return f"tmux session {session['name']}"


def tmux_summary(session: dict[str, int | str], pane: dict[str, str | int], output: str) -> str:
    last_line = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
    command = pane.get("current_command") or "unknown"
    pid = pane.get("pane_pid") or "-"
    base = f"{session['windows']} window(s), attached={session['attached']}, command={command}, pane_pid={pid}."
    return f"{base} Last output: {last_line}" if last_line else base


def git_branch(path: str) -> str:
    if not path:
        return ""
    result = subprocess.run(
        ["git", "-C", path, "branch", "--show-current"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_changed_files(path: str) -> list[str]:
    if not path:
        return []
    try:
        result = subprocess.run(
            ["git", "-C", path, "status", "--short"],
            check=False,
            text=True,
            capture_output=True,
            timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []
    files = []
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        files.append(line)
    return files


def looks_like_attention(output: str) -> bool:
    tail = "\n".join(output.splitlines()[-20:])
    return any(pattern.search(tail) for pattern in ATTENTION_PATTERNS)


def attention_line(output: str) -> str:
    for line in reversed(output.splitlines()[-20:]):
        if any(pattern.search(line) for pattern in ATTENTION_PATTERNS):
            return line.strip()
    return ""


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


def safe_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0
