from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x0800_0000)
_IS_WINDOWS = os.name == "nt"


def background_process_kwargs() -> dict[str, int]:
    """Keep captured background commands from flashing a Windows console."""
    return {"creationflags": _CREATE_NO_WINDOW} if _IS_WINDOWS else {}


def augmented_windows_path(environment: Mapping[str, str] | None = None) -> str:
    source = dict(os.environ if environment is None else environment)
    entries = [item for item in source.get("PATH", "").split(os.pathsep) if item]
    home = Path(source.get("USERPROFILE") or Path.home())
    app_data = source.get("APPDATA", "")
    local_app_data = source.get("LOCALAPPDATA", "")
    program_files = source.get("PROGRAMFILES", "") or source.get("ProgramFiles", "")

    candidates = (
        Path(app_data) / "npm" if app_data else None,
        Path(local_app_data) / "Programs" / "OpenAI" / "Codex" / "bin" if local_app_data else None,
        Path(local_app_data) / "Microsoft" / "WindowsApps" if local_app_data else None,
        Path(local_app_data) / "Microsoft" / "WinGet" / "Links" if local_app_data else None,
        Path(program_files) / "nodejs" if program_files else None,
        Path(program_files) / "Tailscale" if program_files else None,
        home / ".local" / "bin",
        home / ".claude" / "local",
        home / ".opencode" / "bin",
        home / ".bun" / "bin",
        home / "scoop" / "shims",
    )
    ordered = [*entries, *(str(path) for path in candidates if path is not None)]
    unique: list[str] = []
    seen: set[str] = set()
    for value in ordered:
        key = os.path.normcase(os.path.normpath(value))
        if key not in seen:
            unique.append(value)
            seen.add(key)
    return os.pathsep.join(unique)


def windows_process_argv(
    argv: Sequence[str],
    environment: Mapping[str, str],
    *,
    require_executable: bool = False,
) -> list[str]:
    command = list(argv)
    if not command:
        raise ValueError("Process command must not be empty.")
    executable = shutil.which(command[0], path=environment.get("PATH"))
    if executable is None:
        if require_executable:
            raise FileNotFoundError(f"Required command not found: {command[0]}")
        executable = command[0]
    if Path(executable).suffix.lower() not in {".bat", ".cmd"}:
        command[0] = executable
        return command
    command_line = subprocess.list2cmdline([executable, *command[1:]])
    command_prompt = environment.get("COMSPEC") or shutil.which(
        "cmd.exe",
        path=environment.get("PATH"),
    )
    if not command_prompt:
        raise FileNotFoundError("Required Windows command processor not found: cmd.exe")
    return [command_prompt, "/d", "/s", "/c", command_line]
