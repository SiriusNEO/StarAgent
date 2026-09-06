from __future__ import annotations

import asyncio
import contextlib

from fastapi import WebSocket, WebSocketDisconnect

from staragent.pty_terminal import PtyTerminal, TerminalOutputFilter, parse_client_message


async def stream_pty_to_websocket(terminal: PtyTerminal, websocket: WebSocket) -> None:
    output_filter = TerminalOutputFilter()
    try:
        while terminal.process.poll() is None:
            data = await terminal.read()
            if not data:
                break
            filtered = output_filter.feed(data)
            if filtered:
                await websocket.send_bytes(filtered)
    except (OSError, RuntimeError, WebSocketDisconnect):
        pass
    finally:
        filtered = output_filter.flush()
        if filtered:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.send_bytes(filtered)


async def interact_with_pty_websocket(
    terminal: PtyTerminal,
    websocket: WebSocket,
    *,
    max_age_seconds: float | None = None,
) -> None:
    reader = asyncio.create_task(stream_pty_to_websocket(terminal, websocket))
    writer = asyncio.create_task(stream_websocket_to_pty(terminal, websocket))
    try:
        done, pending = await asyncio.wait(
            {reader, writer},
            timeout=max_age_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=1000, reason="temporary terminal expired")
        elif reader in done:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=1000, reason="terminal exited")
        for task in done:
            with contextlib.suppress(OSError, RuntimeError, WebSocketDisconnect):
                task.result()
        for task in pending:
            task.cancel()
    finally:
        for task in (reader, writer):
            if not task.done():
                task.cancel()
        await asyncio.gather(reader, writer, return_exceptions=True)


async def stream_websocket_to_pty(terminal: PtyTerminal, websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive_text()
        message_type, payload = parse_client_message(message)
        if message_type == "input":
            terminal.write(str(payload))
        elif message_type == "resize" and isinstance(payload, dict):
            terminal.resize(int(payload["cols"]), int(payload["rows"]))
