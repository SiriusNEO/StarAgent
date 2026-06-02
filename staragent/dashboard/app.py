from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import os
import re
import shlex
import socket
import threading
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from staragent.adopt import adopt_existing_session, discover_adoptable_sessions
from staragent.dependencies import dependencies_status, ensure_dependencies
from staragent.hub import (
    NodeEntry,
    add_node,
    collect_hub_sessions,
    collect_node_views,
    node_by_name,
    remove_node,
    request_json,
    websocket_url,
)
from staragent.paths import state_dir
from staragent.pty_terminal import (
    MAX_TERMINAL_INPUT_BYTES,
    PtyTerminal,
    TerminalOutputFilter,
    parse_client_message,
)
from staragent.runtime import (
    capture_tmux_pane_ansi,
    kill_tmux_session,
    send_tmux_input,
    send_tmux_message,
    start_tmux_worker,
    tmux_session_exists,
)
from staragent.schemas import CreateDirectory, CreateWorker, SendMessage, TerminalInput
from staragent.session_parser import (
    tmux_transcript_state,
    transcript_state_from_payload,
    transcript_state_payload,
)
from staragent.tailscale import tailscale_hub_payload
from staragent.transcript import strip_ansi

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

COMMAND_PRESETS = [
    {"label": "Codex YOLO", "command": "codex --yolo"},
    {"label": "Codex", "command": "codex"},
    {"label": "Claude Skip Permissions", "command": "claude --dangerously-skip-permissions"},
    {"label": "Claude", "command": "claude"},
    {"label": "Gemini", "command": "gemini"},
    {"label": "OpenCode", "command": "opencode"},
    {"label": "Shell", "command": "bash"},
]


@dataclass
class HttpTerminal:
    terminal_id: str
    node_name: str
    session_name: str
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    last_poll_at: float = field(default_factory=lambda: datetime.now().timestamp())
    queue: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    terminal: PtyTerminal | None = None
    reader: asyncio.Task[None] | None = None


http_terminals: dict[str, HttpTerminal] = {}
HTTP_TERMINAL_IDLE_SECONDS = 45.0
HTTP_TERMINAL_MAX_AGE_SECONDS = 15 * 60.0
CHAT_HISTORY_PATH = state_dir() / "chat_history.json"
CHAT_HISTORY_LOCK = threading.RLock()
AUTH_COOKIE = "staragent_auth"


def create_app() -> FastAPI:
    app = FastAPI(title="StarAgent")
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        if not auth_enabled() or is_public_path(request.url.path):
            return await call_next(request)
        if request_is_authenticated(request):
            return await call_next(request)
        if wants_html(request):
            return RedirectResponse(
                f"/login?next={urllib.parse.quote(str(request.url.path))}", status_code=303
            )
        return PlainTextResponse("Unauthorized", status_code=401)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/") -> HTMLResponse:
        return templates.TemplateResponse(
            request, "login.html", {"next": safe_next_path(next), "error": ""}
        )

    @app.post("/login")
    async def login(request: Request):
        body = (await request.body()).decode("utf-8")
        form = urllib.parse.parse_qs(body)
        token = (form.get("token") or [""])[0]
        next_path = safe_next_path((form.get("next") or ["/"])[0])
        if valid_token(token):
            response = RedirectResponse(next_path, status_code=303)
            response.set_cookie(
                AUTH_COOKIE,
                token,
                httponly=True,
                samesite="lax",
                secure=False,
                max_age=60 * 60 * 24 * 30,
            )
            return response
        return templates.TemplateResponse(
            request, "login.html", {"next": next_path, "error": "Invalid token"}, status_code=401
        )

    @app.post("/logout")
    def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(AUTH_COOKIE)
        return response

    @app.on_event("startup")
    async def startup_http_terminal_janitor() -> None:
        app.state.http_terminal_janitor = asyncio.create_task(http_terminal_janitor())

    @app.on_event("shutdown")
    async def shutdown_http_terminals() -> None:
        janitor = getattr(app.state, "http_terminal_janitor", None)
        if janitor:
            janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await janitor
        for terminal_id in list(http_terminals):
            terminal = http_terminals.pop(terminal_id, None)
            if terminal:
                await close_http_terminal(terminal)

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse("/sessions", status_code=303)

    @app.get("/sessions", response_class=HTMLResponse)
    def sessions_page(request: Request) -> HTMLResponse:
        node_views = collect_node_views()
        views = sorted(
            [session for node in node_views for session in node.sessions],
            key=lambda item: (item.node_id, item.name),
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "views": views,
                "node_views": node_views,
                "stats": dashboard_stats(node_views, views),
                "relative_time": relative_time,
                "command_presets": COMMAND_PRESETS,
                "initial_explorer_path": str(Path.cwd()),
            },
        )

    @app.get("/nodes", response_class=HTMLResponse)
    def nodes_page(request: Request) -> HTMLResponse:
        node_views = collect_node_views()
        views = sorted(
            [session for node in node_views for session in node.sessions],
            key=lambda item: (item.node_id, item.name),
        )
        return templates.TemplateResponse(
            request,
            "nodes.html",
            {
                "node_views": node_views,
                "stats": dashboard_stats(node_views, views),
            },
        )

    @app.get("/sessions/{name}", response_class=HTMLResponse)
    def session_detail(request: Request, name: str) -> HTMLResponse:
        for view in session_views():
            if view.name == name:
                return session_response(request, view)
        raise HTTPException(status_code=404, detail="Session not found")

    @app.get("/nodes/{node_id}/sessions/{name}", response_class=HTMLResponse)
    def node_session_detail(request: Request, node_id: str, name: str) -> HTMLResponse:
        for view in session_views():
            if view.node_id == node_id and view.name == name:
                return session_response(request, view)
        raise HTTPException(status_code=404, detail="Session not found")

    @app.websocket("/ws/sessions/{name}/terminal")
    async def terminal_socket(websocket: WebSocket, name: str) -> None:
        if not websocket_is_authenticated(websocket):
            await websocket.accept()
            await websocket.close(code=4401, reason="unauthorized")
            return
        await local_terminal_socket(websocket, name)

    @app.websocket("/ws/nodes/{node_id}/sessions/{name}/terminal")
    async def node_terminal_socket(websocket: WebSocket, node_id: str, name: str) -> None:
        if not websocket_is_authenticated(websocket):
            await websocket.accept()
            await websocket.close(code=4401, reason="unauthorized")
            return
        try:
            node = node_by_name(node_id)
        except KeyError:
            await websocket.accept()
            await websocket.close(code=4404, reason=f"node not found: {node_id}")
            return
        if node.is_local:
            await local_terminal_socket(websocket, name)
        else:
            await proxy_terminal_socket(websocket, node, name)

    async def local_terminal_socket(websocket: WebSocket, name: str) -> None:
        await websocket.accept()
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

    @app.post("/api/sessions/{name}/send")
    def send_message(name: str, payload: SendMessage) -> dict[str, str]:
        return send_node_message("local", name, payload)

    @app.post("/api/nodes/{node_id}/sessions/{name}/send")
    def send_node_message(node_id: str, name: str, payload: SendMessage) -> dict[str, str]:
        try:
            node = node_by_name(node_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}") from exc
        if not is_agent_session(node_id, name):
            raise HTTPException(
                status_code=400,
                detail="system sessions are read-only; Chat is only available for agent sessions",
            )
        if not node.is_local:
            return request_json(
                node, "POST", f"/api/sessions/{urllib.parse.quote(name)}/send", payload.model_dump()
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
        return node_session_output("local", name, lines)

    @app.get("/api/nodes/{node_id}/sessions/{name}/output")
    def node_session_output(node_id: str, name: str, lines: int = 160) -> dict[str, str]:
        try:
            node = node_by_name(node_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}") from exc
        lines = max(20, min(lines, 5000))
        if not node.is_local:
            return request_json(
                node, "GET", f"/api/sessions/{urllib.parse.quote(name)}/output?lines={lines}"
            )
        if not tmux_session_exists(name):
            raise HTTPException(status_code=404, detail=f"tmux session not found: {name}")
        return {"output": capture_tmux_pane_ansi(name, lines=lines)}

    @app.get("/api/sessions/{name}/transcript-state")
    def local_session_transcript_state(name: str, lines: int = 500) -> dict[str, object]:
        try:
            return transcript_state_payload(tmux_transcript_state(name, lines=lines))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/nodes/{node_id}/sessions/{name}/terminal-http")
    async def create_http_terminal(node_id: str, name: str) -> dict[str, str]:
        await cleanup_http_terminals()
        try:
            node = node_by_name(node_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}") from exc
        try:
            terminal = await open_http_terminal(node, name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        http_terminals[terminal.terminal_id] = terminal
        return {"terminal_id": terminal.terminal_id}

    @app.get("/api/terminal-http/{terminal_id}/output")
    async def http_terminal_output(terminal_id: str, timeout: float = 0.8) -> dict[str, object]:
        await cleanup_http_terminals()
        terminal = http_terminals.get(terminal_id)
        if not terminal:
            raise HTTPException(status_code=404, detail="terminal not found")
        terminal.last_poll_at = datetime.now().timestamp()
        chunks: list[str] = []
        try:
            data = await asyncio.wait_for(terminal.queue.get(), timeout=max(0.1, min(timeout, 3)))
        except TimeoutError:
            return {"chunks": [], "closed": False}
        if data is None:
            http_terminals.pop(terminal_id, None)
            return {"chunks": [], "closed": True}
        chunks.append(base64.b64encode(data).decode("ascii"))
        while len(chunks) < 20:
            try:
                data = terminal.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if data is None:
                http_terminals.pop(terminal_id, None)
                return {"chunks": chunks, "closed": True}
            chunks.append(base64.b64encode(data).decode("ascii"))
        return {"chunks": chunks, "closed": False}

    @app.post("/api/terminal-http/{terminal_id}/input")
    async def http_terminal_input(terminal_id: str, payload: TerminalInput) -> dict[str, str]:
        await cleanup_http_terminals()
        terminal = http_terminals.get(terminal_id)
        if not terminal:
            raise HTTPException(status_code=404, detail="terminal not found")
        terminal.last_poll_at = datetime.now().timestamp()
        if not terminal.terminal:
            raise HTTPException(status_code=410, detail="terminal closed")
        if len(payload.data.encode("utf-8", errors="ignore")) > MAX_TERMINAL_INPUT_BYTES:
            raise HTTPException(status_code=413, detail="terminal input too large")
        terminal.terminal.write(payload.data)
        return {"status": "sent"}

    @app.post("/api/terminal-http/{terminal_id}/resize")
    async def http_terminal_resize(terminal_id: str, payload: TerminalResize) -> dict[str, str]:
        await cleanup_http_terminals()
        terminal = http_terminals.get(terminal_id)
        if not terminal:
            raise HTTPException(status_code=404, detail="terminal not found")
        terminal.last_poll_at = datetime.now().timestamp()
        if terminal.terminal:
            terminal.terminal.resize(payload.cols, payload.rows)
        return {"status": "resized"}

    @app.delete("/api/terminal-http/{terminal_id}")
    async def close_http_terminal_route(terminal_id: str) -> dict[str, str]:
        terminal = http_terminals.pop(terminal_id, None)
        if terminal:
            await close_http_terminal(terminal)
        return {"status": "closed"}

    @app.post("/api/workers")
    def create_worker(payload: CreateWorkerRequest) -> dict[str, str]:
        try:
            node = node_by_name(payload.node)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {payload.node}") from exc
        worker = CreateWorker(name=payload.name, cwd=payload.cwd, command=payload.command)
        if not node.is_local:
            return request_json(node, "POST", "/api/workers", worker.model_dump())
        try:
            start_tmux_worker(worker.name, worker.cwd, worker.command)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "created", "name": worker.name}

    @app.get("/api/adoptable-sessions")
    def adoptable_sessions(node: str = "local") -> dict[str, list[dict[str, object]]]:
        try:
            node_entry = node_by_name(node)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node}") from exc
        if not node_entry.is_local:
            return request_json(node_entry, "GET", "/api/adoptable-sessions")
        return {"sessions": [item.as_dict() for item in discover_adoptable_sessions()]}

    @app.post("/api/adopt")
    def adopt_session(payload: AdoptSessionRequest) -> dict[str, object]:
        try:
            node = node_by_name(payload.node)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {payload.node}") from exc
        if not node.is_local:
            return request_json(node, "POST", "/api/adopt", {"name": payload.name})
        try:
            adopted = adopt_existing_session(payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "adopted", "session": adopted.as_dict()}

    @app.delete("/api/sessions/{name}")
    def stop_session(name: str) -> dict[str, str]:
        return stop_node_session("local", name)

    @app.delete("/api/nodes/{node_id}/sessions/{name}")
    def stop_node_session(node_id: str, name: str) -> dict[str, str]:
        try:
            node = node_by_name(node_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}") from exc
        if not node.is_local:
            return request_json(node, "DELETE", f"/api/sessions/{urllib.parse.quote(name)}")
        try:
            kill_tmux_session(name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "stopped", "name": name}

    @app.post("/api/nodes")
    def create_node(payload: NodeRequest) -> dict[str, str]:
        try:
            node = add_node(payload.name, payload.url, payload.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "created", "name": node.name, "url": node.url or "local"}

    @app.get("/api/nodes")
    def list_nodes() -> dict[str, list[dict[str, object]]]:
        return {"nodes": [node_payload(node) for node in collect_node_views()]}

    @app.get("/api/tailscale/hub")
    def tailscale_hub_status() -> dict[str, object]:
        return tailscale_hub_payload()

    @app.get("/api/dependencies")
    def dependency_status_route() -> dict[str, object]:
        return dependencies_status()

    @app.post("/api/dependencies/ensure")
    def ensure_dependencies_route() -> dict[str, object]:
        try:
            return ensure_dependencies()
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/nodes/{node_id}/sessions/{name}/chat-history")
    def get_chat_history(node_id: str, name: str) -> dict[str, list[dict[str, object]]]:
        return {"messages": chat_history(node_id, name)}

    @app.get("/api/nodes/{node_id}/sessions/{name}/chat-sync")
    def sync_chat(node_id: str, name: str) -> dict[str, object]:
        try:
            return sync_chat_from_transcript(node_id, name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/nodes/{node_id}/sessions/{name}/chat-history")
    def save_chat_message(node_id: str, name: str, payload: ChatMessageRequest) -> dict[str, str]:
        append_chat_message(node_id, name, payload.role, payload.text, payload.time, payload.id)
        return {"status": "saved"}

    @app.delete("/api/nodes/{node_id}")
    def delete_node(node_id: str) -> dict[str, str]:
        try:
            remove_node(node_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "removed", "name": node_id}

    @app.get("/api/directories")
    def directories(
        path: str | None = None,
        node: str = "local",
        include_files: bool = False,
        root: str | None = None,
    ) -> dict[str, object]:
        try:
            node_entry = node_by_name(node)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node}") from exc
        if not node_entry.is_local:
            suffix = "/api/directories"
            query = []
            if path:
                query.append(f"path={urllib.parse.quote(path)}")
            if include_files:
                query.append("include_files=true")
            if root:
                query.append(f"root={urllib.parse.quote(root)}")
            if query:
                suffix += "?" + "&".join(query)
            return request_json(node_entry, "GET", suffix)
        try:
            return directory_listing(path, include_files=include_files, root=root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/directories")
    def create_directory(
        payload: CreateDirectory, node: str = "local", root: str | None = None
    ) -> dict[str, object]:
        try:
            node_entry = node_by_name(node)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node}") from exc
        if not node_entry.is_local:
            suffix = "/api/directories"
            if root:
                suffix += f"?root={urllib.parse.quote(root)}"
            return request_json(node_entry, "POST", suffix, payload.model_dump())
        try:
            return create_directory_payload(payload.path, payload.name, root=root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/files/preview")
    def file_preview(path: str, node: str = "local", root: str | None = None) -> dict[str, object]:
        try:
            node_entry = node_by_name(node)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node}") from exc
        if not node_entry.is_local:
            suffix = f"/api/files/preview?path={urllib.parse.quote(path)}"
            if root:
                suffix += f"&root={urllib.parse.quote(root)}"
            return request_json(
                node_entry,
                "GET",
                suffix,
            )
        try:
            return file_preview_payload(path, root=root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def auth_token() -> str:
    return os.environ.get("STARAGENT_AUTH_TOKEN", "").strip()


def auth_enabled() -> bool:
    return bool(auth_token())


def valid_token(value: str) -> bool:
    token = auth_token()
    return bool(token) and hmac.compare_digest(value or "", token)


def is_public_path(path: str) -> bool:
    return path == "/login" or path.startswith("/static/")


def bearer_token(header: str | None) -> str:
    if not header:
        return ""
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def request_is_authenticated(request: Request) -> bool:
    return valid_token(request.cookies.get(AUTH_COOKIE, "")) or valid_token(
        bearer_token(request.headers.get("authorization"))
    )


def websocket_is_authenticated(websocket: WebSocket) -> bool:
    if not auth_enabled():
        return True
    query_token = websocket.query_params.get("token", "")
    cookie_token = websocket.cookies.get(AUTH_COOKIE, "")
    auth_header = websocket.headers.get("authorization")
    return (
        valid_token(query_token)
        or valid_token(cookie_token)
        or valid_token(bearer_token(auth_header))
    )


def wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def safe_next_path(value: str) -> str:
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


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


async def open_http_terminal(node: NodeEntry, name: str) -> HttpTerminal:
    terminal_id = uuid.uuid4().hex
    bridge = HttpTerminal(terminal_id=terminal_id, node_name=node.name, session_name=name)
    if not node.is_local:
        raise RuntimeError("HTTP terminal fallback is only available for local nodes")
    if not tmux_session_exists(name):
        raise ValueError(f"tmux session not found: {name}")
    bridge.terminal = PtyTerminal.attach_tmux(name)
    bridge.reader = asyncio.create_task(stream_pty_to_queue(bridge.terminal, bridge.queue))
    return bridge


async def stream_pty_to_queue(terminal: PtyTerminal, queue: asyncio.Queue[bytes | None]) -> None:
    output_filter = TerminalOutputFilter()
    try:
        while terminal.process.poll() is None:
            data = await terminal.read()
            if not data:
                break
            filtered = output_filter.feed(data)
            if filtered:
                await queue.put(filtered)
    except (OSError, RuntimeError):
        pass
    finally:
        filtered = output_filter.flush()
        if filtered:
            await queue.put(filtered)
        await queue.put(None)


async def close_http_terminal(terminal: HttpTerminal) -> None:
    if terminal.reader:
        terminal.reader.cancel()
    if terminal.terminal:
        terminal.terminal.close()
    await terminal.queue.put(None)


async def cleanup_http_terminals() -> None:
    now = datetime.now().timestamp()
    stale = [
        terminal_id
        for terminal_id, terminal in http_terminals.items()
        if now - terminal.last_poll_at > HTTP_TERMINAL_IDLE_SECONDS
        or now - terminal.created_at > HTTP_TERMINAL_MAX_AGE_SECONDS
    ]
    for terminal_id in stale:
        terminal = http_terminals.pop(terminal_id, None)
        if terminal:
            await close_http_terminal(terminal)


async def http_terminal_janitor() -> None:
    while True:
        await asyncio.sleep(min(HTTP_TERMINAL_IDLE_SECONDS, 30.0))
        await cleanup_http_terminals()


async def proxy_terminal_socket(websocket: WebSocket, node: NodeEntry, name: str) -> None:
    await websocket.accept()
    remote_url = websocket_url(node, f"/ws/sessions/{urllib.parse.quote(name)}/terminal")
    try:
        async with websockets.connect(remote_url) as remote:
            to_remote = asyncio.create_task(proxy_browser_to_agent(websocket, remote))
            to_browser = asyncio.create_task(proxy_agent_to_browser(remote, websocket))
            done, pending = await asyncio.wait(
                {to_remote, to_browser}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                with contextlib.suppress(WebSocketDisconnect, websockets.ConnectionClosed):
                    task.result()
            for task in pending:
                task.cancel()
    except Exception as exc:
        await websocket.close(code=1011, reason=str(exc)[:120])


async def proxy_browser_to_agent(websocket: WebSocket, remote) -> None:
    while True:
        message = await websocket.receive_text()
        await remote.send(message)


async def proxy_agent_to_browser(remote, websocket: WebSocket) -> None:
    async for message in remote:
        if isinstance(message, bytes):
            await websocket.send_bytes(message)
        else:
            await websocket.send_text(message)


class CreateWorkerRequest(BaseModel):
    node: str = "local"
    name: str
    cwd: str
    command: str


class AdoptSessionRequest(BaseModel):
    node: str = "local"
    name: str


class NodeRequest(BaseModel):
    name: str
    url: str
    mode: str = "lan"


class TerminalResize(BaseModel):
    cols: int = 120
    rows: int = 36


class ChatMessageRequest(BaseModel):
    role: str
    text: str
    time: int | None = None
    id: str = ""


def session_views():
    return collect_hub_sessions()


def session_response(request: Request, view) -> HTMLResponse:
    attach_command = (
        local_attach_command(view) if view.node_id == "local" else ssh_attach_command(view)
    )
    return templates.TemplateResponse(
        request,
        "session.html",
        {
            "view": view,
            "relative_time": relative_time,
            "attach_command": attach_command,
        },
    )


def dashboard_stats(node_views, views):
    return {
        "nodes": len(node_views),
        "connected_nodes": sum(1 for node in node_views if node.status == "connected"),
        "disconnected_nodes": sum(1 for node in node_views if node.status != "connected"),
        "total": len(views),
        "agent": sum(1 for view in views if view.session_type == "agent"),
        "system": sum(1 for view in views if view.session_type == "system"),
        "attention": sum(1 for view in views if view.needs_attention),
        "active": sum(1 for view in views if view.status in {"active", "attached"}),
        "idle": sum(1 for view in views if view.status == "idle"),
    }


def node_payload(node) -> dict[str, object]:
    return {
        "name": node.name,
        "mode": node.mode,
        "status": node.status,
        "endpoint": node.endpoint,
        "session_count": node.session_count,
        "error": node.error,
        "removable": node.is_removable,
        "sessions": [
            {
                "name": session.name,
                "status": session.status,
                "agent": session.agent,
                "session_type": session.session_type,
                "repo": session.repo,
                "branch": session.branch,
                "task": session.task,
            }
            for session in node.sessions
        ],
    }


def is_agent_session(node_id: str, name: str) -> bool:
    for session in collect_hub_sessions():
        if session.node_id == node_id and session.name == name:
            return session.session_type == "agent"
    return True


def chat_key(node_id: str, session: str) -> str:
    return f"{node_id}/{session}"


def load_chat_histories() -> dict[str, list[dict[str, object]]]:
    if not CHAT_HISTORY_PATH.exists():
        return {}
    try:
        data = json.loads(CHAT_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_chat_histories(data: dict[str, list[dict[str, object]]]) -> None:
    with CHAT_HISTORY_LOCK:
        CHAT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = CHAT_HISTORY_PATH.with_name(f"{CHAT_HISTORY_PATH.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temp_path.replace(CHAT_HISTORY_PATH)


def chat_history(node_id: str, session: str) -> list[dict[str, object]]:
    messages = load_chat_histories().get(chat_key(node_id, session), [])
    return sorted_chat_messages(messages) if isinstance(messages, list) else []


def append_chat_message(
    node_id: str,
    session: str,
    role: str,
    text: str,
    timestamp: int | None = None,
    message_id: str = "",
) -> None:
    role = role if role in {"user", "agent", "session"} else "agent"
    text = text.strip()
    if not text:
        return
    with CHAT_HISTORY_LOCK:
        data = load_chat_histories()
        key = chat_key(node_id, session)
        messages = data.get(key, [])
        if not isinstance(messages, list):
            messages = []
        if role == "agent" and looks_like_transcript_fragment(text):
            return
        message = {
            "role": role,
            "text": text,
            "time": timestamp or int(datetime.now().timestamp() * 1000),
        }
        if message_id:
            message["id"] = message_id
        if message_exists(messages, role, text, message_id):
            return
        messages.append(message)
        data[key] = sorted_chat_messages(messages)[-80:]
        save_chat_histories(data)


def sync_chat_from_transcript(node_id: str, session: str) -> dict[str, object]:
    state = node_transcript_state(node_id, session, lines=500)
    completed_reply = state.completed_reply
    reply = state.reply
    working = state.working
    final = state.final
    working_label = state.working_label if working else ""
    messages = chat_history(node_id, session)
    now = int(datetime.now().timestamp() * 1000)

    if state.messages:
        replace_chat_history_from_transcript(node_id, session, state.messages)
        messages = chat_history(node_id, session)
        return {
            "messages": messages,
            "working": working,
            "working_label": working_label,
            "working_since_ms": state.working_since_ms,
            "final": final,
            "reply": reply,
            "token_usage": state.token_usage.as_dict() if state.token_usage else None,
        }

    if completed_reply and not message_exists(messages, "agent", completed_reply):
        timestamp = previous_agent_timestamp(messages, now) if working else now
        append_chat_message(node_id, session, "agent", completed_reply, timestamp)
        messages = chat_history(node_id, session)

    if working and should_record_external_activity(messages, now):
        append_chat_message(
            node_id, session, "session", "External terminal activity detected.", now
        )
        messages = chat_history(node_id, session)

    if final and reply and not message_exists(messages, "agent", reply):
        if should_record_external_activity(messages, now):
            append_chat_message(
                node_id, session, "session", "External terminal activity detected.", now
            )
        append_chat_message(node_id, session, "agent", reply, now)
        messages = chat_history(node_id, session)

    return {
        "messages": messages,
        "working": working,
        "working_label": working_label,
        "working_since_ms": state.working_since_ms,
        "final": final,
        "reply": reply,
        "token_usage": state.token_usage.as_dict() if state.token_usage else None,
    }


def node_transcript_state(node_id: str, session: str, lines: int = 500):
    node = node_by_name(node_id)
    lines = max(20, min(lines, 500))
    if not node.is_local:
        body = request_json(
            node,
            "GET",
            f"/api/sessions/{urllib.parse.quote(session)}/transcript-state?lines={lines}",
        )
        return transcript_state_from_payload(body)
    return tmux_transcript_state(session, lines=lines)


def replace_chat_history_from_transcript(node_id: str, session: str, transcript_messages) -> None:
    rows = []
    now = int(datetime.now().timestamp() * 1000)
    for index, message in enumerate(transcript_messages):
        role = message.role if message.role in {"user", "agent", "session"} else "agent"
        text = message.text.strip()
        if not text or looks_like_transcript_fragment(text):
            continue
        timestamp = message.timestamp_ms or now + index
        row = {"role": role, "text": text, "time": timestamp}
        if message.source_id:
            row["id"] = message.source_id
        rows.append(row)
    with CHAT_HISTORY_LOCK:
        data = load_chat_histories()
        data[chat_key(node_id, session)] = sorted_chat_messages(rows)[-80:]
        save_chat_histories(data)


def session_transcript(node_id: str, session: str, lines: int = 500) -> str:
    node = node_by_name(node_id)
    lines = max(20, min(lines, 500))
    if not node.is_local:
        body = request_json(
            node, "GET", f"/api/sessions/{urllib.parse.quote(session)}/output?lines={lines}"
        )
        return strip_ansi(str(body.get("output") or ""))
    if not tmux_session_exists(session):
        raise RuntimeError(f"tmux session not found: {session}")
    return strip_ansi(capture_tmux_pane_ansi(session, lines=lines))


def should_record_external_activity(messages: list[dict[str, object]], now: int) -> bool:
    last = messages[-1] if messages else {}
    if last.get("role") == "session":
        return False
    recent_messages = messages[-6:]
    return not any(
        item.get("role") == "user" and now - int(item.get("time") or now) < 120_000
        for item in recent_messages
    )


def message_exists(
    messages: list[dict[str, object]], role: str, text: str, message_id: str = ""
) -> bool:
    if message_id:
        return any(str(item.get("id") or "") == message_id for item in messages)
    fingerprint = chat_fingerprint(text)
    return any(
        item.get("role") == role and chat_fingerprint(str(item.get("text") or "")) == fingerprint
        for item in messages
    )


def previous_agent_timestamp(messages: list[dict[str, object]], fallback: int) -> int:
    user_times = [
        int(item.get("time") or 0)
        for item in messages
        if item.get("role") == "user" and item.get("time")
    ]
    if not user_times:
        return fallback
    return max(0, max(user_times) - 1)


def sorted_chat_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(messages, key=lambda item: int(item.get("time") or 0))


def looks_like_transcript_fragment(text: str) -> bool:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first.startswith(("›", "◦ Working"))


def normalize_chat_text(text: str) -> str:
    return " ".join(text.strip().split())


def chat_fingerprint(text: str) -> str:
    return re.sub(r"\s+", "", text.strip()).lower()


def local_attach_command(view) -> str:
    session = view.config.session or view.name
    quoted_session = shlex.quote(session)
    return f'if [ -n "$TMUX" ]; then tmux switch-client -t {quoted_session}; else tmux attach -t {quoted_session}; fi'


def ssh_attach_command(view) -> str:
    session = view.config.session or view.name
    node = view.node_id if getattr(view, "node_id", "local") != "local" else ssh_target()
    quoted_session = shlex.quote(session)
    remote = f'if [ -n "$TMUX" ]; then tmux switch-client -t {quoted_session}; else tmux attach -t {quoted_session}; fi'
    return f"ssh -t {shlex.quote(node)} {shlex.quote(remote)}"


def ssh_target() -> str:
    configured = os.environ.get("STARAGENT_SSH_TARGET")
    if configured:
        return configured
    fqdn = socket.getfqdn()
    if fqdn and not fqdn.startswith("localhost"):
        return fqdn
    return socket.gethostname()


SENSITIVE_PATH_PARTS = {".ssh", ".aws", ".gnupg", ".staragent"}
SENSITIVE_FILE_NAMES = {".env", "dashboard.env", "id_rsa", "id_ed25519"}


def directory_listing(
    path: str | None = None, include_files: bool = False, root: str | None = None
) -> dict[str, object]:
    root_path = resolve_root(root)
    current = secure_resolve_path(path or str(root_path or Path.cwd()), root_path)
    if not current.exists():
        raise ValueError(f"Path does not exist: {path}")
    if not current.is_dir():
        current = current.parent

    entries = []
    try:
        children = sorted(
            current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
        )
    except OSError as exc:
        raise ValueError(str(exc)) from exc

    for child in children:
        is_dir = child.is_dir()
        if not is_dir and not include_files:
            continue
        if is_dir and child.name in {".git", "__pycache__", "node_modules", ".venv", "venv"}:
            continue
        if is_sensitive_path(child):
            continue
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "hidden": child.name.startswith("."),
                "type": "directory" if is_dir else "file",
            }
        )

    if root_path:
        roots = [{"label": "Workspace", "path": str(root_path)}]
    else:
        home = Path.home()
        roots = [
            {"label": "Current", "path": str(Path.cwd())},
            {"label": "Home", "path": str(home)},
        ]
        if home != Path("/root") and Path("/root").exists():
            roots.append({"label": "Root Home", "path": "/root"})

    return {
        "path": str(current),
        "parent": parent_path(current, root_path),
        "entries": entries,
        "roots": roots,
    }


def create_directory_payload(path: str, name: str, root: str | None = None) -> dict[str, object]:
    root_path = resolve_root(root)
    parent = secure_resolve_path(path or str(root_path or Path.cwd()), root_path)
    if not parent.exists() or not parent.is_dir():
        raise ValueError(f"Parent directory does not exist: {path}")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Folder name is required")
    if clean_name in {".", ".."} or "/" in clean_name or "\\" in clean_name:
        raise ValueError("Folder name cannot contain path separators")
    target = (parent / clean_name).resolve()
    if target.parent != parent:
        raise ValueError("Folder must be created under the current directory")
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise ValueError(f"Path already exists: {target}") from exc
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    return {"status": "created", "path": str(target), "name": target.name}


def file_preview_payload(
    path: str, max_bytes: int = 256 * 1024, root: str | None = None
) -> dict[str, object]:
    root_path = resolve_root(root)
    file_path = secure_resolve_path(path, root_path)
    if not file_path.exists():
        raise ValueError(f"Path does not exist: {path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {path}")
    if is_sensitive_path(file_path):
        raise ValueError(f"Preview is blocked for sensitive path: {file_path.name}")
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    if size > max_bytes:
        return {
            "path": str(file_path),
            "name": file_path.name,
            "size": size,
            "text": "",
            "truncated": True,
            "binary": False,
            "error": f"File is larger than {max_bytes // 1024} KiB.",
        }
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    if b"\x00" in raw:
        return {
            "path": str(file_path),
            "name": file_path.name,
            "size": size,
            "text": "",
            "truncated": False,
            "binary": True,
            "error": "Binary file preview is not supported.",
        }
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return {
        "path": str(file_path),
        "name": file_path.name,
        "size": size,
        "text": text,
        "truncated": False,
        "binary": False,
        "error": "",
    }


def resolve_root(root: str | None) -> Path | None:
    if not root:
        return None
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists() or not root_path.is_dir():
        raise ValueError(f"Workspace root does not exist: {root}")
    return root_path


def secure_resolve_path(path: str, root: Path | None) -> Path:
    resolved = Path(path).expanduser().resolve()
    if root and resolved != root and root not in resolved.parents:
        raise ValueError(f"Path is outside workspace: {path}")
    return resolved


def parent_path(path: Path, root: Path | None) -> str:
    if root and path == root:
        return ""
    parent = path.parent
    if root and parent != root and root not in parent.parents:
        return str(root)
    return str(parent) if parent != path else ""


def is_sensitive_path(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SENSITIVE_PATH_PARTS:
        return True
    name = path.name.lower()
    if name in SENSITIVE_FILE_NAMES:
        return True
    return name.endswith((".pem", ".key")) or name.endswith(".env")


def relative_time(value: datetime | None) -> str:
    if value is None:
        return "no report"
    now = datetime.now(UTC).astimezone()
    delta = now - value.astimezone(now.tzinfo)
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"
