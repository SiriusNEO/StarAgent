from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import re
import signal
import struct
import subprocess
import termios
from dataclasses import dataclass

MAX_TERMINAL_INPUT_BYTES = 64 * 1024
TERMINAL_SCROLLBACK_RESET_SEQUENCES = (
    *(f"\x1b[?{mode}{state}".encode() for mode in (47, 1047, 1048, 1049) for state in "hl"),
    b"\x1b[22;0;0t",
    b"\x1b[23;0;0t",
    b"\x1b[3J",
    b"\x1b[H\x1b[2J",
    b"\x1b[1;1H\x1b[2J",
    b"\x1b[2J",
    b"\x1bc",
)
TERMINAL_SCROLLBACK_RESET_PATTERN = re.compile(
    b"|".join(re.escape(sequence) for sequence in TERMINAL_SCROLLBACK_RESET_SEQUENCES)
)


class TerminalOutputFilter:
    def __init__(self) -> None:
        self._tail = b""

    def feed(self, data: bytes) -> bytes:
        if not data:
            return b""
        combined = self._tail + data
        self._tail = b""
        output = bytearray()
        cursor = 0

        while cursor < len(combined):
            escape = combined.find(b"\x1b", cursor)
            if escape < 0:
                output.extend(combined[cursor:])
                break

            output.extend(combined[cursor:escape])
            remaining = combined[escape:]
            blocked = next(
                (
                    sequence
                    for sequence in TERMINAL_SCROLLBACK_RESET_SEQUENCES
                    if remaining.startswith(sequence)
                ),
                None,
            )
            if blocked is not None:
                cursor = escape + len(blocked)
                continue

            if any(
                sequence.startswith(remaining) for sequence in TERMINAL_SCROLLBACK_RESET_SEQUENCES
            ):
                self._tail = remaining
                break

            output.append(0x1B)
            cursor = escape + 1

        return bytes(output)

    def flush(self) -> bytes:
        data = TERMINAL_SCROLLBACK_RESET_PATTERN.sub(b"", self._tail)
        self._tail = b""
        return data


@dataclass
class PtyTerminal:
    master_fd: int
    process: subprocess.Popen[bytes]

    @classmethod
    def attach_tmux(cls, session: str, cols: int = 120, rows: int = 36) -> PtyTerminal:
        master_fd, slave_fd = pty.openpty()
        set_winsize(master_fd, cols, rows)
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("LD_LIBRARY_PATH", None)
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        process = subprocess.Popen(
            ["tmux", "attach-session", "-t", session],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)
        return cls(master_fd=master_fd, process=process)

    async def read(self) -> bytes:
        return await asyncio.to_thread(os.read, self.master_fd, 8192)

    def write(self, data: str) -> None:
        if data:
            os.write(self.master_fd, data.encode("utf-8", errors="ignore"))

    def resize(self, cols: int, rows: int) -> None:
        set_winsize(self.master_fd, cols, rows)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(self.process.pid), signal.SIGWINCH)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.master_fd)
        if self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self.process.pid), signal.SIGHUP)
            try:
                self.process.terminate()
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()


def set_winsize(fd: int, cols: int, rows: int) -> None:
    cols = max(20, min(int(cols), 300))
    rows = max(5, min(int(rows), 120))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def parse_client_message(message: str) -> tuple[str, object]:
    if len(message.encode("utf-8", errors="ignore")) > MAX_TERMINAL_INPUT_BYTES:
        return "unknown", None
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError, ValueError):
        return "unknown", None
    if not isinstance(payload, dict):
        return "unknown", None
    message_type = str(payload.get("type") or "")
    if message_type == "input":
        data = str(payload.get("data") or "")
        if len(data.encode("utf-8", errors="ignore")) > MAX_TERMINAL_INPUT_BYTES:
            return "unknown", None
        return message_type, data
    if message_type == "resize":
        try:
            return message_type, {
                "cols": int(payload.get("cols") or 120),
                "rows": int(payload.get("rows") or 36),
            }
        except (TypeError, ValueError):
            return "unknown", None
    return "unknown", None
