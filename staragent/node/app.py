from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from staragent.adopt import adopt_existing_session, discover_adoptable_sessions
from staragent.dashboard.app import (
    create_directory_payload,
    directory_listing,
    file_preview_payload,
    stream_pty_to_websocket,
)
from staragent.pty_terminal import PtyTerminal, parse_client_message
from staragent.runtime import (
    capture_tmux_pane_ansi,
    kill_tmux_session,
    send_tmux_input,
    send_tmux_message,
    start_tmux_worker,
    tmux_session_exists,
)
from staragent.schemas import CreateDirectory, CreateWorker, SendMessage, TerminalInput
from staragent.session_parser import tmux_transcript_state, transcript_state_payload
from staragent.status import collect_session_views


def create_app() -> FastAPI:
    app = FastAPI(title="StarAgent Node")

    @app.middleware("http")
    async def require_node_auth(request: Request, call_next):
        if request.url.path == "/api/health":
            return await call_next(request)
        if not node_auth_token():
            return PlainTextResponse(
                "STARAGENT_NODE_TOKEN or STARAGENT_AUTH_TOKEN is required for node API",
                status_code=503,
            )
        if request_is_authenticated(request):
            return await call_next(request)
        return PlainTextResponse("Unauthorized", status_code=401)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/sessions")
    def sessions() -> dict[str, list[dict[str, object]]]:
        return {"sessions": [session_payload(view) for view in collect_session_views()]}

    @app.post("/api/workers")
    def create_worker(payload: CreateWorker) -> dict[str, str]:
        try:
            start_tmux_worker(payload.name, payload.cwd, payload.command)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "created", "name": payload.name}

    @app.get("/api/adoptable-sessions")
    def adoptable_sessions() -> dict[str, list[dict[str, object]]]:
        return {"sessions": [item.as_dict() for item in discover_adoptable_sessions()]}

    @app.post("/api/adopt")
    def adopt_session(payload: AdoptRequest) -> dict[str, object]:
        try:
            adopted = adopt_existing_session(payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "adopted", "session": adopted.as_dict()}

    @app.delete("/api/sessions/{name}")
    def stop_session(name: str) -> dict[str, str]:
        try:
            kill_tmux_session(name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "stopped", "name": name}

    @app.post("/api/sessions/{name}/send")
    def send_message(name: str, payload: SendMessage) -> dict[str, str]:
        if not is_agent_session(name):
            raise HTTPException(
                status_code=400,
                detail="system sessions are read-only; Chat is only available for agent sessions",
            )
        try:
            send_tmux_message(name, payload.text)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "sent"}

    @app.post("/api/sessions/{name}/input")
    def send_input(name: str, payload: TerminalInput) -> dict[str, str]:
        try:
            send_tmux_input(name, payload.data)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "sent"}

    @app.get("/api/sessions/{name}/output")
    def session_output(name: str, lines: int = 160) -> dict[str, str]:
        if not tmux_session_exists(name):
            raise HTTPException(status_code=404, detail=f"tmux session not found: {name}")
        return {"output": capture_tmux_pane_ansi(name, lines=max(20, min(lines, 500)))}

    @app.get("/api/sessions/{name}/transcript-state")
    def session_transcript_state(name: str, lines: int = 500) -> dict[str, object]:
        try:
            return transcript_state_payload(tmux_transcript_state(name, lines=lines))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/directories")
    def directories(
        path: str | None = None, include_files: bool = False, root: str | None = None
    ) -> dict[str, object]:
        try:
            return directory_listing(
                path or str(Path.cwd()), include_files=include_files, root=root
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/directories")
    def create_directory(payload: CreateDirectory, root: str | None = None) -> dict[str, object]:
        try:
            return create_directory_payload(payload.path, payload.name, root=root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/files/preview")
    def file_preview(path: str, root: str | None = None) -> dict[str, object]:
        try:
            return file_preview_payload(path, root=root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.websocket("/ws/sessions/{name}/terminal")
    async def terminal_socket(websocket: WebSocket, name: str) -> None:
        await websocket.accept()
        if not websocket_is_authenticated(websocket):
            await websocket.close(code=4401, reason="unauthorized")
            return
        if not tmux_session_exists(name):
            await websocket.close(code=4404, reason=f"tmux session not found: {name}")
            return
        terminal = PtyTerminal.attach_tmux(name)
        reader = asyncio.create_task(stream_pty_to_websocket(terminal, websocket))
        try:
            while True:
                message = await websocket.receive_text()
                message_type, payload = parse_client_message(message)
                if message_type == "input":
                    terminal.write(str(payload))
                elif message_type == "resize" and isinstance(payload, dict):
                    terminal.resize(int(payload["cols"]), int(payload["rows"]))
        except WebSocketDisconnect:
            pass
        finally:
            reader.cancel()
            terminal.close()

    return app


def node_auth_token() -> str:
    return (
        os.environ.get("STARAGENT_NODE_TOKEN", "").strip()
        or os.environ.get("STARAGENT_AUTH_TOKEN", "").strip()
    )


def bearer_token(header: str | None) -> str:
    if not header:
        return ""
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def valid_node_token(value: str) -> bool:
    token = node_auth_token()
    return bool(token) and hmac.compare_digest(value or "", token)


def request_is_authenticated(request: Request) -> bool:
    return valid_node_token(bearer_token(request.headers.get("authorization")))


def websocket_is_authenticated(websocket: WebSocket) -> bool:
    if not node_auth_token():
        return False
    return valid_node_token(websocket.query_params.get("token", "")) or valid_node_token(
        bearer_token(websocket.headers.get("authorization"))
    )


class AdoptRequest(BaseModel):
    name: str


def session_payload(view) -> dict[str, object]:
    return {
        "name": view.name,
        "agent": view.agent,
        "session_type": view.session_type,
        "node": view.node_name,
        "repo": view.repo,
        "branch": view.branch,
        "task": view.task,
        "status": view.status,
        "summary": view.status_report.summary if view.status_report else "",
        "needs_attention": view.needs_attention,
        "question": view.status_report.question if view.status_report else "",
        "source": view.status_report.source if view.status_report else "tmux",
        "last_updated": view.last_updated.isoformat() if view.last_updated else None,
    }


def is_agent_session(name: str) -> bool:
    for view in collect_session_views():
        if view.name == name:
            return view.session_type == "agent"
    return True
