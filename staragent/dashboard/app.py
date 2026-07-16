from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import importlib.util
import inspect
import io
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from staragent.adopt import adopt_existing_session, discover_adoptable_sessions
from staragent.agent_history import resume_worker_command
from staragent.agent_tools import AgentToolUpdateBusyError, agent_catalog_payload
from staragent.auth import hub_auth_token as stored_hub_auth_token
from staragent.auth import hub_auth_token_source
from staragent.dependencies import dependencies_status, ensure_dependencies
from staragent.event_log import (
    append_hub_event,
    append_node_event,
    archived_node_names,
    read_hub_events,
    read_node_events,
)
from staragent.files import (
    create_directory_payload,
    directory_listing,
    file_preview_payload,
    file_raw_info_payload,
    file_raw_payload,
)
from staragent.hub import (
    NODE_HEARTBEAT_INTERVAL_SECONDS,
    NodeEntry,
    add_node,
    collect_hub_sessions,
    collect_node_session,
    collect_node_view,
    collect_node_views,
    collect_session_navigation_nodes,
    load_nodes,
    mark_hub_session_seen,
    node_agent_history_payload,
    node_agent_tool_update_payload,
    node_agent_tools_payload,
    node_by_name,
    refresh_remote_node_heartbeats,
    remove_node,
    request_json,
    request_raw,
    websocket_url,
)
from staragent.paths import PROJECT_ROOT, state_dir
from staragent.presets import command_presets_payload
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
from staragent.state import atomic_write_bytes, atomic_write_json, file_lock, locked_file, read_json
from staragent.tailscale import tailscale_hub_payload
from staragent.text import strip_ansi
from staragent.transcript import parse_transcript
from staragent.web_terminal import stream_pty_to_websocket

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
STATIC_DIR = PACKAGE_DIR / "static"


def static_version(path: str) -> int:
    try:
        return int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["static_version"] = static_version


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
CHAT_HISTORY_LOCK = file_lock(CHAT_HISTORY_PATH)
CHAT_MESSAGE_MATCH_WINDOW_MS = 5000
CHAT_PENDING_USER_RETENTION_MS = 15 * 60 * 1000
AUTH_COOKIE = "staragent_auth"
THEME_BACKGROUND_DIR = state_dir() / "theme"
THEME_BACKGROUND_STEM = "background"
THEME_BACKGROUND_CONFIG_PATH = THEME_BACKGROUND_DIR / "theme.json"
THEME_BACKGROUND_CONFIG_LOCK = file_lock(THEME_BACKGROUND_CONFIG_PATH)
THEME_BACKGROUND_LIBRARY_DIR = THEME_BACKGROUND_DIR / "backgrounds"
THEME_BACKGROUND_MAX_BYTES = 8 * 1024 * 1024
THEME_BACKGROUND_LIBRARY_LIMIT = 12
THEME_BACKGROUND_ID_PATTERN = re.compile(r"^[a-f0-9]{12,32}$")
THEME_BACKGROUND_FULL_MAX_EDGE = 2560
THEME_BACKGROUND_THUMB_MAX_EDGE = 360
THEME_BACKGROUND_FULL_QUALITY = 82
THEME_BACKGROUND_THUMB_QUALITY = 72
THEME_BACKGROUND_OPTIMIZED_SUFFIX = ".webp"
THEME_BACKGROUND_THUMB_SUFFIX = ".thumb.webp"
THEME_BACKGROUND_TYPES = {
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": (".jpg", b"\xff\xd8\xff"),
    "image/webp": (".webp", b"RIFF"),
    "image/gif": (".gif", b"GIF8"),
}
LARK_SESSION_NAME = "staragent-lark"
LARK_CONFIG_PATH = state_dir() / "lark_config.json"
LARK_CONFIG_LOCK = file_lock(LARK_CONFIG_PATH)
LARK_ENV_NAMES = (
    "STARAGENT_LARK_APP_ID",
    "STARAGENT_LARK_APP_SECRET",
    "STARAGENT_LARK_ALLOWED_USERS",
    "STARAGENT_LARK_ALLOWED_CHATS",
    "STARAGENT_LARK_ALLOW_ALL",
    "STARAGENT_LARK_VERIFICATION_TOKEN",
    "STARAGENT_LARK_ENCRYPT_KEY",
    "STARAGENT_DASHBOARD_URL",
    "STARAGENT_AUTH_TOKEN",
    "STARAGENT_NODE_TOKEN",
    "STARAGENT_STATE_DIR",
    "STARAGENT_NODES",
)
LARK_EDITABLE_ENV_NAMES = (
    "STARAGENT_LARK_APP_ID",
    "STARAGENT_LARK_APP_SECRET",
    "STARAGENT_LARK_ALLOWED_USERS",
    "STARAGENT_LARK_ALLOWED_CHATS",
    "STARAGENT_LARK_ALLOW_ALL",
    "STARAGENT_LARK_VERIFICATION_TOKEN",
    "STARAGENT_LARK_ENCRYPT_KEY",
    "STARAGENT_DASHBOARD_URL",
    "STARAGENT_NODE_TOKEN",
)
LARK_OPENAPI_BASES = (
    ("Feishu", "https://open.feishu.cn"),
    ("Lark", "https://open.larksuite.com"),
)
LARK_SDK_CHECK_CACHE: dict[str, tuple[float, bool]] = {}
LARK_SDK_CHECK_TTL_SECONDS = 15.0
PROXY_ENV_NAMES = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)
TRUE_VALUES = {"1", "true", "yes"}


@contextlib.asynccontextmanager
async def dashboard_lifespan(app: FastAPI):
    with contextlib.suppress(OSError):
        append_hub_event(
            "info",
            "hub.started",
            "Hub application started.",
            source="hub.runtime",
            details={"pid": os.getpid()},
        )
    app.state.http_terminal_janitor = asyncio.create_task(http_terminal_janitor())
    app.state.node_heartbeat = asyncio.create_task(node_heartbeat_loop())
    try:
        yield
    finally:
        for task_name in ("http_terminal_janitor", "node_heartbeat"):
            task = getattr(app.state, task_name, None)
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for terminal_id in list(http_terminals):
            terminal = http_terminals.pop(terminal_id, None)
            if terminal:
                await close_http_terminal(terminal)
        with contextlib.suppress(OSError):
            append_hub_event(
                "info",
                "hub.stopped",
                "Hub application stopped gracefully.",
                source="hub.runtime",
                details={"pid": os.getpid()},
            )


def create_app() -> FastAPI:
    app = FastAPI(title="StarAgent", lifespan=dashboard_lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    register_auth_routes(app)
    register_theme_routes(app)
    register_pages_routes(app)
    register_terminal_routes(app)
    register_sessions_routes(app)
    register_nodes_routes(app)
    register_logs_routes(app)
    register_lark_routes(app)
    register_workspace_routes(app)
    return app


def register_auth_routes(app: FastAPI) -> None:
    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        if not auth_enabled() or is_public_path(request.url.path):
            response = await call_next(request)
            add_static_cache_header(request, response)
            return response
        if request_is_authenticated(request):
            response = await call_next(request)
            add_static_cache_header(request, response)
            return response
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


def register_theme_routes(app: FastAPI) -> None:
    @app.get("/api/theme")
    def theme_config() -> dict[str, object]:
        return theme_config_payload()

    @app.get("/api/theme/background", deprecated=True)
    def theme_background() -> FileResponse:
        selected = selected_theme_background()
        if not selected:
            raise HTTPException(status_code=404, detail="No theme background uploaded")
        background = selected["path"]
        media_type = mimetypes.guess_type(background.name)[0] or "application/octet-stream"
        return FileResponse(
            background,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/theme/backgrounds/{background_id}")
    def theme_background_by_id(background_id: str) -> FileResponse:
        entry = theme_background_by_id_entry(background_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Theme background not found")
        background = entry["path"]
        media_type = mimetypes.guess_type(background.name)[0] or "application/octet-stream"
        return FileResponse(
            background,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/api/theme/backgrounds/{background_id}/thumb")
    def theme_background_thumb_by_id(background_id: str) -> FileResponse:
        entry = theme_background_by_id_entry(background_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Theme background not found")
        thumb = entry["thumb_path"]
        media_type = mimetypes.guess_type(thumb.name)[0] or "application/octet-stream"
        return FileResponse(
            thumb,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.post("/api/theme/background")
    async def upload_theme_background(request: Request) -> dict[str, object]:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        data = await request.body()
        validate_theme_background(data, content_type)
        THEME_BACKGROUND_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        background_id = uuid.uuid4().hex[:12]
        write_optimized_theme_background(background_id, data)
        write_theme_config({"selected_background_id": background_id})
        prune_theme_background_library()
        return {"ok": True, **theme_config_payload()}

    @app.delete("/api/theme/background")
    def delete_theme_background() -> dict[str, object]:
        selected = selected_theme_background()
        if selected:
            delete_theme_background_id(str(selected["id"]))
        return {"ok": True, **theme_config_payload()}

    @app.delete("/api/theme/backgrounds/{background_id}")
    def delete_theme_background_by_id(background_id: str) -> dict[str, object]:
        delete_theme_background_id(background_id)
        return {"ok": True, **theme_config_payload()}

    @app.post("/api/theme/backgrounds/{background_id}/select")
    def select_theme_background(background_id: str) -> dict[str, object]:
        if not theme_background_by_id_entry(background_id):
            raise HTTPException(status_code=404, detail="Theme background not found")
        write_theme_config({"selected_background_id": background_id})
        return {"ok": True, **theme_config_payload()}


def register_pages_routes(app: FastAPI) -> None:
    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse("/sessions", status_code=303)

    @app.get("/sessions", response_class=HTMLResponse)
    def sessions_page(request: Request) -> HTMLResponse:
        node_views = sorted(
            collect_node_views(prefer_cached=True),
            key=lambda node: (not node.entry.is_local, node.name),
        )
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
                "command_presets": command_presets_payload(),
                "initial_explorer_path": str(Path.cwd()),
            },
        )

    @app.get("/nodes", response_class=HTMLResponse)
    def nodes_page(request: Request) -> HTMLResponse:
        node_views = collect_node_views(prefer_cached=True)
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

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request) -> HTMLResponse:
        agent_presets = [
            preset for preset in command_presets_payload() if preset.get("agent") != "shell"
        ]
        return templates.TemplateResponse(
            request,
            "agents.html",
            {
                "node_views": collect_node_views(prefer_cached=True),
                "agent_catalog": agent_catalog_payload(),
                "agent_presets": agent_presets,
            },
        )

    @app.get("/lark", response_class=HTMLResponse)
    def lark_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "lark.html",
            {"lark": lark_status_payload()},
        )

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "logs.html",
            {"log_sources": log_source_payloads()},
        )

    @app.get("/sessions/{name}", response_class=HTMLResponse)
    def session_detail(request: Request, name: str) -> HTMLResponse:
        view = node_session_view("local", name, prefer_cached=True)
        if view:
            return session_response(request, view)
        raise HTTPException(status_code=404, detail="Session not found")

    @app.get("/nodes/{node_id}/sessions/{name}", response_class=HTMLResponse)
    def node_session_detail(request: Request, node_id: str, name: str) -> HTMLResponse:
        view = node_session_view(node_id, name, prefer_cached=True)
        if view:
            return session_response(request, view)
        raise HTTPException(status_code=404, detail="Session not found")


def register_terminal_routes(app: FastAPI) -> None:
    # Compatibility route for pre-node-aware clients. New UI code uses /ws/nodes/... exclusively.
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


def register_sessions_routes(app: FastAPI) -> None:
    @app.post("/api/nodes/{node_id}/sessions/{name}/seen")
    def mark_node_session_seen(node_id: str, name: str) -> dict[str, object]:
        view = node_session_view(node_id, name)
        if not view:
            raise HTTPException(status_code=404, detail="Session not found")
        acknowledged = mark_hub_session_seen(view)
        return {"status": view.status, "acknowledged": acknowledged}

    @app.post("/api/sessions/{name}/send", deprecated=True)
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

    @app.post("/api/sessions/{name}/input", deprecated=True)
    def send_input(name: str, payload: TerminalInput) -> dict[str, str]:
        try:
            send_tmux_input(name, payload.data)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "sent"}

    @app.get("/api/sessions/{name}/output", deprecated=True)
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

    @app.get("/api/sessions/{name}/transcript-state", deprecated=True)
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


def register_nodes_routes(app: FastAPI) -> None:
    @app.post("/api/workers")
    def create_worker(payload: CreateWorkerRequest) -> dict[str, str]:
        try:
            node = node_by_name(payload.node)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {payload.node}") from exc
        command = payload.command
        if payload.resume:
            try:
                command = resume_worker_command(
                    payload.resume.agent,
                    payload.resume.id,
                    payload.cwd,
                    payload.command,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        worker = CreateWorker(name=payload.name, cwd=payload.cwd, command=command)
        if not node.is_local:
            return request_json(node, "POST", "/api/workers", worker.model_dump())
        try:
            start_tmux_worker(worker.name, worker.cwd, worker.command)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        append_node_event(
            node.name,
            "info",
            "session.created",
            f"Session {worker.name} was created.",
            source="hub.api",
            details={"session": worker.name, "cwd": worker.cwd},
        )
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
        append_node_event(
            node.name,
            "info",
            "session.adopted",
            f"Session {payload.name} was adopted.",
            source="hub.api",
            details={"session": payload.name},
        )
        return {"status": "adopted", "session": adopted.as_dict()}

    @app.delete("/api/sessions/{name}", deprecated=True)
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
        append_node_event(
            node.name,
            "info",
            "session.stopped",
            f"Session {name} was stopped.",
            source="hub.api",
            details={"session": name},
        )
        return {"status": "stopped", "name": name}

    @app.post("/api/nodes")
    def create_node(payload: NodeRequest) -> dict[str, str]:
        try:
            node = add_node(payload.name, payload.url, payload.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        append_hub_event(
            "info",
            "node.added",
            f"Node {node.name} was added.",
            source="hub.api",
            details={"node": node.name, "endpoint": node.url or "local", "mode": node.mode},
        )
        return {"status": "created", "name": node.name, "url": node.url or "local"}

    @app.get("/api/nodes")
    def list_nodes() -> dict[str, list[dict[str, object]]]:
        return {"nodes": [node_payload(node) for node in collect_node_views()]}

    @app.get("/api/nodes/{node_id}/agent-tools")
    def node_agent_tools(node_id: str, refresh: bool = False) -> dict[str, object]:
        try:
            node = node_by_name(node_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}") from exc
        return node_agent_tools_payload(node, refresh=refresh)

    @app.post("/api/nodes/{node_id}/agent-tools/{agent}/update")
    def update_node_agent_tool(node_id: str, agent: str) -> dict[str, object]:
        try:
            node = node_by_name(node_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}") from exc
        try:
            result = node_agent_tool_update_payload(node, agent)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AgentToolUpdateBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        append_hub_event(
            "info" if result.get("ok") else "warning",
            "agent.update_succeeded" if result.get("ok") else "agent.update_failed",
            f"{result.get('label') or agent} update "
            f"{'completed' if result.get('ok') else 'failed'} on {node.name}.",
            source="hub.agents",
            details={
                "node": node.name,
                "agent": result.get("agent") or agent,
                "before_version": result.get("before_version") or "",
                "after_version": result.get("after_version") or "",
                "changed": bool(result.get("changed")),
                "error": result.get("error") or "",
            },
        )
        return result

    @app.get("/api/nodes/{node_id}/agent-history")
    def node_agent_history(
        node_id: str,
        agent: str = "",
        limit: int = 50,
        refresh: bool = False,
    ) -> dict[str, object]:
        try:
            node = node_by_name(node_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node_id}") from exc
        try:
            return node_agent_history_payload(
                node,
                agent=agent,
                limit=limit,
                refresh=refresh,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tailscale/hub")
    def tailscale_hub_status() -> dict[str, object]:
        return tailscale_hub_payload()


def register_logs_routes(app: FastAPI) -> None:
    @app.get("/api/logs")
    def logs(
        source: str = "hub", level: str = "", q: str = "", limit: int = 200
    ) -> dict[str, object]:
        sources = log_source_payloads()
        selected = next((item for item in sources if item["id"] == source), None)
        if selected is None:
            raise HTTPException(status_code=404, detail=f"log source not found: {source}")
        events = (
            read_hub_events(limit=limit, level=level, query=q)
            if selected["kind"] == "hub"
            else read_node_events(str(selected["name"]), limit=limit, level=level, query=q)
        )
        return {
            "source": source,
            "sources": sources,
            "events": events,
            "latest_id": str(events[0].get("id") or "") if events else "",
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }


def register_lark_routes(app: FastAPI) -> None:
    @app.get("/api/lark/status")
    def lark_status() -> dict[str, object]:
        return lark_status_payload()

    @app.get("/api/lark", deprecated=True)
    def lark_status_alias() -> dict[str, object]:
        return lark_status_payload()

    @app.post("/api/lark/test")
    def test_lark_connection() -> dict[str, object]:
        return lark_connection_test_payload()

    @app.post("/api/lark/config")
    def save_lark_config(payload: LarkConfigRequest) -> dict[str, object]:
        try:
            write_lark_config(payload)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "saved", "lark": lark_status_payload()}

    @app.delete("/api/lark/config")
    def clear_lark_config() -> dict[str, object]:
        try:
            clear_saved_lark_config()
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "cleared", "lark": lark_status_payload()}

    @app.post("/api/lark/start")
    def start_lark() -> dict[str, object]:
        status = lark_status_payload()
        if status["worker"]["running"]:
            return {"status": "running", "lark": status}
        missing = status["config"]["missing_required"]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required Lark environment: {', '.join(missing)}",
            )
        try:
            start_tmux_worker(
                LARK_SESSION_NAME,
                str(PROJECT_ROOT),
                lark_worker_command(),
                keep_shell_on_exit=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "started", "lark": lark_status_payload()}

    @app.post("/api/lark/stop")
    def stop_lark() -> dict[str, object]:
        if not tmux_session_exists(LARK_SESSION_NAME):
            return {"status": "stopped", "lark": lark_status_payload()}
        try:
            kill_tmux_session(LARK_SESSION_NAME)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "stopped", "lark": lark_status_payload()}

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


def register_workspace_routes(app: FastAPI) -> None:
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
        append_hub_event(
            "info",
            "node.removed",
            f"Node {node_id} was removed.",
            source="hub.api",
            details={"node": node_id},
        )
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
            try:
                return request_json(node_entry, "GET", suffix)
            except (OSError, urllib.error.URLError) as exc:
                raise remote_request_exception(exc) from exc
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
            try:
                return request_json(node_entry, "POST", suffix, payload.model_dump())
            except (OSError, urllib.error.URLError) as exc:
                raise remote_request_exception(exc) from exc
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
            try:
                return request_json(node_entry, "GET", suffix)
            except (OSError, urllib.error.URLError) as exc:
                raise remote_request_exception(exc) from exc
        try:
            return file_preview_payload(path, root=root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/files/raw")
    def file_raw(path: str, node: str = "local", root: str | None = None) -> Response:
        try:
            node_entry = node_by_name(node)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node}") from exc
        if not node_entry.is_local:
            suffix = f"/api/files/raw?path={urllib.parse.quote(path)}"
            if root:
                suffix += f"&root={urllib.parse.quote(root)}"
            try:
                body, media_type = request_raw(node_entry, "GET", suffix)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except (OSError, urllib.error.URLError) as exc:
                raise remote_request_exception(exc, raw_file=True) from exc
            return Response(content=body, media_type=media_type)
        try:
            body, media_type = file_raw_payload(path, root=root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(content=body, media_type=media_type)

    @app.get("/api/files/raw-info")
    def file_raw_info(path: str, node: str = "local", root: str | None = None) -> dict[str, object]:
        try:
            node_entry = node_by_name(node)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"node not found: {node}") from exc
        if not node_entry.is_local:
            suffix = f"/api/files/raw-info?path={urllib.parse.quote(path)}"
            if root:
                suffix += f"&root={urllib.parse.quote(root)}"
            try:
                return request_json(node_entry, "GET", suffix)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except (OSError, urllib.error.URLError) as exc:
                raise remote_request_exception(exc, raw_file=True) from exc
        try:
            return file_raw_info_payload(path, root=root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def legacy_theme_background_path() -> Path | None:
    for suffix, _signature in THEME_BACKGROUND_TYPES.values():
        candidate = THEME_BACKGROUND_DIR / f"{THEME_BACKGROUND_STEM}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def clear_theme_background() -> None:
    for suffix, _signature in THEME_BACKGROUND_TYPES.values():
        candidate = THEME_BACKGROUND_DIR / f"{THEME_BACKGROUND_STEM}{suffix}"
        if candidate.exists():
            candidate.unlink()


def read_theme_config() -> dict[str, str]:
    with locked_file(THEME_BACKGROUND_CONFIG_PATH):
        raw = read_json(THEME_BACKGROUND_CONFIG_PATH, {})
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if isinstance(value, str)}


def write_theme_config(config: dict[str, str]) -> None:
    with locked_file(THEME_BACKGROUND_CONFIG_PATH):
        atomic_write_json(THEME_BACKGROUND_CONFIG_PATH, config)


def migrate_legacy_theme_background() -> None:
    legacy = legacy_theme_background_path()
    if not legacy:
        return
    config = read_theme_config()
    selected_id = config.get("selected_background_id", "")
    suffixes = {suffix for suffix, _signature in THEME_BACKGROUND_TYPES.values()}
    if selected_id and any(
        (THEME_BACKGROUND_LIBRARY_DIR / f"{selected_id}{suffix}").is_file() for suffix in suffixes
    ):
        clear_theme_background()
        return
    THEME_BACKGROUND_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    background_id = uuid.uuid4().hex[:12]
    write_optimized_theme_background(background_id, legacy.read_bytes())
    write_theme_config({"selected_background_id": background_id})
    clear_theme_background()


def theme_background_full_path(background_id: str) -> Path:
    return THEME_BACKGROUND_LIBRARY_DIR / f"{background_id}{THEME_BACKGROUND_OPTIMIZED_SUFFIX}"


def theme_background_thumb_path(background_id: str) -> Path:
    return THEME_BACKGROUND_LIBRARY_DIR / f"{background_id}{THEME_BACKGROUND_THUMB_SUFFIX}"


def normalize_theme_background_file(path: Path) -> Path | None:
    if path.name.endswith(THEME_BACKGROUND_THUMB_SUFFIX):
        return None
    background_id = path.stem
    if not THEME_BACKGROUND_ID_PATTERN.fullmatch(background_id):
        return None
    full = theme_background_full_path(background_id)
    thumb = theme_background_thumb_path(background_id)
    if path == full and thumb.is_file():
        return full
    try:
        write_optimized_theme_background(background_id, path.read_bytes())
    except HTTPException:
        return path if path.is_file() else None
    if path != full:
        path.unlink(missing_ok=True)
    return full


def theme_background_entries() -> list[dict[str, object]]:
    migrate_legacy_theme_background()
    entries: list[dict[str, object]] = []
    if not THEME_BACKGROUND_LIBRARY_DIR.is_dir():
        return entries
    suffixes = {suffix for suffix, _signature in THEME_BACKGROUND_TYPES.values()}
    seen_ids: set[str] = set()
    for path in THEME_BACKGROUND_LIBRARY_DIR.iterdir():
        background_id = path.stem
        if path.name.endswith(THEME_BACKGROUND_THUMB_SUFFIX):
            continue
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if not THEME_BACKGROUND_ID_PATTERN.fullmatch(background_id):
            continue
        if background_id in seen_ids:
            if path.suffix.lower() != THEME_BACKGROUND_OPTIMIZED_SUFFIX:
                path.unlink(missing_ok=True)
            continue
        path = normalize_theme_background_file(path) or path
        seen_ids.add(background_id)
        stat = path.stat()
        thumb_path = theme_background_thumb_path(background_id)
        entries.append(
            {
                "id": background_id,
                "path": path,
                "thumb_path": thumb_path if thumb_path.is_file() else path,
                "url": f"/api/theme/backgrounds/{background_id}",
                "thumb_url": f"/api/theme/backgrounds/{background_id}/thumb",
                "mtime": int(stat.st_mtime),
                "thumb_mtime": int(thumb_path.stat().st_mtime)
                if thumb_path.is_file()
                else int(stat.st_mtime),
                "size": stat.st_size,
                "content_type": "image/webp",
            }
        )
    entries.sort(key=lambda entry: int(entry["mtime"]), reverse=True)
    return entries


def theme_background_by_id_entry(background_id: str) -> dict[str, object] | None:
    if not THEME_BACKGROUND_ID_PATTERN.fullmatch(background_id):
        return None
    for entry in theme_background_entries():
        if entry["id"] == background_id:
            return entry
    return None


def selected_theme_background(
    entries: list[dict[str, object]] | None = None,
) -> dict[str, object] | None:
    config = read_theme_config()
    selected_id = config.get("selected_background_id", "")
    entries = theme_background_entries() if entries is None else entries
    if selected_id:
        selected = next((entry for entry in entries if entry["id"] == selected_id), None)
        if selected:
            return selected
    if not entries:
        if selected_id:
            write_theme_config({})
        return None
    selected = entries[0]
    write_theme_config({"selected_background_id": str(selected["id"])})
    return selected


def theme_background_public_payload(entry: dict[str, object]) -> dict[str, object]:
    return {
        "id": entry["id"],
        "url": entry["url"],
        "thumb_url": entry["thumb_url"],
        "mtime": entry["mtime"],
        "thumb_mtime": entry["thumb_mtime"],
        "size": entry["size"],
        "content_type": entry["content_type"],
    }


def theme_config_payload() -> dict[str, object]:
    entries = theme_background_entries()
    selected = selected_theme_background(entries)
    selected_id = str(selected["id"]) if selected else ""
    return {
        "background_url": selected["url"] if selected else "",
        "background_mtime": selected["mtime"] if selected else 0,
        "selected_background_id": selected_id,
        "backgrounds": [theme_background_public_payload(entry) for entry in entries],
    }


def prune_theme_background_library() -> None:
    entries = theme_background_entries()
    selected = selected_theme_background(entries)
    selected_id = str(selected["id"]) if selected else ""
    for entry in entries[THEME_BACKGROUND_LIBRARY_LIMIT:]:
        if entry["id"] == selected_id:
            continue
        path = entry["path"]
        if isinstance(path, Path):
            path.unlink(missing_ok=True)
        thumb_path = entry.get("thumb_path")
        if isinstance(thumb_path, Path):
            thumb_path.unlink(missing_ok=True)


def delete_theme_background_id(background_id: str) -> None:
    entry = theme_background_by_id_entry(background_id)
    if not entry:
        return
    path = entry["path"]
    if isinstance(path, Path):
        path.unlink(missing_ok=True)
    thumb_path = entry.get("thumb_path")
    if isinstance(thumb_path, Path):
        thumb_path.unlink(missing_ok=True)
    selected_id = read_theme_config().get("selected_background_id", "")
    if selected_id == background_id:
        remaining = theme_background_entries()
        if remaining:
            write_theme_config({"selected_background_id": str(remaining[0]["id"])})
        else:
            write_theme_config({})


def prepare_theme_image(data: bytes):
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="Pillow is required to process theme backgrounds",
        ) from exc
    try:
        image = Image.open(io.BytesIO(data))
        if getattr(image, "is_animated", False):
            image.seek(0)
        image = ImageOps.exif_transpose(image)
        image.load()
    except (OSError, UnidentifiedImageError) as exc:
        raise HTTPException(status_code=400, detail="Uploaded image could not be decoded") from exc
    if image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    return image


def theme_image_to_webp_bytes(image, max_edge: int, quality: int) -> bytes:
    try:
        from PIL import Image
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="Pillow is required to process theme backgrounds",
        ) from exc
    optimized = image.copy()
    optimized.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    if optimized.mode not in {"RGB", "RGBA"}:
        optimized = optimized.convert("RGBA" if "A" in optimized.getbands() else "RGB")
    output = io.BytesIO()
    optimized.save(output, format="WEBP", quality=quality, method=6)
    return output.getvalue()


def write_optimized_theme_background(background_id: str, data: bytes) -> None:
    image = prepare_theme_image(data)
    THEME_BACKGROUND_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        theme_background_full_path(background_id),
        theme_image_to_webp_bytes(
            image,
            THEME_BACKGROUND_FULL_MAX_EDGE,
            THEME_BACKGROUND_FULL_QUALITY,
        ),
    )
    atomic_write_bytes(
        theme_background_thumb_path(background_id),
        theme_image_to_webp_bytes(
            image,
            THEME_BACKGROUND_THUMB_MAX_EDGE,
            THEME_BACKGROUND_THUMB_QUALITY,
        ),
    )


def validate_theme_background(data: bytes, content_type: str) -> str:
    if not data:
        raise HTTPException(status_code=400, detail="Empty image upload")
    if len(data) > THEME_BACKGROUND_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Theme background must be 8 MiB or smaller")
    if content_type in THEME_BACKGROUND_TYPES:
        suffix, signature = THEME_BACKGROUND_TYPES[content_type]
        if not data.startswith(signature):
            raise HTTPException(
                status_code=400, detail="Uploaded image does not match its content type"
            )
        if content_type == "image/webp" and data[8:12] != b"WEBP":
            raise HTTPException(status_code=400, detail="Uploaded image is not a valid WebP file")
        return suffix
    for media_type, (suffix, signature) in THEME_BACKGROUND_TYPES.items():
        if data.startswith(signature) and (media_type != "image/webp" or data[8:12] == b"WEBP"):
            return suffix
    if data.startswith(b"RIFF") and data[8:12] != b"WEBP":
        raise HTTPException(status_code=400, detail="Uploaded image is not a valid WebP file")
    raise HTTPException(status_code=400, detail="Use PNG, JPEG, WebP, or GIF")


def auth_token() -> str:
    return stored_hub_auth_token()


def auth_enabled() -> bool:
    return bool(auth_token())


def valid_token(value: str) -> bool:
    token = auth_token()
    return bool(token) and hmac.compare_digest(value or "", token)


def is_public_path(path: str) -> bool:
    return path == "/login" or path.startswith("/static/")


def add_static_cache_header(request: Request, response) -> None:
    if request.url.path.startswith("/static/") and response.status_code == 200:
        cache_control = (
            "public, max-age=31536000, immutable"
            if request.query_params.get("v")
            else "public, max-age=86400"
        )
        response.headers.setdefault("Cache-Control", cache_control)


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


async def node_heartbeat_loop() -> None:
    while True:
        await asyncio.to_thread(refresh_remote_node_heartbeats)
        await asyncio.sleep(NODE_HEARTBEAT_INTERVAL_SECONDS)


async def proxy_terminal_socket(websocket: WebSocket, node: NodeEntry, name: str) -> None:
    await websocket.accept()
    remote_url = websocket_url(node, f"/ws/sessions/{urllib.parse.quote(name)}/terminal")
    try:
        async with websockets.connect(remote_url, **websocket_connect_kwargs(node)) as remote:
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


def websocket_connect_kwargs(node: NodeEntry) -> dict[str, object]:
    if node.mode != "lan":
        return {}
    try:
        parameters = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):
        return {}
    if "proxy" in parameters:
        return {"proxy": None}
    return {}


class ResumeConversationRequest(BaseModel):
    agent: str
    id: str


class CreateWorkerRequest(BaseModel):
    node: str = "local"
    name: str
    cwd: str
    command: str
    resume: ResumeConversationRequest | None = None


class LarkConfigRequest(BaseModel):
    app_id: str = ""
    app_secret: str = ""
    allowed_chats: str = ""
    allowed_users: str = ""
    allow_all: bool = False
    verification_token: str = ""
    encrypt_key: str = ""
    dashboard_url: str = ""
    node_token: str = ""


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


def node_session_view(node_id: str, name: str, *, prefer_cached: bool = False):
    try:
        node = node_by_name(node_id)
    except KeyError:
        return None
    return collect_node_session(node, name, prefer_cached=prefer_cached)


def request_is_speculative_navigation(request: Request) -> bool:
    purpose = " ".join(
        str(request.headers.get(name) or "")
        for name in ("purpose", "sec-purpose", "sec-fetch-purpose")
    ).lower()
    return "prefetch" in purpose or "prerender" in purpose


def session_response(request: Request, view) -> HTMLResponse:
    if not request_is_speculative_navigation(request):
        mark_hub_session_seen(view)
    attach_command = tmux_attach_command(view)
    sidebar_nodes = collect_session_navigation_nodes()
    return templates.TemplateResponse(
        request,
        "session.html",
        {
            "view": view,
            "relative_time": relative_time,
            "attach_command": attach_command,
            "tmux_commands": tmux_quick_commands(view),
            "initial_token_usage": None,
            "sidebar_nodes": sidebar_nodes,
            "sidebar_session_count": sum(len(node.sessions) for node in sidebar_nodes),
        },
    )


def dashboard_stats(node_views, views):
    return {
        "nodes": len(node_views),
        "connected_nodes": sum(1 for node in node_views if node.status in {"connected", "stale"}),
        "disconnected_nodes": sum(1 for node in node_views if node.status == "disconnected"),
        "total": len(views),
        "working": sum(1 for view in views if view.status == "working"),
        "review": sum(1 for view in views if view.status == "review"),
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


def log_source_payloads() -> list[dict[str, str]]:
    active_nodes = {node.name: node for node in load_nodes()}
    node_names = sorted({*active_nodes, *archived_node_names()})
    sources = [{"id": "hub", "name": "hub", "label": "Hub", "kind": "hub"}]
    for name in node_names:
        node = active_nodes.get(name)
        suffix = " · archived" if node is None else ""
        sources.append(
            {
                "id": f"node:{name}",
                "name": name,
                "label": f"{name}{suffix}",
                "kind": "node",
            }
        )
    return sources


def lark_status_payload() -> dict[str, object]:
    saved = read_lark_config()
    values = lark_effective_env(saved)
    app_id = values.get("STARAGENT_LARK_APP_ID", "")
    app_secret = values.get("STARAGENT_LARK_APP_SECRET", "")
    allowed_users = values.get("STARAGENT_LARK_ALLOWED_USERS", "")
    allowed_chats = values.get("STARAGENT_LARK_ALLOWED_CHATS", "")
    verification_token = values.get("STARAGENT_LARK_VERIFICATION_TOKEN", "")
    encrypt_key = values.get("STARAGENT_LARK_ENCRYPT_KEY", "")
    dashboard_url = values.get("STARAGENT_DASHBOARD_URL", "")
    auth_token = values.get("STARAGENT_AUTH_TOKEN", "") or stored_hub_auth_token()
    node_token = values.get("STARAGENT_NODE_TOKEN", "")
    allow_all = truthy(values.get("STARAGENT_LARK_ALLOW_ALL", ""))
    access_configured = bool(allowed_users or allowed_chats or allow_all)
    config_items = [
        config_item(
            "STARAGENT_LARK_APP_ID",
            "App ID",
            bool(app_id),
            True,
            masked_value(app_id),
            config_source(saved, "STARAGENT_LARK_APP_ID"),
        ),
        config_item(
            "STARAGENT_LARK_APP_SECRET",
            "App Secret",
            bool(app_secret),
            True,
            masked_value(app_secret),
            config_source(saved, "STARAGENT_LARK_APP_SECRET"),
        ),
        config_item(
            "STARAGENT_LARK_ALLOWED_CHATS",
            "Allowed chats",
            bool(allowed_chats),
            False,
            count_csv(allowed_chats),
            config_source(saved, "STARAGENT_LARK_ALLOWED_CHATS"),
        ),
        config_item(
            "STARAGENT_LARK_ALLOWED_USERS",
            "Allowed users",
            bool(allowed_users),
            False,
            count_csv(allowed_users),
            config_source(saved, "STARAGENT_LARK_ALLOWED_USERS"),
        ),
        config_item(
            "STARAGENT_LARK_ALLOW_ALL",
            "Allow all",
            allow_all,
            False,
            "enabled" if allow_all else "",
            config_source(saved, "STARAGENT_LARK_ALLOW_ALL"),
        ),
        config_item(
            "STARAGENT_LARK_VERIFICATION_TOKEN",
            "Verification token",
            bool(verification_token),
            False,
            masked_value(verification_token),
            config_source(saved, "STARAGENT_LARK_VERIFICATION_TOKEN"),
        ),
        config_item(
            "STARAGENT_LARK_ENCRYPT_KEY",
            "Encrypt key",
            bool(encrypt_key),
            False,
            masked_value(encrypt_key),
            config_source(saved, "STARAGENT_LARK_ENCRYPT_KEY"),
        ),
        config_item(
            "STARAGENT_DASHBOARD_URL",
            "Dashboard URL",
            bool(dashboard_url),
            False,
            dashboard_url,
            config_source(saved, "STARAGENT_DASHBOARD_URL"),
        ),
        config_item(
            "STARAGENT_AUTH_TOKEN",
            "Hub auth token",
            bool(auth_token),
            False,
            masked_value(auth_token),
            config_source(saved, "STARAGENT_AUTH_TOKEN"),
        ),
        config_item(
            "STARAGENT_NODE_TOKEN",
            "Node token",
            bool(node_token),
            False,
            masked_value(node_token),
            config_source(saved, "STARAGENT_NODE_TOKEN"),
        ),
    ]
    missing_required = [
        item["name"] for item in config_items if item["required"] and not item["present"]
    ]
    if not access_configured:
        missing_required.append("STARAGENT_LARK_ALLOWED_CHATS or STARAGENT_LARK_ALLOWED_USERS")
    worker_running = tmux_session_exists(LARK_SESSION_NAME)
    worker_output = (
        strip_ansi(capture_tmux_pane_ansi(LARK_SESSION_NAME, lines=80)) if worker_running else ""
    )
    venv_executable = lark_executable()
    sdk_installed = lark_sdk_installed(venv_executable)
    python_executable = lark_python_executable(venv_executable)
    return {
        "config": {
            "items": config_items,
            "access_configured": access_configured,
            "missing_required": missing_required,
            "path": str(LARK_CONFIG_PATH),
        },
        "sdk": {
            "installed": sdk_installed,
            "venv_executable": str(venv_executable),
            "venv_ready": venv_executable.exists(),
            "python_executable": str(python_executable) if python_executable else "",
        },
        "worker": {
            "session": LARK_SESSION_NAME,
            "session_url": f"/nodes/local/sessions/{urllib.parse.quote(LARK_SESSION_NAME)}",
            "running": worker_running,
            "status": "running" if worker_running else "stopped",
            "recent_output": worker_output[-4000:],
        },
        "form": {
            "app_id": app_id,
            "allowed_chats": allowed_chats,
            "allowed_users": allowed_users,
            "allow_all": allow_all,
            "dashboard_url": dashboard_url,
            "secrets": {
                "app_secret": bool(app_secret),
                "verification_token": bool(verification_token),
                "encrypt_key": bool(encrypt_key),
                "node_token": bool(node_token),
            },
        },
        "commands": {
            "install": "pip install -e '.[lark]'",
            "start": lark_display_command(),
            "tmux": f"tmux attach -t {LARK_SESSION_NAME}",
            "scopes": "\n".join(
                [
                    "im:message:send_as_bot",
                    "im:message.p2p_msg:readonly",
                    "im:message.group_at_msg:readonly",
                    "Send and delete message reaction",
                ]
            ),
        },
    }


def config_item(
    name: str,
    label: str,
    present: bool,
    required: bool,
    value: str = "",
    source: str = "",
) -> dict[str, object]:
    return {
        "name": name,
        "label": label,
        "present": present,
        "required": required,
        "value": value,
        "source": source,
    }


def lark_connection_test_payload() -> dict[str, object]:
    values = lark_effective_env()
    app_id = values.get("STARAGENT_LARK_APP_ID", "")
    app_secret = values.get("STARAGENT_LARK_APP_SECRET", "")
    checked_at = datetime.now(UTC).astimezone().isoformat()
    steps: list[dict[str, object]] = []

    if not app_id or not app_secret:
        missing = []
        if not app_id:
            missing.append("App ID")
        if not app_secret:
            missing.append("App Secret")
        steps.append(
            test_step(
                "Configuration",
                False,
                f"Missing {', '.join(missing)}.",
            )
        )
        return {
            "ok": False,
            "status": "failed",
            "checked_at": checked_at,
            "base_url": "",
            "bot": {},
            "steps": steps,
        }

    steps.append(test_step("Configuration", True, "App ID and App Secret are configured."))
    token = ""
    base_url = ""
    provider = ""
    token_attempt_details = []

    for provider_name, candidate_base in LARK_OPENAPI_BASES:
        response = lark_openapi_json(
            candidate_base,
            "/open-apis/auth/v3/tenant_access_token/internal",
            body={"app_id": app_id, "app_secret": app_secret},
        )
        if is_lark_success(response) and isinstance(response.get("tenant_access_token"), str):
            token = str(response["tenant_access_token"])
            base_url = candidate_base
            provider = provider_name
            expire = response.get("expire")
            detail = f"Tenant access token acquired from {provider_name} OpenAPI."
            if expire:
                detail += f" Expires in {expire}s."
            steps.append(test_step("Credentials", True, detail, target=candidate_base))
            break
        token_attempt_details.append(f"{provider_name}: {lark_response_detail(response)}")

    if not token:
        steps.append(
            test_step(
                "Credentials",
                False,
                "Could not acquire tenant access token. " + " | ".join(token_attempt_details),
            )
        )
        return {
            "ok": False,
            "status": "failed",
            "checked_at": checked_at,
            "base_url": "",
            "bot": {},
            "steps": steps,
        }

    bot_response = lark_openapi_json(
        base_url,
        "/open-apis/bot/v3/info",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    if not is_lark_success(bot_response):
        steps.append(
            test_step(
                "Bot info",
                False,
                lark_response_detail(bot_response),
                target=f"{base_url}/open-apis/bot/v3/info",
            )
        )
        return {
            "ok": False,
            "status": "failed",
            "checked_at": checked_at,
            "base_url": base_url,
            "bot": {},
            "steps": steps,
        }

    bot = lark_bot_info(bot_response)
    label = bot.get("name") or bot.get("app_name") or app_id
    steps.append(
        test_step(
            "Bot info",
            True,
            f"Bot information loaded from {provider}. Name: {label}.",
            target=f"{base_url}/open-apis/bot/v3/info",
        )
    )
    worker_running = tmux_session_exists(LARK_SESSION_NAME)
    steps.append(
        test_step(
            "Worker",
            worker_running,
            "Lark worker is running." if worker_running else "Lark worker is not running yet.",
            status="passed" if worker_running else "warning",
        )
    )
    return {
        "ok": True,
        "status": "passed" if worker_running else "warning",
        "checked_at": checked_at,
        "base_url": base_url,
        "bot": bot,
        "steps": steps,
    }


def test_step(
    name: str,
    ok: bool,
    detail: str,
    *,
    status: str | None = None,
    target: str = "",
) -> dict[str, object]:
    return {
        "name": name,
        "ok": ok,
        "status": status or ("passed" if ok else "failed"),
        "detail": detail,
        "target": target,
    }


def lark_openapi_json(
    base_url: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    method: str = "POST",
    timeout: float = 8.0,
) -> dict[str, object]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
            payload = json.loads(text) if text.strip() else {}
            return (
                payload if isinstance(payload, dict) else {"code": -1, "msg": "invalid JSON body"}
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(detail)
        except json.JSONDecodeError:
            payload = {"msg": detail.strip() or exc.reason}
        if isinstance(payload, dict):
            payload.setdefault("http_status", exc.code)
            return payload
        return {"code": -1, "http_status": exc.code, "msg": str(payload)}
    except (OSError, TimeoutError) as exc:
        return {"code": -1, "msg": str(exc)}
    except json.JSONDecodeError as exc:
        return {"code": -1, "msg": f"invalid JSON response: {exc}"}


def is_lark_success(response: dict[str, object]) -> bool:
    return response.get("code") in {0, "0"}


def lark_response_detail(response: dict[str, object]) -> str:
    code = response.get("code")
    msg = response.get("msg") or response.get("message") or "Lark OpenAPI request failed"
    http_status = response.get("http_status")
    if http_status:
        return f"HTTP {http_status}: {msg}"
    if code is not None:
        return f"code={code}: {msg}"
    return str(msg)


def lark_bot_info(response: dict[str, object]) -> dict[str, object]:
    data = response.get("bot") or response.get("data") or {}
    if not isinstance(data, dict):
        return {}
    fields = (
        "app_name",
        "avatar_url",
        "bot_id",
        "name",
        "open_id",
        "union_id",
        "activate_status",
    )
    return {field: data[field] for field in fields if data.get(field)}


def read_lark_config() -> dict[str, str]:
    with LARK_CONFIG_LOCK:
        data = read_json(LARK_CONFIG_PATH, {})
    if not isinstance(data, dict):
        return {}
    config: dict[str, str] = {}
    for name in LARK_EDITABLE_ENV_NAMES:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            config[name] = value.strip()
    return config


def write_lark_config(payload: LarkConfigRequest) -> None:
    with LARK_CONFIG_LOCK:
        current = read_lark_config()
        next_config = dict(current)
        text_fields = {
            "STARAGENT_LARK_APP_ID": payload.app_id,
            "STARAGENT_LARK_ALLOWED_CHATS": payload.allowed_chats,
            "STARAGENT_LARK_ALLOWED_USERS": payload.allowed_users,
            "STARAGENT_DASHBOARD_URL": payload.dashboard_url,
        }
        for name, value in text_fields.items():
            set_or_remove_lark_config_value(next_config, name, value)
        for name, value in {
            "STARAGENT_LARK_APP_SECRET": payload.app_secret,
            "STARAGENT_LARK_VERIFICATION_TOKEN": payload.verification_token,
            "STARAGENT_LARK_ENCRYPT_KEY": payload.encrypt_key,
            "STARAGENT_NODE_TOKEN": payload.node_token,
        }.items():
            if value.strip():
                next_config[name] = value.strip()
        if payload.allow_all:
            next_config["STARAGENT_LARK_ALLOW_ALL"] = "1"
        else:
            next_config.pop("STARAGENT_LARK_ALLOW_ALL", None)

        try:
            atomic_write_json(LARK_CONFIG_PATH, next_config, mode=0o600)
        except OSError as exc:
            raise RuntimeError(f"failed to save Lark config: {exc}") from exc


def clear_saved_lark_config() -> None:
    with LARK_CONFIG_LOCK:
        try:
            LARK_CONFIG_PATH.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError(f"failed to clear Lark config: {exc}") from exc


def set_or_remove_lark_config_value(config: dict[str, str], name: str, value: str) -> None:
    value = value.strip()
    if value:
        config[name] = value
    else:
        config.pop(name, None)


def lark_effective_env(saved: dict[str, str] | None = None) -> dict[str, str]:
    saved = read_lark_config() if saved is None else saved
    values: dict[str, str] = {}
    for name in LARK_ENV_NAMES:
        value = saved.get(name) or os.environ.get(name, "").strip()
        if value:
            values[name] = value
    return values


def config_source(saved: dict[str, str], name: str) -> str:
    if saved.get(name):
        return "saved"
    if os.environ.get(name, "").strip():
        return "environment"
    if name == "STARAGENT_AUTH_TOKEN":
        return hub_auth_token_source()
    return ""


def masked_value(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def count_csv(value: str) -> str:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        return ""
    return f"{len(items)} configured"


def truthy(value: str) -> bool:
    return value.strip().lower() in TRUE_VALUES


def lark_executable() -> Path:
    local = PROJECT_ROOT / ".venv-lark" / "bin" / "staragent"
    if local.exists():
        return local
    found = shutil.which("staragent")
    return Path(found).resolve() if found else Path("staragent")


def lark_python_executable(staragent_executable: Path | None = None) -> Path | None:
    executable = staragent_executable or lark_executable()
    if executable.name.startswith("python") and executable.exists():
        return executable
    if executable.name == "staragent" and executable.parent != Path("."):
        candidate = executable.parent / "python"
        if candidate.exists():
            return candidate
    return None


def lark_sdk_installed(staragent_executable: Path | None = None) -> bool:
    python_executable = lark_python_executable(staragent_executable)
    if not python_executable:
        return importlib.util.find_spec("lark_oapi") is not None

    cache_key = str(python_executable)
    now = datetime.now().timestamp()
    cached = LARK_SDK_CHECK_CACHE.get(cache_key)
    if cached and now - cached[0] <= LARK_SDK_CHECK_TTL_SECONDS:
        return cached[1]

    try:
        result = subprocess.run(
            [
                str(python_executable),
                "-c",
                (
                    "import importlib.util, sys; "
                    "sys.exit(0 if importlib.util.find_spec('lark_oapi') else 1)"
                ),
            ],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        installed = False
    else:
        installed = result.returncode == 0
    LARK_SDK_CHECK_CACHE[cache_key] = (now, installed)
    return installed


def lark_display_command() -> str:
    return f"cd {shlex.quote(str(PROJECT_ROOT))} && {shlex.quote(str(lark_executable()))} lark"


def lark_worker_command() -> str:
    values = lark_effective_env()
    values.setdefault("STARAGENT_STATE_DIR", str(state_dir()))
    env_parts = [f"PATH={shlex.quote(os.environ.get('PATH', ''))}"]
    for name in PROXY_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            env_parts.append(f"{name}={shlex.quote(value)}")
    for name in LARK_ENV_NAMES:
        if name == "STARAGENT_AUTH_TOKEN":
            continue
        value = values.get(name)
        if value:
            env_parts.append(f"{name}={shlex.quote(value)}")
    return f"{' '.join(env_parts)} {shlex.quote(str(lark_executable()))} lark"


def is_agent_session(node_id: str, name: str) -> bool:
    for session in collect_hub_sessions():
        if session.node_id == node_id and session.name == name:
            return session.session_type == "agent"
    return True


def chat_key(node_id: str, session: str) -> str:
    return f"{node_id}/{session}"


def load_chat_histories() -> dict[str, list[dict[str, object]]]:
    with CHAT_HISTORY_LOCK:
        data = read_json(CHAT_HISTORY_PATH, {})
        return data if isinstance(data, dict) else {}


def save_chat_histories(data: dict[str, list[dict[str, object]]]) -> None:
    with CHAT_HISTORY_LOCK:
        atomic_write_json(CHAT_HISTORY_PATH, data)


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
        messages = replace_chat_history_from_transcript(node_id, session, state.messages)
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
        try:
            body = request_json(
                node,
                "GET",
                f"/api/sessions/{urllib.parse.quote(session)}/transcript-state?lines={lines}",
            )
            return transcript_state_from_payload(body)
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 405}:
                raise RuntimeError(remote_http_error_detail(exc)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(str(exc)) from exc

        try:
            output = request_json(
                node,
                "GET",
                f"/api/sessions/{urllib.parse.quote(session)}/output?lines={lines}",
            )
        except urllib.error.HTTPError as exc:
            raise RuntimeError(remote_http_error_detail(exc)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(str(exc)) from exc
        cached_node = collect_node_view(node, prefer_cached=True)
        remote_session = next(
            (item for item in cached_node.sessions if item.name == session),
            None,
        )
        return parse_transcript(
            strip_ansi(str(output.get("output") or "")),
            remote_session.agent if remote_session else "",
        )
    return tmux_transcript_state(session, lines=lines)


def replace_chat_history_from_transcript(
    node_id: str, session: str, transcript_messages
) -> list[dict[str, object]]:
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
        key = chat_key(node_id, session)
        existing = data.get(key, [])
        if not isinstance(existing, list):
            existing = []
        pending_users = pending_chat_user_messages(existing, rows, now)
        messages = sorted_chat_messages([*rows, *pending_users])[-80:]
        if data.get(key) != messages:
            data[key] = messages
            save_chat_histories(data)
    return messages


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


def chat_fingerprint(text: str) -> str:
    return re.sub(r"\s+", "", text.strip()).lower()


def same_chat_message_instance(
    left: dict[str, object], right: dict[str, object]
) -> bool:
    left_id = str(left.get("id") or "")
    right_id = str(right.get("id") or "")
    if left_id and right_id and left_id == right_id:
        return True
    if left.get("role") != right.get("role"):
        return False
    if chat_fingerprint(str(left.get("text") or "")) != chat_fingerprint(
        str(right.get("text") or "")
    ):
        return False
    left_time = int(left.get("time") or 0)
    right_time = int(right.get("time") or 0)
    return bool(
        left_time
        and right_time
        and abs(left_time - right_time) <= CHAT_MESSAGE_MATCH_WINDOW_MS
    )


def pending_chat_user_messages(
    existing: list[dict[str, object]],
    transcript: list[dict[str, object]],
    now: int,
) -> list[dict[str, object]]:
    transcript_users = [message for message in transcript if message.get("role") == "user"]
    matched_transcript: set[int] = set()
    pending = []
    for message in sorted_chat_messages(existing):
        if message.get("role") != "user":
            continue
        match_index = next(
            (
                index
                for index, transcript_message in enumerate(transcript_users)
                if index not in matched_transcript
                and same_chat_message_instance(message, transcript_message)
            ),
            -1,
        )
        if match_index >= 0:
            matched_transcript.add(match_index)
            continue
        timestamp = int(message.get("time") or 0)
        if timestamp and now - timestamp <= CHAT_PENDING_USER_RETENTION_MS:
            pending.append(message)
    return pending


def tmux_attach_command(view) -> str:
    session = view.config.session or view.name
    return f"tmux attach -t {shlex.quote(session)}"


def tmux_quick_commands(view) -> list[dict[str, str]]:
    session = view.config.session or view.name
    quoted_session = shlex.quote(session)
    return [
        {"label": "List", "command": "tmux ls"},
        {"label": "Attach", "command": f"tmux attach -t {quoted_session}"},
        {"label": "Detach", "command": "Ctrl-b d"},
        {"label": "Kill", "command": f"tmux kill-session -t {quoted_session}"},
        {"label": "Exit", "command": "exit"},
    ]


def remote_http_error_detail(exc: urllib.error.HTTPError) -> str:
    text = exc.read().decode("utf-8", errors="replace")
    detail = text.strip() or exc.reason
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        detail_value = payload.get("detail")
        if isinstance(detail_value, str) and detail_value:
            detail = detail_value
    return str(detail)


def remote_file_error_detail(exc: urllib.error.HTTPError) -> str:
    detail = remote_http_error_detail(exc)
    if exc.code == 404 and detail == "Not Found":
        return "Remote node does not support raw file previews yet. Restart or update the StarAgent node process."
    return detail


def remote_request_exception(
    exc: OSError | urllib.error.URLError,
    *,
    raw_file: bool = False,
) -> HTTPException:
    if isinstance(exc, urllib.error.HTTPError):
        detail = remote_file_error_detail(exc) if raw_file else remote_http_error_detail(exc)
        return HTTPException(status_code=exc.code, detail=detail)
    return HTTPException(status_code=502, detail=str(exc))


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
