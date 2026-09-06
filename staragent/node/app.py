from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from staragent.adopt import adopt_existing_session, discover_adoptable_sessions
from staragent.agent_auth import logout_codex
from staragent.agent_history import agent_history_payload
from staragent.agent_tools import (
    AgentToolUpdateBusyError,
    agent_tools_payload,
    clear_agent_tools_cache,
    install_agent_tool,
    update_agent_tool,
)
from staragent.auth import node_auth_token
from staragent.event_log import append_node_outbox_event, node_outbox_payload
from staragent.files import (
    create_directory_payload,
    directory_listing,
    file_preview_payload,
    file_raw_info_payload,
    file_raw_payload,
)
from staragent.harness_config import (
    harness_configuration_payload,
    save_harness_config,
    save_harness_environment,
)
from staragent.harness_terminal import open_codex_login_terminal, open_harness_terminal
from staragent.pty_terminal import PtyTerminal, parse_client_message
from staragent.runtime import (
    capture_tmux_pane_ansi,
    kill_tmux_session,
    send_tmux_input,
    send_tmux_message,
    start_tmux_worker,
    tmux_session_exists,
)
from staragent.schemas import (
    CreateDirectory,
    CreateWorker,
    HarnessConfigRequest,
    HarnessEnvironmentRequest,
    SendMessage,
    TerminalInput,
)
from staragent.self_update import (
    STARAGENT_UPDATE_CONFLICTS,
    StarAgentUpdateBusyError,
    StarAgentUpdateError,
    apply_official_update,
    current_installation_info,
    official_update_status,
    schedule_node_restart,
)
from staragent.session_parser import tmux_transcript_state, transcript_state_payload
from staragent.status import collect_session_view, collect_session_views
from staragent.web_terminal import interact_with_pty_websocket, stream_pty_to_websocket


@contextlib.asynccontextmanager
async def node_lifespan(_app: FastAPI):
    append_node_outbox_event(
        "info",
        "node.started",
        "Node API started.",
        source="node.runtime",
        details={"pid": os.getpid(), "cwd": str(Path.cwd())},
    )
    try:
        yield
    finally:
        append_node_outbox_event(
            "info",
            "node.stopped",
            "Node API stopped gracefully.",
            source="node.runtime",
            details={"pid": os.getpid()},
        )


def staragent_update_error_response(error: StarAgentUpdateError) -> JSONResponse:
    status_code = 409 if error.code in STARAGENT_UPDATE_CONFLICTS else 502
    return JSONResponse(
        {"ok": False, "error": error.code, "detail": str(error)},
        status_code=status_code,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="StarAgent Node", lifespan=node_lifespan)
    node_info = current_installation_info()

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
    def sessions() -> dict[str, object]:
        return {
            "sessions": [session_payload(view) for view in collect_session_views()],
            "node": node_info,
            "capabilities": {
                "logs": 1,
                "agent_tools": 6,
                "agent_auth": 1,
                "agent_auth_management": 1,
                "agent_configuration": 1,
                "agent_install": 1,
                "agent_update": 1,
                "agent_usage": 1,
                "agent_history": 1,
                "agent_terminal": 1,
                "session_status": 2,
                "staragent_update": 1,
            },
        }

    @app.get("/api/staragent-update")
    def staragent_update_status() -> JSONResponse:
        try:
            return JSONResponse({"ok": True, **official_update_status(service="node")})
        except StarAgentUpdateBusyError as exc:
            return staragent_update_error_response(exc)
        except StarAgentUpdateError as exc:
            return staragent_update_error_response(exc)

    @app.post("/api/staragent-update/check")
    def check_staragent_update() -> JSONResponse:
        try:
            return JSONResponse(
                {"ok": True, **official_update_status(refresh=True, service="node")}
            )
        except StarAgentUpdateBusyError as exc:
            return staragent_update_error_response(exc)
        except StarAgentUpdateError as exc:
            append_node_outbox_event(
                "warning",
                "node.update_check_failed",
                "Could not check the official StarAgent update channel.",
                source="node.update",
                details={"reason": exc.code},
            )
            return staragent_update_error_response(exc)

    @app.post("/api/staragent-update/apply")
    def install_staragent_update() -> JSONResponse:
        try:
            result = apply_official_update(service="node")
        except StarAgentUpdateBusyError as exc:
            return staragent_update_error_response(exc)
        except StarAgentUpdateError as exc:
            append_node_outbox_event(
                "warning",
                "node.update_failed",
                "StarAgent refused or failed an official update.",
                source="node.update",
                details={"reason": exc.code},
            )
            return staragent_update_error_response(exc)

        after = result.get("after") if isinstance(result.get("after"), dict) else {}
        before = result.get("before") if isinstance(result.get("before"), dict) else {}
        updated = bool(result.get("updated"))
        restart_scheduled = schedule_node_restart() if updated else False
        if updated:
            append_node_outbox_event(
                "info",
                "node.updated",
                "StarAgent fast-forwarded to the latest official commit.",
                source="node.update",
                details={
                    "branch": after.get("branch", ""),
                    "before_commit": before.get("current_short_commit", ""),
                    "after_commit": after.get("current_short_commit", ""),
                    "restart_scheduled": restart_scheduled,
                },
            )
        return JSONResponse({**result, "restart_scheduled": restart_scheduled})

    @app.get("/api/logs")
    def logs(after: str = "", limit: int = 250) -> dict[str, object]:
        return node_outbox_payload(after=after, limit=limit)

    @app.get("/api/agent-tools")
    def agent_tools(refresh: bool = False) -> dict[str, object]:
        return agent_tools_payload(force=refresh)

    @app.get("/api/agent-tools/{agent}/configuration")
    def harness_configuration(agent: str) -> JSONResponse:
        try:
            payload = harness_configuration_payload(agent)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return no_store_json(payload)

    @app.put("/api/agent-tools/{agent}/configuration/file")
    def update_harness_config(agent: str, request: HarnessConfigRequest) -> JSONResponse:
        try:
            payload = save_harness_config(agent, request.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        clear_agent_tools_cache()
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        append_node_outbox_event(
            "info",
            "agent.config_saved",
            f"{agent} configuration was saved.",
            source="node.agents",
            details={"agent": agent, "path": config.get("path") or ""},
        )
        return no_store_json(payload)

    @app.put("/api/agent-tools/{agent}/configuration/environment")
    def update_harness_environment(
        agent: str,
        request: HarnessEnvironmentRequest,
    ) -> JSONResponse:
        try:
            payload = save_harness_environment(agent, request.variables)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        clear_agent_tools_cache()
        append_node_outbox_event(
            "info",
            "agent.environment_saved",
            f"{agent} environment was saved.",
            source="node.agents",
            details={"agent": agent, "variables": sorted(request.variables)},
        )
        return no_store_json(payload)

    @app.post("/api/agent-tools/codex/auth/logout")
    def codex_logout() -> JSONResponse:
        try:
            result = logout_codex()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        clear_agent_tools_cache()
        append_node_outbox_event(
            "info" if result.get("ok") else "warning",
            "agent.auth_logout" if result.get("ok") else "agent.auth_logout_failed",
            "Codex logged out." if result.get("ok") else "Codex logout failed.",
            source="node.agents",
            details={"agent": "codex"},
        )
        return no_store_json(result)

    @app.post("/api/agent-tools/{agent}/update")
    def update_agent_cli(agent: str) -> dict[str, object]:
        try:
            result = update_agent_tool(agent)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AgentToolUpdateBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        append_node_outbox_event(
            "info" if result.get("ok") else "warning",
            "agent.update_succeeded" if result.get("ok") else "agent.update_failed",
            f"{result.get('label') or agent} update "
            f"{'completed' if result.get('ok') else 'failed'}.",
            source="node.agents",
            details={
                "agent": result.get("agent") or agent,
                "before_version": result.get("before_version") or "",
                "after_version": result.get("after_version") or "",
                "changed": bool(result.get("changed")),
                "error": result.get("error") or "",
            },
        )
        return result

    @app.post("/api/agent-tools/{agent}/install/{option_id}")
    def install_agent_cli(agent: str, option_id: str) -> dict[str, object]:
        try:
            result = install_agent_tool(agent, option_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AgentToolUpdateBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        append_node_outbox_event(
            "info" if result.get("ok") else "warning",
            "agent.install_succeeded" if result.get("ok") else "agent.install_failed",
            f"{result.get('label') or agent} installation "
            f"{'completed' if result.get('ok') else 'failed'}.",
            source="node.agents",
            details={
                "agent": result.get("agent") or agent,
                "option": result.get("option") or "",
                "source": result.get("source") or "",
                "after_status": result.get("after_status") or "",
                "after_version": result.get("after_version") or "",
                "error": result.get("error") or "",
            },
        )
        return result

    @app.get("/api/agent-history")
    def agent_history(agent: str = "", limit: int = 50, refresh: bool = False) -> dict[str, object]:
        try:
            return agent_history_payload(agent=agent, limit=limit, force=refresh)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions/{name}")
    def session(name: str) -> dict[str, object]:
        view = collect_session_view(name)
        if not view:
            raise HTTPException(status_code=404, detail=f"session not found: {name}")
        return session_payload(view)

    @app.post("/api/workers")
    def create_worker(payload: CreateWorker) -> dict[str, str]:
        try:
            start_tmux_worker(payload.name, payload.cwd, payload.command)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        append_node_outbox_event(
            "info",
            "session.created",
            f"Session {payload.name} was created.",
            source="node.api",
            details={"session": payload.name, "cwd": payload.cwd},
        )
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
        append_node_outbox_event(
            "info",
            "session.adopted",
            f"Session {payload.name} was adopted.",
            source="node.api",
            details={"session": payload.name},
        )
        return {"status": "adopted", "session": adopted.as_dict()}

    @app.delete("/api/sessions/{name}")
    def stop_session(name: str) -> dict[str, str]:
        try:
            kill_tmux_session(name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        append_node_outbox_event(
            "info",
            "session.stopped",
            f"Session {name} was stopped.",
            source="node.api",
            details={"session": name},
        )
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
            raise HTTPException(status_code=404, detail=f"session not found: {name}")
        return {"output": capture_tmux_pane_ansi(name, lines=max(20, min(lines, 5000)))}

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

    @app.get("/api/files/raw")
    def file_raw(path: str, root: str | None = None) -> Response:
        try:
            body, media_type = file_raw_payload(path, root=root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(content=body, media_type=media_type)

    @app.get("/api/files/raw-info")
    def file_raw_info(path: str, root: str | None = None) -> dict[str, object]:
        try:
            return file_raw_info_payload(path, root=root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.websocket("/ws/sessions/{name}/terminal")
    async def terminal_socket(websocket: WebSocket, name: str) -> None:
        await websocket.accept()
        if not websocket_is_authenticated(websocket):
            await websocket.close(code=4401, reason="unauthorized")
            return
        if not tmux_session_exists(name):
            await websocket.close(code=4404, reason=f"session not found: {name}")
            return
        terminal = PtyTerminal.attach_session(name)
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

    @app.websocket("/ws/agent-tools/{agent}/terminal")
    async def agent_terminal_socket(websocket: WebSocket, agent: str) -> None:
        await websocket.accept()
        if not websocket_is_authenticated(websocket):
            await websocket.close(code=4401, reason="unauthorized")
            return
        opened = False
        try:
            with open_harness_terminal(agent) as terminal:
                opened = True
                append_node_outbox_event(
                    "info",
                    "agent.terminal_opened",
                    f"{agent} interactive shell opened.",
                    source="node.agents",
                    details={"agent": agent},
                )
                await interact_with_pty_websocket(terminal, websocket)
        except ValueError as exc:
            await websocket.close(code=4404, reason=str(exc)[:120])
        except OSError:
            await websocket.close(code=1011, reason="could not start interactive shell")
        finally:
            if opened:
                append_node_outbox_event(
                    "info",
                    "agent.terminal_closed",
                    f"{agent} interactive shell closed.",
                    source="node.agents",
                    details={"agent": agent},
                )

    @app.websocket("/ws/agent-tools/codex/auth/login")
    async def codex_login_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        if not websocket_is_authenticated(websocket):
            await websocket.close(code=4401, reason="unauthorized")
            return
        opened = False
        try:
            with open_codex_login_terminal() as terminal:
                opened = True
                append_node_outbox_event(
                    "info",
                    "agent.auth_login_started",
                    "Codex device login started.",
                    source="node.agents",
                    details={"agent": "codex"},
                )
                await interact_with_pty_websocket(
                    terminal,
                    websocket,
                    max_age_seconds=15 * 60,
                )
        except OSError:
            await websocket.close(code=1011, reason="could not start Codex login")
        finally:
            clear_agent_tools_cache()
            if opened:
                append_node_outbox_event(
                    "info",
                    "agent.auth_login_finished",
                    "Codex device login terminal closed.",
                    source="node.agents",
                    details={"agent": "codex"},
                )

    return app


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


def no_store_json(payload: object) -> JSONResponse:
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


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
        "status_revision": view.status_report.status_revision if view.status_report else "",
        "source": view.status_report.source if view.status_report else "tmux",
        "last_updated": view.last_updated.isoformat() if view.last_updated else None,
    }


def is_agent_session(name: str) -> bool:
    view = collect_session_view(name)
    return view.session_type == "agent" if view else True
