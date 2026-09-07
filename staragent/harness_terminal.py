from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from staragent.agent_models import apply_harness_model_preference
from staragent.agent_tools import agent_tool_spec
from staragent.harness_config import harness_process_environment
from staragent.pty_terminal import PtyTerminal
from staragent.runtime import worker_shell_script

if os.name != "nt":
    import pwd


@dataclass(frozen=True)
class HarnessAuthFlow:
    method: str
    action: str
    transport: str
    args: tuple[str, ...] = ()


HARNESS_AUTH_FLOWS = {
    "codex": (
        HarnessAuthFlow("browser", "login", "terminal", ("login",)),
        HarnessAuthFlow("device", "login", "terminal", ("login", "--device-auth")),
        HarnessAuthFlow("api-key", "login", "api_key"),
        HarnessAuthFlow("environment", "configure", "environment"),
    ),
    "claude": (
        HarnessAuthFlow("account", "login", "terminal", ("auth", "login", "--claudeai")),
        HarnessAuthFlow("console", "login", "terminal", ("auth", "login", "--console")),
        HarnessAuthFlow("sso", "login", "terminal", ("auth", "login", "--sso")),
        HarnessAuthFlow("environment", "configure", "environment"),
    ),
    "opencode": (
        HarnessAuthFlow("provider", "login", "terminal", ("auth", "login")),
        HarnessAuthFlow("remove-provider", "logout", "terminal", ("auth", "logout")),
        HarnessAuthFlow("environment", "configure", "environment"),
    ),
}


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
    command = apply_harness_model_preference(spec.command)
    if os.name == "nt":
        from staragent.native_sessions import windows_shell_argv

        return windows_shell_argv(command, keep_open=True)
    bash = shutil.which("bash")
    if not bash:
        raise OSError("Bash is required to start an Agent Harness terminal.")
    return [bash, "-lc", worker_shell_script(command)]


def harness_auth_flows(name: str) -> tuple[HarnessAuthFlow, ...]:
    flows = HARNESS_AUTH_FLOWS.get(name)
    if flows is None:
        raise ValueError(f"Authentication is not supported for Agent CLI: {name}")
    return flows


def default_harness_auth_method(name: str, *, local: bool) -> str:
    if name == "codex":
        return "browser" if local else "device"
    return harness_auth_flows(name)[0].method


def harness_auth_flow(name: str, action: str, method: str) -> HarnessAuthFlow:
    flow = next(
        (
            candidate
            for candidate in harness_auth_flows(name)
            if candidate.action == action and candidate.method == method
        ),
        None,
    )
    if flow is None:
        raise ValueError(f"Unsupported {name} authentication flow: {action}/{method}")
    return flow


def harness_auth_argv(name: str, action: str, method: str) -> list[str]:
    flow = harness_auth_flow(name, action, method)
    if flow.transport != "terminal":
        raise ValueError(f"Authentication flow does not use a terminal: {name}/{method}")
    spec = agent_tool_spec(name)
    environment = harness_process_environment(name)
    executable = shutil.which(spec.command, path=environment.get("PATH")) if spec else None
    if not executable:
        raise OSError(f"{spec.label if spec else name} is not installed in the Node service PATH.")
    return [executable, *flow.args]


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
def open_harness_auth_terminal(
    name: str,
    *,
    action: str,
    method: str,
    cols: int = 100,
    rows: int = 24,
) -> Iterator[PtyTerminal]:
    terminal = PtyTerminal.spawn(
        harness_auth_argv(name, action, method),
        cwd=shell_working_directory(),
        env=harness_shell_environment(name),
        cols=cols,
        rows=rows,
    )
    try:
        yield terminal
    finally:
        terminal.close()
