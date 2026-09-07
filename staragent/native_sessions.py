from __future__ import annotations

import asyncio
import base64
import errno
import os
import queue
import shlex
import shutil
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from staragent.windows import augmented_windows_path

MAX_SCROLLBACK_BYTES = 4 * 1024 * 1024
MAX_ATTACH_SNAPSHOT_BYTES = 1024 * 1024
SUBSCRIBER_QUEUE_CHUNKS = 512
SHELL_ALIASES = {"bash", "bash.exe", "sh", "sh.exe", "powershell", "powershell.exe", "pwsh"}


def native_session_mode_enabled() -> bool:
    """Use the in-process terminal registry for self-contained desktop builds."""
    return os.name == "nt" or os.environ.get("STARAGENT_DESKTOP_BUNDLED") == "1"


def native_session_backend_name() -> str:
    return "conpty" if os.name == "nt" else "pty"


def native_session_backend_available() -> bool:
    if os.name == "nt":
        try:
            from winpty import Backend, PtyProcess  # noqa: F401
        except ImportError:
            return False
        return True
    return callable(getattr(os, "openpty", None)) and Path("/bin/sh").is_file()


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


class PosixPtyProcess:
    """Small adapter that gives the standard-library PTY the winpty process contract."""

    def __init__(self, terminal: Any) -> None:
        self.terminal = terminal

    @property
    def pid(self) -> int:
        return int(self.terminal.process.pid)

    def isalive(self) -> bool:
        return self.terminal.is_alive()

    def read(self, size: int) -> bytes:
        try:
            return os.read(self.terminal.master_fd, size)
        except OSError as exc:
            # Linux reports EIO when the PTY slave exits; macOS commonly returns
            # an empty read. Present both as the EOF contract used by the registry.
            if exc.errno in {errno.EIO, errno.EBADF}:
                raise EOFError from exc
            raise

    def write(self, value: str) -> None:
        self.terminal.write(value)

    def setwinsize(self, rows: int, cols: int) -> None:
        self.terminal.resize(cols, rows)

    def close(self, force: bool = False) -> None:
        del force
        self.terminal.close()


def posix_shell_executable() -> str:
    configured = os.environ.get("SHELL", "").strip()
    candidates = (configured, "/bin/zsh", "/bin/bash", "/bin/sh")
    return next(
        (
            candidate
            for candidate in candidates
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK)
        ),
        "/bin/sh",
    )


def posix_shell_argv(command: str = "", *, keep_open: bool = True) -> list[str]:
    shell = posix_shell_executable()
    normalized = command.strip()
    aliases = {"sh", "bash", "zsh", Path(shell).name}
    if not normalized or normalized.lower() in aliases:
        return [shell, "-l"]
    if not keep_open:
        return [shell, "-lc", normalized]
    fallback_shell = shlex.quote(shell)
    script = "\n".join(
        (
            "set +e",
            normalized,
            "staragent_status=$?",
            "printf '\\n[StarAgent] agent exited with status %s. Dropping into shell.\\n' "
            '"$staragent_status"',
            f'exec "${{SHELL:-{fallback_shell}}}" -l',
        )
    )
    return [shell, "-lc", script]


def spawn_posix_pty_process(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: Mapping[str, str],
    dimensions: tuple[int, int],
) -> PosixPtyProcess:
    from staragent.pty_terminal import PtyTerminal

    rows, cols = dimensions
    try:
        terminal = PtyTerminal.spawn(argv, cwd=cwd, env=env, cols=cols, rows=rows)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise OSError(f"Could not start the native PTY command: {exc}") from exc
    return PosixPtyProcess(terminal)


def spawn_native_session_process(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: Mapping[str, str],
    dimensions: tuple[int, int],
) -> Any:
    if os.name == "nt":
        return spawn_conpty_process(argv, cwd=cwd, env=env, dimensions=dimensions)
    return spawn_posix_pty_process(argv, cwd=cwd, env=env, dimensions=dimensions)


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
                raise OSError("Native session has exited.")
            try:
                self.process.write(data)
            except EOFError as exc:
                raise OSError("Native session has exited.") from exc
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
        self._process_factory = process_factory or spawn_native_session_process
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
        if os.name == "nt":
            process_env["PATH"] = augmented_windows_path(process_env)
        process_env["TERM"] = "xterm-256color"
        process_env["COLORTERM"] = "truecolor"
        process_env.pop("TMUX", None)
        process_env.pop("LD_LIBRARY_PATH", None)
        process_env.pop("DYLD_LIBRARY_PATH", None)
        process_env.pop("DYLD_FALLBACK_LIBRARY_PATH", None)
        argv = (
            windows_shell_argv(command, keep_open=keep_shell_on_exit)
            if os.name == "nt"
            else posix_shell_argv(command, keep_open=keep_shell_on_exit)
        )
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
            name=f"staragent-{native_session_backend_name()}-{name}",
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

    def close_all(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
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
            session.terminate()


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
