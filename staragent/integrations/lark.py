from __future__ import annotations

import json
import os
import shlex
import textwrap
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from staragent.hub import HubSession, collect_hub_sessions, node_by_name, request_json
from staragent.paths import state_dir
from staragent.runtime import capture_tmux_pane_ansi, send_tmux_message, strip_ansi
from staragent.session_parser import tmux_transcript_state, transcript_state_from_payload
from staragent.transcript import TranscriptMessage, TranscriptState

MAX_REPLY_CHARS = 3600
DEFAULT_TAIL_LINES = 80
MAX_TAIL_LINES = 300
DEFAULT_HISTORY_MESSAGES = 12
MAX_HISTORY_MESSAGES = 30
LARK_WORKING_REACTION_EMOJI = "THUMBSUP"
AGENT_REPLY_POLL_INTERVAL_SECONDS = 2.0
AGENT_REPLY_TIMEOUT_SECONDS = 20 * 60.0
LARK_ROUTES_PATH = state_dir() / "lark_routes.json"
LARK_PENDING_REACTIONS_PATH = state_dir() / "lark_pending_reactions.json"
DIRECT_CHAT_TYPES = {"p2p", "private", "direct"}
GROUP_CHAT_TYPES = {"group", "chat"}
PROXY_ENV_NAMES = (
    "wss_proxy",
    "WSS_PROXY",
    "ws_proxy",
    "WS_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "http_proxy",
    "HTTP_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


@dataclass(frozen=True)
class LarkConfig:
    app_id: str
    app_secret: str
    verification_token: str = ""
    encrypt_key: str = ""
    allowed_users: frozenset[str] = field(default_factory=frozenset)
    allowed_chats: frozenset[str] = field(default_factory=frozenset)
    allow_all: bool = False
    dashboard_url: str = ""

    @classmethod
    def from_env(
        cls,
        *,
        app_id: str = "",
        app_secret: str = "",
        verification_token: str = "",
        encrypt_key: str = "",
        allowed_users: str = "",
        allowed_chats: str = "",
        allow_all: bool | None = None,
        dashboard_url: str = "",
    ) -> LarkConfig:
        configured_allow_all = allow_all
        if configured_allow_all is None:
            configured_allow_all = os.environ.get("STARAGENT_LARK_ALLOW_ALL", "").strip() in {
                "1",
                "true",
                "yes",
            }
        config = cls(
            app_id=app_id or os.environ.get("STARAGENT_LARK_APP_ID", "").strip(),
            app_secret=app_secret or os.environ.get("STARAGENT_LARK_APP_SECRET", "").strip(),
            verification_token=verification_token
            or os.environ.get("STARAGENT_LARK_VERIFICATION_TOKEN", "").strip(),
            encrypt_key=encrypt_key or os.environ.get("STARAGENT_LARK_ENCRYPT_KEY", "").strip(),
            allowed_users=parse_csv(allowed_users or os.environ.get("STARAGENT_LARK_ALLOWED_USERS", "")),
            allowed_chats=parse_csv(allowed_chats or os.environ.get("STARAGENT_LARK_ALLOWED_CHATS", "")),
            allow_all=bool(configured_allow_all),
            dashboard_url=(dashboard_url or os.environ.get("STARAGENT_DASHBOARD_URL", "")).strip(),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.app_id:
            raise ValueError("STARAGENT_LARK_APP_ID is required")
        if not self.app_secret:
            raise ValueError("STARAGENT_LARK_APP_SECRET is required")
        if not (self.allow_all or self.allowed_users or self.allowed_chats):
            raise ValueError(
                "Set STARAGENT_LARK_ALLOWED_USERS, STARAGENT_LARK_ALLOWED_CHATS, "
                "or STARAGENT_LARK_ALLOW_ALL=1 before starting Lark integration"
            )

    def permits(self, message: IncomingLarkMessage) -> bool:
        if self.allow_all:
            return True
        if message.chat_id and message.chat_id in self.allowed_chats:
            return True
        return bool(message.sender.ids() & self.allowed_users)


@dataclass(frozen=True)
class SenderIdentity:
    open_id: str = ""
    user_id: str = ""
    union_id: str = ""
    sender_type: str = ""

    def ids(self) -> set[str]:
        return {value for value in (self.open_id, self.user_id, self.union_id) if value}


@dataclass(frozen=True)
class IncomingLarkMessage:
    message_id: str
    chat_id: str
    chat_type: str
    text: str
    sender: SenderIdentity
    root_id: str = ""
    thread_id: str = ""

    @classmethod
    def from_sdk_event(cls, event: Any) -> IncomingLarkMessage:
        payload = getattr(event, "event", None)
        if payload is None:
            raise ValueError("Lark event payload is missing")
        message = getattr(payload, "message", None)
        sender = getattr(payload, "sender", None)
        if message is None or sender is None:
            raise ValueError("Lark message event is incomplete")
        sender_id = getattr(sender, "sender_id", None)
        identity = SenderIdentity(
            open_id=getattr(sender_id, "open_id", "") or "",
            user_id=getattr(sender_id, "user_id", "") or "",
            union_id=getattr(sender_id, "union_id", "") or "",
            sender_type=getattr(sender, "sender_type", "") or "",
        )
        return cls(
            message_id=getattr(message, "message_id", "") or "",
            chat_id=getattr(message, "chat_id", "") or "",
            chat_type=getattr(message, "chat_type", "") or "",
            root_id=getattr(message, "root_id", "") or "",
            thread_id=getattr(message, "thread_id", "") or "",
            text=extract_message_text(message),
            sender=identity,
        )


@dataclass(frozen=True)
class LarkSessionRoute:
    node_id: str
    session: str

    @property
    def target(self) -> str:
        return f"{self.node_id}/{self.session}"

    def as_dict(self) -> dict[str, str]:
        return {"node_id": self.node_id, "session": self.session}


@dataclass(frozen=True)
class LarkConversationBinding:
    key: str
    chat_id: str
    route: LarkSessionRoute


@dataclass(frozen=True)
class LarkPendingReaction:
    message_id: str
    reaction_id: str

    def as_dict(self) -> dict[str, str]:
        return {"message_id": self.message_id, "reaction_id": self.reaction_id}


class LarkConversationRoutes:
    def __init__(self, path: Path = LARK_ROUTES_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()

    def get(self, message: IncomingLarkMessage) -> LarkSessionRoute | None:
        key = lark_conversation_key(message)
        if not key:
            return None
        raw = self._load().get(key)
        if not isinstance(raw, dict):
            return None
        node_id = str(raw.get("node_id") or "").strip()
        session = str(raw.get("session") or "").strip()
        if not node_id or not session:
            return None
        return LarkSessionRoute(node_id=node_id, session=session)

    def set(self, message: IncomingLarkMessage, route: LarkSessionRoute) -> None:
        key = lark_conversation_key(message)
        if not key:
            raise ValueError("Lark session routes require a group chat message")
        with self._lock:
            routes = self._load()
            row = route.as_dict()
            row["chat_id"] = message.chat_id
            row["bound_at"] = str(int(time.time()))
            routes[key] = row
            self._save(routes)

    def clear(self, message: IncomingLarkMessage) -> bool:
        key = lark_conversation_key(message)
        if not key:
            return False
        with self._lock:
            routes = self._load()
            removed = routes.pop(key, None) is not None
            if removed:
                self._save(routes)
            return removed

    def bindings(self) -> list[LarkConversationBinding]:
        bindings: list[LarkConversationBinding] = []
        for key, raw in self._load().items():
            if not isinstance(raw, dict):
                continue
            chat_id = lark_chat_id_from_key(str(key))
            if not chat_id:
                continue
            node_id = str(raw.get("node_id") or "").strip()
            session = str(raw.get("session") or "").strip()
            if not node_id or not session:
                continue
            bindings.append(
                LarkConversationBinding(
                    key=str(key),
                    chat_id=chat_id,
                    route=LarkSessionRoute(node_id=node_id, session=session),
                )
            )
        return bindings

    def _load(self) -> dict[str, object]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        routes = data.get("routes", data) if isinstance(data, dict) else {}
        return dict(routes) if isinstance(routes, dict) else {}

    def _save(self, routes: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps({"routes": routes}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.path)


class StarAgentBackend(Protocol):
    def list_sessions(self) -> list[HubSession]:
        ...

    def send_message(self, node_id: str, session: str, text: str) -> None:
        ...

    def tail_session(self, node_id: str, session: str, lines: int) -> str:
        ...

    def transcript_state(self, node_id: str, session: str, lines: int = 500) -> TranscriptState:
        ...

    def session_url(self, node_id: str, session: str) -> str:
        ...


class HubStarAgentBackend:
    def __init__(self, dashboard_url: str = "") -> None:
        self.dashboard_url = dashboard_url.rstrip("/")

    def list_sessions(self) -> list[HubSession]:
        return collect_hub_sessions()

    def send_message(self, node_id: str, session: str, text: str) -> None:
        node = node_by_name(node_id)
        if node.is_local:
            send_tmux_message(session, text)
            return
        path = f"/api/sessions/{urllib.parse.quote(session, safe='')}/send"
        request_json(node, "POST", path, {"text": text})

    def tail_session(self, node_id: str, session: str, lines: int) -> str:
        node = node_by_name(node_id)
        lines = max(20, min(lines, MAX_TAIL_LINES))
        if node.is_local:
            return strip_ansi(capture_tmux_pane_ansi(session, lines=lines))
        path = f"/api/sessions/{urllib.parse.quote(session, safe='')}/output?lines={lines}"
        return strip_ansi(str(request_json(node, "GET", path).get("output") or ""))

    def transcript_state(self, node_id: str, session: str, lines: int = 500) -> TranscriptState:
        node = node_by_name(node_id)
        lines = max(20, min(lines, 500))
        if node.is_local:
            return tmux_transcript_state(session, lines=lines)
        path = (
            f"/api/sessions/{urllib.parse.quote(session, safe='')}"
            f"/transcript-state?lines={lines}"
        )
        return transcript_state_from_payload(request_json(node, "GET", path))

    def session_url(self, node_id: str, session: str) -> str:
        if not self.dashboard_url:
            return ""
        quoted_node = urllib.parse.quote(node_id, safe="")
        quoted_session = urllib.parse.quote(session, safe="")
        return f"{self.dashboard_url}/nodes/{quoted_node}/sessions/{quoted_session}"


class LarkCommandHandler:
    def __init__(
        self,
        backend: StarAgentBackend,
        routes: LarkConversationRoutes | None = None,
        on_agent_message: Callable[
            [IncomingLarkMessage, LarkSessionRoute, TranscriptState], None
        ]
        | None = None,
        on_bind: Callable[[IncomingLarkMessage, LarkSessionRoute, TranscriptState | None], None]
        | None = None,
    ) -> None:
        self.backend = backend
        self.routes = routes or LarkConversationRoutes()
        self.on_agent_message = on_agent_message
        self.on_bind = on_bind

    def handle(self, message: IncomingLarkMessage) -> str | None:
        command_text = normalize_command_text(message.text)
        try:
            if not command_text:
                return self.forward_to_bound_session(message)
            command, rest = split_command(command_text)
            if not command:
                return None
            if command in {"help", "h"}:
                return help_text()
            if command in {"sessions", "ls", "ps"}:
                return self.list_sessions()
            if command == "status":
                return self.status(message, rest)
            if command == "tail":
                return self.tail(message, rest)
            if command in {"history", "messages", "chat"}:
                return self.history(message, rest)
            if command == "open":
                return self.open_session(message, rest)
            if command in {"use", "bind", "session"}:
                return self.bind_conversation(message, rest)
            if command in {"where", "target"}:
                return self.conversation_target(message)
            if command in {"unbind", "clear"}:
                return self.unbind_conversation(message)
            if command == "send":
                return self.send_to_bound_session(message, rest)
            return f"Unknown command: /{command}\n\n{help_text()}"
        except CommandError as exc:
            return str(exc)
        except Exception as exc:
            return f"StarAgent command failed: {exc}"

    def list_sessions(self) -> str:
        sessions = self.backend.list_sessions()
        if not sessions:
            return "No StarAgent sessions found."
        lines = ["StarAgent sessions:"]
        for item in sessions[:30]:
            lines.append(
                f"- {item.node_id}/{item.name} [{item.session_type}] "
                f"{item.status} {item.agent or '-'} {item.repo_name or item.repo or '-'}"
            )
        if len(sessions) > 30:
            lines.append(f"... {len(sessions) - 30} more")
        return "\n".join(lines)

    def status(self, message: IncomingLarkMessage, rest: str) -> str:
        session = self.resolve_session_or_bound_default(message, rest, "/status <node/session>")
        view = session.view
        rows = [
            f"Session: {session.node_id}/{session.name}",
            f"Type: {view.session_type}",
            f"Agent: {view.agent or '-'}",
            f"Status: {view.status}",
            f"Repo: {view.repo or '-'}",
            f"Branch: {view.branch or '-'}",
            f"Task: {view.task or '-'}",
        ]
        summary = view.status_report.summary if view.status_report else ""
        if summary:
            rows.append(f"Summary: {summary}")
        question = view.status_report.question if view.status_report else ""
        if question:
            rows.append(f"Question: {question}")
        return "\n".join(rows)

    def tail(self, message: IncomingLarkMessage, rest: str) -> str:
        args = split_args(rest)
        lines = DEFAULT_TAIL_LINES
        if is_agent_group_chat(message) and (not args or is_int_string(args[0])):
            session = self.resolve_bound_session(message, "/tail <node/session> [lines]")
            if len(args) > 1:
                raise CommandError("Usage in a bound Feishu group chat: /tail [lines]")
            line_arg = args[0] if args else ""
        else:
            if not args:
                raise CommandError("Usage: /tail <node/session> [lines]")
            if len(args) > 2:
                raise CommandError("Usage: /tail <node/session> [lines]")
            session = self.resolve_session(args[0])
            line_arg = args[1] if len(args) > 1 else ""
        if line_arg:
            try:
                lines = int(line_arg)
            except ValueError as exc:
                raise CommandError("Tail lines must be a number") from exc
        output = self.backend.tail_session(session.node_id, session.name, lines)
        if not output.strip():
            return f"{session.node_id}/{session.name} has no captured output."
        return truncate_reply(f"{session.node_id}/{session.name} tail:\n\n{output}")

    def history(self, message: IncomingLarkMessage, rest: str) -> str:
        session, count = self.resolve_history_request(message, rest)
        state = self.backend.transcript_state(session.node_id, session.name)
        route = LarkSessionRoute(node_id=session.node_id, session=session.name)
        return format_history(route, state, count)

    def resolve_history_request(
        self, message: IncomingLarkMessage, rest: str
    ) -> tuple[HubSession, int]:
        args = split_args(rest)
        count = DEFAULT_HISTORY_MESSAGES
        if is_agent_group_chat(message) and (not args or is_int_string(args[0])):
            route = self.routes.get(message)
            if route is None:
                raise CommandError(
                    "No StarAgent session is bound to this Feishu group chat. "
                    "Use /use <node/session> first, or use /history <node/session> [count]."
                )
            if len(args) > 1:
                raise CommandError("Usage in a bound Feishu group chat: /history [count]")
            if args:
                count = parse_history_count(args[0])
            return self.resolve_session(route.target), count
        if not args:
            raise CommandError("Usage: /history <node/session> [count]")
        if len(args) > 2:
            raise CommandError("Usage: /history <node/session> [count]")
        session = self.resolve_session(args[0])
        if len(args) == 2:
            count = parse_history_count(args[1])
        return session, count

    def send_to_bound_session(self, message: IncomingLarkMessage, rest: str) -> str:
        text = rest.strip()
        if not text:
            raise CommandError("Usage in a bound Feishu group chat: /send <message>")
        return (
            self.forward_to_bound_session(message, text=text, acknowledge=True)
            or group_required_text(message)
        )

    def bind_conversation(self, message: IncomingLarkMessage, rest: str) -> str:
        if not is_agent_group_chat(message):
            raise CommandError(group_required_text(message))
        if not rest.strip():
            return self.conversation_target(message)
        session = self.resolve_required_session(rest)
        if session.session_type != "agent":
            raise CommandError("Only agent sessions can be bound to Lark conversations.")
        route = LarkSessionRoute(node_id=session.node_id, session=session.name)
        self.routes.set(message, route)
        if self.on_bind:
            self.on_bind(message, route, self.transcript_state_or_none(route.node_id, route.session))
        return (
            f"Bound this Feishu group chat to {route.target}.\n"
            "Send plain messages in this group to talk to that session. Use /unbind to clear it."
        )

    def conversation_target(self, message: IncomingLarkMessage) -> str:
        if not is_agent_group_chat(message):
            return group_required_text(message)
        route = self.routes.get(message)
        if route is None:
            return "No StarAgent session is bound to this Feishu group chat. Use /use <node/session>."
        return f"This Feishu group chat is bound to {route.target}."

    def unbind_conversation(self, message: IncomingLarkMessage) -> str:
        if not is_agent_group_chat(message):
            return group_required_text(message)
        if self.routes.clear(message):
            return "Cleared the StarAgent session binding for this Feishu group chat."
        return "No StarAgent session binding was set for this Feishu group chat."

    def forward_to_bound_session(
        self,
        message: IncomingLarkMessage,
        *,
        text: str | None = None,
        acknowledge: bool = False,
    ) -> str | None:
        text = message.text.strip() if text is None else text.strip()
        if not text:
            return None
        if not is_agent_group_chat(message):
            return group_required_text(message)
        route = self.routes.get(message)
        if route is None:
            return (
                "No StarAgent session is bound to this Feishu group chat. "
                "Use /use <node/session> first."
            )
        session = self.resolve_session(route.target)
        if session.session_type != "agent":
            raise CommandError("Bound session is not an agent session. Use /use <node/session>.")
        baseline = self.transcript_state_or_none(session.node_id, session.name)
        self.backend.send_message(session.node_id, session.name, text)
        if self.on_agent_message and baseline is not None:
            self.on_agent_message(
                message,
                LarkSessionRoute(node_id=session.node_id, session=session.name),
                baseline,
            )
        if acknowledge:
            return f"Sent to {session.node_id}/{session.name}."
        return None

    def transcript_state_or_none(self, node_id: str, session: str) -> TranscriptState | None:
        try:
            return self.backend.transcript_state(node_id, session)
        except Exception as exc:
            print(f"Lark transcript baseline failed for {node_id}/{session}: {exc}", flush=True)
            return None

    def open_session(self, message: IncomingLarkMessage, rest: str) -> str:
        session = self.resolve_session_or_bound_default(message, rest, "/open <node/session>")
        url = self.backend.session_url(session.node_id, session.name)
        if not url:
            return "Set STARAGENT_DASHBOARD_URL to enable /open links."
        return url

    def resolve_session_or_bound_default(
        self, message: IncomingLarkMessage, rest: str, usage: str
    ) -> HubSession:
        args = split_args(rest)
        if not args:
            return self.resolve_bound_session(message, usage)
        if len(args) != 1:
            raise CommandError(f"Usage: {usage}")
        return self.resolve_session(args[0])

    def resolve_bound_session(self, message: IncomingLarkMessage, usage: str) -> HubSession:
        if not is_agent_group_chat(message):
            raise CommandError(f"Usage: {usage}")
        route = self.routes.get(message)
        if route is None:
            raise CommandError(
                "No StarAgent session is bound to this Feishu group chat. "
                f"Use /use <node/session> first, or {usage}."
            )
        return self.resolve_session(route.target)

    def resolve_required_session(self, rest: str) -> HubSession:
        args = split_args(rest)
        if len(args) != 1:
            raise CommandError("Expected exactly one session target, for example local/my-session.")
        return self.resolve_session(args[0])

    def resolve_session(self, target: str) -> HubSession:
        target = target.strip()
        if not target:
            raise CommandError("Session target is required")
        sessions = self.backend.list_sessions()
        if "/" in target:
            node_id, _, session_name = target.partition("/")
            matches = [
                item for item in sessions if item.node_id == node_id and item.name == session_name
            ]
        else:
            matches = [item for item in sessions if item.name == target]
        if not matches:
            raise CommandError(f"Session not found: {target}")
        if len(matches) > 1:
            choices = ", ".join(f"{item.node_id}/{item.name}" for item in matches[:8])
            raise CommandError(f"Ambiguous session name. Use node/session. Matches: {choices}")
        return matches[0]


class LarkTransport:
    def __init__(self, config: LarkConfig) -> None:
        self.config = config
        self.lark = import_lark_sdk()
        self.client = (
            self.lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .log_level(self.lark.LogLevel.WARNING)
            .build()
        )
        from lark_oapi.api.im.v1 import (  # type: ignore[import-not-found]
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            CreateMessageRequest,
            CreateMessageRequestBody,
            DeleteMessageReactionRequest,
            Emoji,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        self.create_request = CreateMessageRequest
        self.create_body = CreateMessageRequestBody
        self.create_reaction_request = CreateMessageReactionRequest
        self.create_reaction_body = CreateMessageReactionRequestBody
        self.delete_reaction_request = DeleteMessageReactionRequest
        self.emoji = Emoji
        self.reply_request = ReplyMessageRequest
        self.reply_body = ReplyMessageRequestBody

    def reply_text(self, message: IncomingLarkMessage, text: str) -> None:
        body = (
            self.reply_body.builder()
            .msg_type("text")
            .content(text_content(truncate_reply(text)))
            .reply_in_thread(bool(message.thread_id or message.root_id))
            .build()
        )
        request = self.reply_request.builder().message_id(message.message_id).request_body(body).build()
        response = self.client.im.v1.message.reply(request)
        ensure_lark_response(response)

    def send_text(self, chat_id: str, text: str) -> None:
        body = (
            self.create_body.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(text_content(truncate_reply(text)))
            .build()
        )
        request = (
            self.create_request.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response = self.client.im.v1.message.create(request)
        ensure_lark_response(response)

    def add_reaction(self, message: IncomingLarkMessage, emoji_type: str) -> str:
        if not message.message_id:
            return ""
        reaction_type = self.emoji.builder().emoji_type(emoji_type).build()
        body = self.create_reaction_body.builder().reaction_type(reaction_type).build()
        request = (
            self.create_reaction_request.builder()
            .message_id(message.message_id)
            .request_body(body)
            .build()
        )
        response = self.client.im.v1.message_reaction.create(request)
        ensure_lark_response(response)
        data = getattr(response, "data", None)
        return str(getattr(data, "reaction_id", "") or "")

    def delete_reaction(self, message_id: str, reaction_id: str) -> None:
        if not message_id or not reaction_id:
            return
        request = (
            self.delete_reaction_request.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        response = self.client.im.v1.message_reaction.delete(request)
        ensure_lark_response(response)


class AgentReplyWatcher:
    def __init__(
        self,
        backend: StarAgentBackend,
        transport: LarkTransport,
        *,
        poll_interval: float = AGENT_REPLY_POLL_INTERVAL_SECONDS,
        timeout: float = AGENT_REPLY_TIMEOUT_SECONDS,
    ) -> None:
        self.backend = backend
        self.transport = transport
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._guard = threading.Lock()
        self._tokens: dict[str, int] = {}

    def schedule(
        self,
        message: IncomingLarkMessage,
        route: LarkSessionRoute,
        baseline: TranscriptState,
    ) -> None:
        key = lark_conversation_key(message)
        if not key:
            return
        token = self._next_token(key)
        thread = threading.Thread(
            target=self._watch,
            args=(key, token, message, route, baseline),
            name=f"lark-reply-{route.node_id}-{route.session}",
            daemon=True,
        )
        thread.start()

    def _next_token(self, key: str) -> int:
        with self._guard:
            token = self._tokens.get(key, 0) + 1
            self._tokens[key] = token
            return token

    def _is_current(self, key: str, token: int) -> bool:
        with self._guard:
            return self._tokens.get(key) == token

    def _watch(
        self,
        key: str,
        token: int,
        message: IncomingLarkMessage,
        route: LarkSessionRoute,
        baseline: TranscriptState,
    ) -> None:
        baseline_reply = final_reply_from_state(baseline)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            time.sleep(self.poll_interval)
            if not self._is_current(key, token):
                return
            try:
                state = self.backend.transcript_state(route.node_id, route.session)
            except Exception as exc:
                print(
                    f"Lark final reply poll failed for {route.target}: {exc}",
                    flush=True,
                )
                continue
            reply = final_reply_from_state(state)
            if not reply or reply == baseline_reply:
                continue
            if not self._is_current(key, token):
                return
            self.transport.reply_text(message, format_agent_final_reply(route, reply))
            return
        if self._is_current(key, token):
            print(f"Lark final reply watcher timed out for {route.target}", flush=True)


class BoundSessionReplyBroadcaster:
    def __init__(
        self,
        backend: StarAgentBackend,
        transport: LarkTransport,
        routes: LarkConversationRoutes,
        *,
        poll_interval: float = AGENT_REPLY_POLL_INTERVAL_SECONDS,
        pending_reactions_path: Path = LARK_PENDING_REACTIONS_PATH,
    ) -> None:
        self.backend = backend
        self.transport = transport
        self.routes = routes
        self.poll_interval = poll_interval
        self.pending_reactions_path = pending_reactions_path
        self._last_final: dict[str, str] = {}
        self._pending_reactions = self._load_pending_reactions()
        self._observed_working: set[str] = set()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._last_guard = threading.Lock()
        self._reaction_guard = threading.Lock()

    def start(self) -> None:
        with self._guard:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="lark-bound-session-replies",
                daemon=True,
            )
            self._thread.start()

    def mark_message_working(
        self,
        message: IncomingLarkMessage,
        route: LarkSessionRoute,
        baseline: TranscriptState,
    ) -> None:
        self.mark_baseline(message, route, baseline)
        key = lark_conversation_key(message)
        if not key:
            return
        state_key = self._state_key(key, route)
        with self._last_guard:
            self._observed_working.add(state_key)
        try:
            reaction_id = self.transport.add_reaction(message, LARK_WORKING_REACTION_EMOJI)
        except Exception as exc:
            print(f"Lark working reaction add failed for {route.target}: {exc}", flush=True)
            return
        if not reaction_id:
            return
        self.remember_pending_reaction(
            state_key,
            LarkPendingReaction(message_id=message.message_id, reaction_id=reaction_id),
        )

    def mark_baseline(
        self,
        message: IncomingLarkMessage,
        route: LarkSessionRoute,
        baseline: TranscriptState | None = None,
    ) -> None:
        key = lark_conversation_key(message)
        if not key:
            return
        if baseline is not None:
            reply = final_reply_from_state(baseline)
            fingerprint = final_reply_fingerprint(baseline, reply) if reply else ""
        else:
            fingerprint = self._route_final_fingerprint(route)
        with self._last_guard:
            self._last_final[self._state_key(key, route)] = fingerprint

    def poll_once(self) -> None:
        active_keys: set[str] = set()
        for binding in self.routes.bindings():
            state_key = self._state_key(binding.key, binding.route)
            active_keys.add(state_key)
            fingerprint, reply, working = self._route_state(binding.route)
            if not fingerprint or not reply:
                if working:
                    with self._last_guard:
                        self._observed_working.add(state_key)
                continue
            with self._last_guard:
                previous = self._last_final.get(state_key)
                saw_working = state_key in self._observed_working
            with self._reaction_guard:
                saw_working = saw_working or bool(self._pending_reactions.get(state_key))
            if previous is None:
                with self._last_guard:
                    self._last_final[state_key] = fingerprint
                    self._observed_working.discard(state_key)
                if not saw_working:
                    continue
            if previous == fingerprint:
                with self._last_guard:
                    self._observed_working.discard(state_key)
                continue
            with self._last_guard:
                self._last_final[state_key] = fingerprint
                self._observed_working.discard(state_key)
            self.transport.send_text(
                binding.chat_id,
                format_agent_final_reply(binding.route, reply),
            )
            self.clear_pending_reactions(state_key, binding.route)
        with self._last_guard:
            for key in list(self._last_final):
                if key not in active_keys:
                    self._last_final.pop(key, None)
            self._observed_working.intersection_update(active_keys)
        with self._reaction_guard:
            stale_reactions = [
                key for key in self._pending_reactions if key not in active_keys
            ]
        for key in stale_reactions:
            self.clear_pending_reactions(key)

    def _run(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                print(f"Lark bound reply broadcaster failed: {exc}", flush=True)
            time.sleep(self.poll_interval)

    def _state_key(self, key: str, route: LarkSessionRoute) -> str:
        return f"{key}:{route.target}"

    def _route_final_fingerprint(self, route: LarkSessionRoute) -> str:
        fingerprint, _, _ = self._route_state(route)
        return fingerprint

    def _route_state(self, route: LarkSessionRoute) -> tuple[str, str, bool]:
        try:
            state = self.backend.transcript_state(route.node_id, route.session)
        except Exception as exc:
            print(f"Lark bound reply poll failed for {route.target}: {exc}", flush=True)
            return "", "", False
        reply = final_reply_from_state(state)
        if not reply:
            return "", "", state.working
        return final_reply_fingerprint(state, reply), reply, state.working

    def clear_pending_reactions(
        self,
        state_key: str,
        route: LarkSessionRoute | None = None,
    ) -> None:
        with self._reaction_guard:
            reactions = self._pending_reactions.pop(state_key, [])
            if reactions:
                self._save_pending_reactions_locked()
        for reaction in reactions:
            try:
                self.transport.delete_reaction(reaction.message_id, reaction.reaction_id)
            except Exception as exc:
                target = route.target if route else state_key
                print(f"Lark working reaction delete failed for {target}: {exc}", flush=True)

    def remember_pending_reaction(
        self,
        state_key: str,
        reaction: LarkPendingReaction,
    ) -> None:
        with self._reaction_guard:
            reactions = self._pending_reactions.setdefault(state_key, [])
            reactions.append(reaction)
            self._save_pending_reactions_locked()

    def _load_pending_reactions(self) -> dict[str, list[LarkPendingReaction]]:
        try:
            data = json.loads(self.pending_reactions_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        raw_reactions = data.get("reactions", data) if isinstance(data, dict) else {}
        if not isinstance(raw_reactions, dict):
            return {}
        reactions: dict[str, list[LarkPendingReaction]] = {}
        for key, raw_items in raw_reactions.items():
            if not isinstance(raw_items, list):
                continue
            items: list[LarkPendingReaction] = []
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                message_id = str(raw.get("message_id") or "").strip()
                reaction_id = str(raw.get("reaction_id") or "").strip()
                if message_id and reaction_id:
                    items.append(LarkPendingReaction(message_id, reaction_id))
            if items:
                reactions[str(key)] = items
        return reactions

    def _save_pending_reactions_locked(self) -> None:
        self.pending_reactions_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.pending_reactions_path.with_suffix(".json.tmp")
        rows = {
            key: [reaction.as_dict() for reaction in reactions]
            for key, reactions in self._pending_reactions.items()
            if reactions
        }
        temp_path.write_text(
            json.dumps({"reactions": rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.pending_reactions_path)


class LarkIntegration:
    def __init__(
        self,
        config: LarkConfig,
        *,
        backend: StarAgentBackend | None = None,
        transport: LarkTransport | None = None,
    ) -> None:
        self.config = config
        self.backend = backend or HubStarAgentBackend(config.dashboard_url)
        self.transport = transport or LarkTransport(config)
        self.routes = LarkConversationRoutes()
        self.reply_broadcaster = BoundSessionReplyBroadcaster(
            self.backend,
            self.transport,
            self.routes,
        )
        self.handler = LarkCommandHandler(
            self.backend,
            routes=self.routes,
            on_agent_message=self.reply_broadcaster.mark_message_working,
            on_bind=self.reply_broadcaster.mark_baseline,
        )
        self.lark = self.transport.lark
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._deduper = LarkMessageDeduper()

    def on_message(self, event: Any) -> None:
        message = IncomingLarkMessage.from_sdk_event(event)
        if self._deduper.seen(message):
            print(
                "Lark duplicate message ignored "
                f"message_id={message.message_id or '-'} "
                f"chat_id={message.chat_id or '-'}",
                flush=True,
            )
            return
        print(
            "Lark message "
            f"message_id={message.message_id or '-'} "
            f"chat_id={message.chat_id or '-'} "
            f"chat_type={message.chat_type or '-'} "
            f"root_id={message.root_id or '-'} "
            f"thread_id={message.thread_id or '-'} "
            f"open_id={message.sender.open_id or '-'} "
            f"user_id={message.sender.user_id or '-'} "
            f"union_id={message.sender.union_id or '-'}",
            flush=True,
        )
        with self._anchor_lock(message):
            if not self.config.permits(message):
                self.transport.reply_text(message, "StarAgent Lark access denied.")
                return
            response = self.handler.handle(message)
            if response:
                self.transport.reply_text(message, response)

    def _anchor_lock(self, message: IncomingLarkMessage) -> threading.Lock:
        anchor = lark_conversation_key(message) or message.chat_id or message.message_id
        with self._locks_guard:
            lock = self._locks.get(anchor)
            if lock is None:
                lock = threading.Lock()
                self._locks[anchor] = lock
            return lock

    def run_forever(self) -> None:
        ws_proxy_enabled = enable_lark_ws_env_proxy()
        if ws_proxy_enabled:
            print("Lark WebSocket proxy enabled from environment.", flush=True)
        self.reply_broadcaster.start()
        handler = (
            self.lark.EventDispatcherHandler.builder(
                self.config.encrypt_key,
                self.config.verification_token,
            )
            .register_p2_im_message_receive_v1(self.on_message)
            .register_p2_im_message_reaction_created_v1(ignore_lark_event)
            .register_p2_im_message_reaction_deleted_v1(ignore_lark_event)
            .build()
        )
        client = self.lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=handler,
            log_level=self.lark.LogLevel.WARNING,
        )
        client.start()


class LarkMessageDeduper:
    def __init__(self, ttl_seconds: float = 10 * 60.0, max_entries: int = 2000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def seen(self, message: IncomingLarkMessage) -> bool:
        message_id = message.message_id.strip()
        if not message_id:
            return False
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if message_id in self._seen:
                self._seen[message_id] = now
                return True
            self._seen[message_id] = now
            return False

    def _prune(self, now: float) -> None:
        if len(self._seen) <= self.max_entries:
            expired = [
                message_id
                for message_id, last_seen in self._seen.items()
                if now - last_seen > self.ttl_seconds
            ]
            for message_id in expired:
                self._seen.pop(message_id, None)
            return
        keep = sorted(self._seen.items(), key=lambda item: item[1])[-self.max_entries :]
        self._seen = dict(keep)


class CommandError(Exception):
    pass


def run_lark_integration(config: LarkConfig) -> None:
    LarkIntegration(config).run_forever()


def ignore_lark_event(event: Any) -> None:
    return None


def import_lark_sdk():
    try:
        import lark_oapi as lark  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Install Lark support with: pip install -e '.[lark]'") from exc
    return lark


def enable_lark_ws_env_proxy() -> bool:
    if not any(os.environ.get(name, "").strip() for name in PROXY_ENV_NAMES):
        return False
    try:
        import inspect

        import lark_oapi.ws.client as ws_client  # type: ignore[import-not-found]
        import websockets
    except ImportError:
        return False
    if "proxy" not in inspect.signature(websockets.connect).parameters:
        return False
    original_connect_kwargs = getattr(ws_client, "_ws_connect_kwargs", None)
    if original_connect_kwargs is None:
        return False
    if getattr(ws_client, "_staragent_env_proxy_enabled", False):
        return True

    def ws_connect_kwargs() -> dict[str, object]:
        kwargs = dict(original_connect_kwargs())
        kwargs["proxy"] = True
        return kwargs

    ws_client._ws_connect_kwargs = ws_connect_kwargs
    ws_client._staragent_env_proxy_enabled = True
    return True


def ensure_lark_response(response: Any) -> None:
    if response.success():
        return
    message = getattr(response, "msg", None) or "Lark API request failed"
    log_id = response.get_log_id() if hasattr(response, "get_log_id") else None
    if log_id:
        message = f"{message} (log_id={log_id})"
    raise RuntimeError(message)


def parse_csv(raw: str) -> frozenset[str]:
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def text_content(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False)


def extract_message_text(message: Any) -> str:
    try:
        payload = json.loads(getattr(message, "content", "") or "{}")
    except json.JSONDecodeError:
        return ""
    text = payload["text"] if isinstance(payload.get("text"), str) else extract_post_text(payload)
    return strip_leading_mentions(text, getattr(message, "mentions", None) or [])


def extract_post_text(payload: dict[str, Any]) -> str:
    body = payload.get("zh_cn") or payload.get("en_us") or payload
    if not isinstance(body, dict):
        return ""
    content = body.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for paragraph in content:
        if not isinstance(paragraph, list):
            continue
        for node in paragraph:
            if isinstance(node, dict) and node.get("tag") == "text":
                parts.append(str(node.get("text") or ""))
    return "".join(parts)


def strip_leading_mentions(text: str, mentions: list[Any]) -> str:
    text = text.strip()
    while text:
        matched = False
        for mention in mentions:
            keys = [getattr(mention, "key", "") or ""]
            name = getattr(mention, "name", "") or ""
            if name:
                keys.append(f"@{name}")
            for key in keys:
                if key and text.startswith(key):
                    text = text[len(key) :].lstrip()
                    matched = True
                    break
            if matched:
                break
        if not matched:
            break
    return text


def normalize_command_text(text: str) -> str:
    text = text.strip()
    had_prefix = False
    if text.lower().startswith("staragent "):
        text = text[len("staragent ") :].strip()
        had_prefix = True
    if had_prefix and text and not text.startswith("/"):
        text = f"/{text}"
    if text and not text.startswith("/"):
        return ""
    return text


def is_direct_chat(message: IncomingLarkMessage) -> bool:
    return message.chat_type.strip().lower() in DIRECT_CHAT_TYPES


def is_group_chat(message: IncomingLarkMessage) -> bool:
    return message.chat_type.strip().lower() in GROUP_CHAT_TYPES


def is_agent_group_chat(message: IncomingLarkMessage) -> bool:
    return bool(message.chat_id and is_group_chat(message))


def lark_conversation_key(message: IncomingLarkMessage) -> str:
    if not is_agent_group_chat(message):
        return ""
    return f"chat:{message.chat_id}"


def lark_chat_id_from_key(key: str) -> str:
    prefix = "chat:"
    return key.removeprefix(prefix) if key.startswith(prefix) else ""


def group_required_text(message: IncomingLarkMessage) -> str:
    if is_direct_chat(message):
        return (
            "Private chat with StarAgent Bot is for session management only.\n"
            "Use /sessions, /status <node/session>, /history <node/session>, "
            "/tail <node/session>, /open <node/session>, or /help here.\n"
            "To talk to an agent session, add StarAgent to a Feishu group chat from group settings, "
            "then run /use <node/session> in that group."
        )
    return (
        "Agent session chat requires a Feishu group chat.\n"
        "Add StarAgent from the group settings, then run /use <node/session> in that group."
    )


def split_command(text: str) -> tuple[str, str]:
    head, _, rest = text.partition(" ")
    return head.removeprefix("/").strip().lower(), rest.strip()


def split_args(text: str) -> list[str]:
    if not text.strip():
        return []
    try:
        return shlex.split(text)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc


def truncate_reply(text: str, max_chars: int = MAX_REPLY_CHARS) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    suffix = f"\n\n... truncated to {max_chars} chars"
    return text[: max_chars - len(suffix)].rstrip() + suffix


def parse_history_count(raw: str) -> int:
    try:
        count = int(raw)
    except ValueError as exc:
        raise CommandError("History count must be a number") from exc
    if count < 1:
        raise CommandError("History count must be at least 1")
    return min(count, MAX_HISTORY_MESSAGES)


def is_int_string(raw: str) -> bool:
    return raw.strip().lstrip("+-").isdigit()


def final_reply_from_state(state: TranscriptState) -> str:
    if not state.final:
        return ""
    return (state.reply or state.completed_reply).strip()


def final_reply_fingerprint(state: TranscriptState, reply: str) -> str:
    for message in reversed(state.messages):
        if message.role == "agent" and message.text.strip() == reply:
            return f"{message.source_id}:{message.timestamp_ms}:{reply}"
    return reply


def format_agent_final_reply(route: LarkSessionRoute, reply: str) -> str:
    return reply.strip()


def format_history(route: LarkSessionRoute, state: TranscriptState, count: int) -> str:
    messages = [message for message in state.messages if message.text.strip()]
    if not messages:
        fallback = (state.reply or state.completed_reply).strip()
        if fallback:
            return truncate_reply(f"{route.target} latest parsed reply:\n\nAgent: {fallback}")
        return (
            f"No structured conversation history found for {route.target}.\n"
            f"Use /tail {route.target} 120 to inspect raw terminal output."
        )
    recent = messages[-count:]
    first_index = len(messages) - len(recent) + 1
    rows = [f"{route.target} recent conversation ({len(recent)}/{len(messages)}):"]
    for offset, message in enumerate(recent, start=first_index):
        rows.append("")
        rows.append(format_history_message(offset, message))
    return truncate_reply("\n".join(rows))


def format_history_message(index: int, message: TranscriptMessage) -> str:
    role = format_history_role(message.role)
    timestamp = format_history_timestamp(message.timestamp_ms)
    label = f"{index}. {role}{timestamp}"
    return f"{label}\n{compact_history_text(message.text)}"


def format_history_role(role: str) -> str:
    normalized = role.strip().lower()
    if normalized == "user":
        return "User"
    if normalized == "agent":
        return "Agent"
    if normalized == "session":
        return "Session"
    return normalized.title() or "Message"


def format_history_timestamp(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        return ""
    return " " + time.strftime("%m-%d %H:%M", time.localtime(timestamp_ms / 1000))


def compact_history_text(text: str, max_chars: int = 900) -> str:
    lines = []
    blank = False
    for raw_line in strip_ansi(text).strip().splitlines():
        line = raw_line.rstrip()
        if not line:
            if not blank:
                lines.append("")
            blank = True
            continue
        lines.append(line)
        blank = False
    value = "\n".join(lines).strip()
    if len(value) <= max_chars:
        return value
    suffix = f"\n... message truncated to {max_chars} chars"
    return value[: max_chars - len(suffix)].rstrip() + suffix


def help_text() -> str:
    return textwrap.dedent(
        """
        StarAgent Lark commands:

        Private chat:
        /help
        /sessions
        /status <node/session>
        /history <node/session> [count]
        /tail <node/session> [lines]
        /open <node/session>

        Bound Feishu group:
        /use <node/session>
        /use
        /status
        /history [count]
        /tail [lines]
        /open
        /unbind

        Plain messages in a bound group are sent to that session.
        """
    ).strip()
