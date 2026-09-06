from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from staragent.agent_tools import agent_tool_spec
from staragent.harness_config import harness_process_environment
from staragent.pty_terminal import PtyTerminal
from staragent.runtime import worker_shell_script

if os.name != "nt":
    import pwd


def login_shell() -> str:
    if os.name == "nt":
        from staragent.native_sessions import windows_shell_executable

        return windows_shell_executable()
    candidates = [os.environ.get("SHELL", "")]
    with contextlib.suppress(KeyError):
        candidates.append(pwd.getpwuid(os.getuid()).pw_shell)
    candidates.extend(("bash", "sh"))
    for candidate in candidates:
        executable = shutil.which(candidate) if candidate else None
        if executable:
            return executable
    raise OSError("No interactive shell is available on this Node.")


def shell_working_directory() -> str:
    home = Path.home()
    return str(home if home.is_dir() else Path.cwd())


def harness_shell_environment(name: str) -> dict[str, str]:
    spec = agent_tool_spec(name)
    if spec is None:
        raise ValueError(f"Unsupported Agent CLI: {name}")
    environment = harness_process_environment(spec.name)
    # Editor shell integration can leave a PROMPT_COMMAND that references
    # functions unavailable in this independent PTY.
    environment.pop("PROMPT_COMMAND", None)
    environment["SHELL"] = login_shell()
    environment["STARAGENT_HARNESS"] = spec.name
    environment["STARAGENT_HARNESS_COMMAND"] = spec.command
    return environment


def harness_terminal_argv(name: str) -> list[str]:
    spec = agent_tool_spec(name)
    if spec is None:
        raise ValueError(f"Unsupported Agent CLI: {name}")
    if os.name == "nt":
        from staragent.native_sessions import windows_shell_argv

        return windows_shell_argv(spec.command, keep_open=True)
    bash = shutil.which("bash")
    if not bash:
        raise OSError("Bash is required to start an Agent Harness terminal.")
    return [bash, "-lc", worker_shell_script(spec.command)]


def codex_login_argv() -> list[str]:
    spec = agent_tool_spec("codex")
    executable = shutil.which(spec.command) if spec else None
    if not executable:
        raise OSError("Codex is not installed in the Node service PATH.")
    return [executable, "login", "--device-auth"]


@contextmanager
def open_harness_terminal(
    name: str,
    *,
    cols: int = 120,
    rows: int = 30,
) -> Iterator[PtyTerminal]:
    spec = agent_tool_spec(name)
    if spec is None:
        raise ValueError(f"Unsupported Agent CLI: {name}")
    terminal = PtyTerminal.spawn(
        harness_terminal_argv(spec.name),
        cwd=shell_working_directory(),
        env=harness_shell_environment(spec.name),
        cols=cols,
        rows=rows,
    )
    try:
        yield terminal
    finally:
        terminal.close()


@contextmanager
def open_codex_login_terminal(
    *,
    cols: int = 100,
    rows: int = 24,
) -> Iterator[PtyTerminal]:
    terminal = PtyTerminal.spawn(
        codex_login_argv(),
        cwd=shell_working_directory(),
        env=harness_shell_environment("codex"),
        cols=cols,
        rows=rows,
    )
    try:
        yield terminal
    finally:
        terminal.close()
