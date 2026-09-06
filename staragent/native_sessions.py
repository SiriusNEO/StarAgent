from __future__ import annotations

import asyncio
import base64
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_SCROLLBACK_BYTES = 4 * 1024 * 1024
MAX_ATTACH_SNAPSHOT_BYTES = 1024 * 1024
SUBSCRIBER_QUEUE_CHUNKS = 512
SHELL_ALIASES = {"bash", "bash.exe", "sh", "sh.exe", "powershell", "powershell.exe", "pwsh"}


def native_session_backend_available() -> bool:
    if os.name != "nt":
        return False
    try:
        from winpty import Backend, PtyProcess  # noqa: F401
    except ImportError:
        return False
    return True


def windows_shell_executable() -> str:
    for candidate in ("pwsh.exe", "powershell.exe", "cmd.exe"):
        executable = shutil.which(candidate)
        if executable:
            return executable
    raise OSError("PowerShell or Command Prompt is required for native Windows sessions.")


def windows_shell_argv(command: str = "", *, keep_open: bool = True) -> list[str]:
    shell = windows_shell_executable()
    normalized = command.strip()
    if Path(shell).name.lower() == "cmd.exe":
        if not normalized or normalized.lower() in SHELL_ALIASES:
            return [shell, "/d"]
        return [shell, "/d", "/k" if keep_open else "/c", normalized]

    argv = [shell, "-NoLogo"]
    if not normalized or normalized.lower() in SHELL_ALIASES:
        return argv
    script = normalized
    if keep_open:
        script = "\n".join(
            (
                normalized,
                "$starAgentExit = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }",
                'Write-Host "`n[StarAgent] agent exited with status $starAgentExit. '
                'Dropping into PowerShell."',
            )
        )
        argv.append("-NoExit")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [*argv, "-EncodedCommand", encoded]


def augmented_windows_path(environment: Mapping[str, str] | None = None) -> str:
    source = dict(os.environ if environment is None else environment)
    entries = [item for item in source.get("PATH", "").split(os.pathsep) if item]
    home = Path(source.get("USERPROFILE") or Path.home())
    app_data = source.get("APPDATA", "")
    local_app_data = source.get("LOCALAPPDATA", "")
    program_files = source.get("ProgramFiles", "")
    candidates = (
        Path(app_data) / "npm" if app_data else None,
        Path(local_app_data) / "Microsoft" / "WindowsApps" if local_app_data else None,
        Path(program_files) / "nodejs" if program_files else None,
        home / ".local" / "bin",
        home / ".claude" / "local",
        home / ".opencode" / "bin",
        home / ".bun" / "bin",
        home / "scoop" / "shims",
    )
    seen = {os.path.normcase(os.path.normpath(item)) for item in entries}
    for candidate in candidates:
        if candidate is None:
            continue
        value = str(candidate)
        key = os.path.normcase(os.path.normpath(value))
        if key not in seen:
            entries.append(value)
            seen.add(key)
    return os.pathsep.join(entries)


def windows_pty_argv(argv: Sequence[str], environment: Mapping[str, str]) -> list[str]:
    command = list(argv)
    if not command:
        raise ValueError("PTY command must not be empty.")
    executable = shutil.which(command[0], path=environment.get("PATH")) or command[0]
    if Path(executable).suffix.lower() not in {".bat", ".cmd"}:
        command[0] = executable
        return command
    command_line = subprocess.list2cmdline([executable, *command[1:]])
    command_prompt = (
        environment.get("COMSPEC")
        or shutil.which("cmd.exe", path=environment.get("PATH"))
        or "cmd.exe"
    )
    return [command_prompt, "/d", "/s", "/c", command_line]


def spawn_conpty_process(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: Mapping[str, str],
    dimensions: tuple[int, int],
) -> Any:
    try:
        from winpty import Backend, PtyProcess, WinptyError
    except ImportError as exc:
        raise RuntimeError(
            "The bundled Windows ConPTY component is unavailable. Reinstall StarAgent Desktop."
        ) from exc
    try:
        return PtyProcess.spawn(
            list(argv),
            cwd=cwd,
            env=dict(env),
            dimensions=dimensions,
            # pywinpty 3.x treats the integer value 0 as "unspecified" before
            # parsing its backend option. Its environment-compatible string form
            # is truthy and is then converted back to Backend.ConPTY (0).
            backend=str(Backend.ConPTY),
        )
    except (EOFError, FileNotFoundError, OSError, WinptyError) as exc:
        raise OSError(f"Could not start the Windows ConPTY command: {exc}") from exc


ProcessFactory = Callable[..., Any]


@dataclass(eq=False)
class NativeSession:
    name: str
    cwd: str
    command: str
    agent: str
    process: Any
    created: int
    activity: int
    _output: bytearray = field(default_factory=bytearray, repr=False)
    _subscribers: set[queue.Queue[bytes | None]] = field(default_factory=set, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def pid(self) -> int:
        try:
            return int(self.process.pid or 0)
        except (AttributeError, TypeError, ValueError):
            return 0

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def is_alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.process.isalive())
        except (AttributeError, OSError):
            return False

    def append_output(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self.activity = int(time.time())
            self._output.extend(data)
            overflow = len(self._output) - MAX_SCROLLBACK_BYTES
            if overflow > 0:
                del self._output[:overflow]
            for subscriber in tuple(self._subscribers):
                _offer(subscriber, data)

    def snapshot(self, lines: int | None = None) -> bytes:
        with self._lock:
            value = bytes(self._output)
        if lines is None:
            return value
        return b"\n".join(value.splitlines()[-max(1, int(lines)) :])

    def subscribe(self) -> NativeSessionAttachment:
        subscriber: queue.Queue[bytes | None] = queue.Queue(SUBSCRIBER_QUEUE_CHUNKS)
        with self._lock:
            snapshot = bytes(self._output[-MAX_ATTACH_SNAPSHOT_BYTES:])
            if snapshot:
                _offer(subscriber, snapshot)
            self._subscribers.add(subscriber)
        return NativeSessionAttachment(session=self, subscriber=subscriber)

    def unsubscribe(self, subscriber: queue.Queue[bytes | None]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)
        _offer(subscriber, None)

    def write(self, data: str) -> None:
        if not data:
            return
        with self._lock:
            if not self.is_alive():
                raise OSError("Native Windows session has exited.")
            try:
                self.process.write(data)
            except EOFError as exc:
                raise OSError("Native Windows session has exited.") from exc
            self.activity = int(time.time())

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = terminal_dimensions(cols, rows)
        with self._lock:
            if self.is_alive():
                self.process.setwinsize(rows, cols)

    def finish(self) -> None:
        with self._lock:
            self._closed = True
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            _offer(subscriber, None)

    def terminate(self) -> None:
        self.finish()
        with suppress(EOFError, OSError):
            self.process.close(force=True)


@dataclass(eq=False)
class NativeSessionAttachment:
    session: NativeSession
    subscriber: queue.Queue[bytes | None]
    _closed: bool = False

    async def read(self) -> bytes:
        if self._closed:
            return b""
        data = await asyncio.to_thread(self.subscriber.get)
        return data or b""

    def write(self, data: str) -> None:
        if not self._closed:
            self.session.write(data)

    def resize(self, cols: int, rows: int) -> None:
        if not self._closed:
            self.session.resize(cols, rows)

    def is_alive(self) -> bool:
        return not self._closed and self.session.is_alive()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.session.unsubscribe(self.subscriber)


class NativeSessionRegistry:
    def __init__(self, process_factory: ProcessFactory | None = None) -> None:
        self._process_factory = process_factory or spawn_conpty_process
        self._sessions: dict[str, NativeSession] = {}
        self._lock = threading.RLock()

    def create(
        self,
        name: str,
        cwd: str,
        command: str,
        *,
        agent: str = "unknown",
        environment: Mapping[str, str] | None = None,
        keep_shell_on_exit: bool = True,
        cols: int = 120,
        rows: int = 36,
    ) -> NativeSession:
        process_env = dict(os.environ if environment is None else environment)
        process_env["PATH"] = augmented_windows_path(process_env)
        process_env["TERM"] = "xterm-256color"
        process_env["COLORTERM"] = "truecolor"
        process_env.pop("TMUX", None)
        process_env.pop("LD_LIBRARY_PATH", None)
        argv = windows_shell_argv(command, keep_open=keep_shell_on_exit)
        cols, rows = terminal_dimensions(cols, rows)
        now = int(time.time())
        with self._lock:
            existing = self._sessions.get(name)
            if existing and existing.is_alive():
                raise ValueError(f"session already exists: {name}")
            if existing:
                self._sessions.pop(name, None)
            process = self._process_factory(
                argv,
                cwd=cwd,
                env=process_env,
                dimensions=(rows, cols),
            )
            session = NativeSession(
                name=name,
                cwd=cwd,
                command=command,
                agent=agent,
                process=process,
                created=now,
                activity=now,
            )
            self._sessions[name] = session
        reader = threading.Thread(
            target=self._read_session,
            args=(session,),
            name=f"staragent-conpty-{name}",
            daemon=True,
        )
        reader.start()
        return session

    def get(self, name: str) -> NativeSession | None:
        with self._lock:
            session = self._sessions.get(name)
            if session and session.is_alive():
                return session
            if session:
                self._sessions.pop(name, None)
            return None

    def exists(self, name: str) -> bool:
        return self.get(name) is not None

    def list(self) -> list[NativeSession]:
        with self._lock:
            names = tuple(self._sessions)
        return [session for name in names if (session := self.get(name)) is not None]

    def attach(self, name: str) -> NativeSessionAttachment:
        session = self.get(name)
        if not session:
            raise ValueError(f"session not found: {name}")
        return session.subscribe()

    def kill(self, name: str) -> None:
        with self._lock:
            session = self._sessions.pop(name, None)
        if not session or not session.is_alive():
            raise ValueError(f"session not found: {name}")
        session.terminate()

    @staticmethod
    def _read_session(session: NativeSession) -> None:
        try:
            while True:
                try:
                    value = session.process.read(8192)
                except EOFError:
                    break
                data = value.encode("utf-8", errors="replace") if isinstance(value, str) else value
                if data:
                    session.append_output(bytes(data))
        except (OSError, RuntimeError):
            pass
        finally:
            session.finish()


def _offer(target: queue.Queue[bytes | None], value: bytes | None) -> None:
    try:
        target.put_nowait(value)
        return
    except queue.Full:
        pass
    with suppress(queue.Empty):
        target.get_nowait()
    with suppress(queue.Full):
        target.put_nowait(value)


def terminal_dimensions(cols: int, rows: int) -> tuple[int, int]:
    return max(20, min(int(cols), 300)), max(5, min(int(rows), 120))


_REGISTRY = NativeSessionRegistry()


def native_session_registry() -> NativeSessionRegistry:
    return _REGISTRY
