from __future__ import annotations

import contextlib

from fastapi import WebSocket, WebSocketDisconnect

from staragent.pty_terminal import PtyTerminal, TerminalOutputFilter


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
