from __future__ import annotations

import json
import os
import shlex
import textwrap
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol

from staragent.hub import HubSession, collect_hub_sessions, node_by_name, request_json
from staragent.runtime import capture_tmux_pane_ansi, send_tmux_message, strip_ansi

MAX_REPLY_CHARS = 3600
DEFAULT_TAIL_LINES = 80
MAX_TAIL_LINES = 300


@dataclass(frozen=True)
class FeishuConfig:
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
    ) -> FeishuConfig:
        configured_allow_all = allow_all
        if configured_allow_all is None:
            configured_allow_all = os.environ.get("STARAGENT_FEISHU_ALLOW_ALL", "").strip() in {
                "1",
                "true",
                "yes",
            }
        config = cls(
            app_id=app_id or os.environ.get("STARAGENT_FEISHU_APP_ID", "").strip(),
            app_secret=app_secret or os.environ.get("STARAGENT_FEISHU_APP_SECRET", "").strip(),
            verification_token=verification_token
            or os.environ.get("STARAGENT_FEISHU_VERIFICATION_TOKEN", "").strip(),
            encrypt_key=encrypt_key or os.environ.get("STARAGENT_FEISHU_ENCRYPT_KEY", "").strip(),
            allowed_users=parse_csv(allowed_users or os.environ.get("STARAGENT_FEISHU_ALLOWED_USERS", "")),
            allowed_chats=parse_csv(allowed_chats or os.environ.get("STARAGENT_FEISHU_ALLOWED_CHATS", "")),
            allow_all=bool(configured_allow_all),
            dashboard_url=(dashboard_url or os.environ.get("STARAGENT_DASHBOARD_URL", "")).strip(),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.app_id:
            raise ValueError("STARAGENT_FEISHU_APP_ID is required")
        if not self.app_secret:
            raise ValueError("STARAGENT_FEISHU_APP_SECRET is required")
        if not (self.allow_all or self.allowed_users or self.allowed_chats):
            raise ValueError(
                "Set STARAGENT_FEISHU_ALLOWED_USERS, STARAGENT_FEISHU_ALLOWED_CHATS, "
                "or STARAGENT_FEISHU_ALLOW_ALL=1 before starting Feishu integration"
            )

    def permits(self, message: IncomingFeishuMessage) -> bool:
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
class IncomingFeishuMessage:
    message_id: str
    chat_id: str
    chat_type: str
    text: str
    sender: SenderIdentity
    root_id: str = ""
    thread_id: str = ""

    @classmethod
    def from_sdk_event(cls, event: Any) -> IncomingFeishuMessage:
        payload = getattr(event, "event", None)
        if payload is None:
            raise ValueError("Feishu event payload is missing")
        message = getattr(payload, "message", None)
        sender = getattr(payload, "sender", None)
        if message is None or sender is None:
            raise ValueError("Feishu message event is incomplete")
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


class StarAgentBackend(Protocol):
    def list_sessions(self) -> list[HubSession]:
        ...

    def send_message(self, node_id: str, session: str, text: str) -> None:
        ...

    def tail_session(self, node_id: str, session: str, lines: int) -> str:
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

    def session_url(self, node_id: str, session: str) -> str:
        if not self.dashboard_url:
            return ""
        quoted_node = urllib.parse.quote(node_id, safe="")
        quoted_session = urllib.parse.quote(session, safe="")
        return f"{self.dashboard_url}/nodes/{quoted_node}/sessions/{quoted_session}"


class FeishuCommandHandler:
    def __init__(self, backend: StarAgentBackend) -> None:
        self.backend = backend

    def handle(self, message: IncomingFeishuMessage) -> str | None:
        command_text = normalize_command_text(message.text)
        if not command_text:
            return None
        command, rest = split_command(command_text)
        if not command:
            return None
        try:
            if command in {"help", "h"}:
                return help_text()
            if command in {"sessions", "ls", "ps"}:
                return self.list_sessions()
            if command == "status":
                return self.status(rest)
            if command == "tail":
                return self.tail(rest)
            if command == "send":
                return self.send(rest)
            if command == "open":
                return self.open_session(rest)
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

    def status(self, rest: str) -> str:
        session = self.resolve_required_session(rest)
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

    def tail(self, rest: str) -> str:
        args = split_args(rest)
        if not args:
            raise CommandError("Usage: /tail <node/session> [lines]")
        session = self.resolve_session(args[0])
        lines = DEFAULT_TAIL_LINES
        if len(args) > 1:
            try:
                lines = int(args[1])
            except ValueError as exc:
                raise CommandError("Tail lines must be a number") from exc
        output = self.backend.tail_session(session.node_id, session.name, lines)
        if not output.strip():
            return f"{session.node_id}/{session.name} has no captured output."
        return truncate_reply(f"{session.node_id}/{session.name} tail:\n\n{output}")

    def send(self, rest: str) -> str:
        args = split_args(rest)
        if len(args) < 2:
            raise CommandError("Usage: /send <node/session> <message>")
        session = self.resolve_session(args[0])
        if session.session_type != "agent":
            raise CommandError("System sessions are read-only; /send only supports agent sessions.")
        text = " ".join(args[1:]).strip()
        if not text:
            raise CommandError("Message is empty")
        self.backend.send_message(session.node_id, session.name, text)
        return f"Sent to {session.node_id}/{session.name}."

    def open_session(self, rest: str) -> str:
        session = self.resolve_required_session(rest)
        url = self.backend.session_url(session.node_id, session.name)
        if not url:
            return "Set STARAGENT_DASHBOARD_URL to enable /open links."
        return url

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


class FeishuTransport:
    def __init__(self, config: FeishuConfig) -> None:
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
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        self.create_request = CreateMessageRequest
        self.create_body = CreateMessageRequestBody
        self.reply_request = ReplyMessageRequest
        self.reply_body = ReplyMessageRequestBody

    def reply_text(self, message: IncomingFeishuMessage, text: str) -> None:
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


class FeishuIntegration:
    def __init__(
        self,
        config: FeishuConfig,
        *,
        backend: StarAgentBackend | None = None,
        transport: FeishuTransport | None = None,
    ) -> None:
        self.config = config
        self.backend = backend or HubStarAgentBackend(config.dashboard_url)
        self.handler = FeishuCommandHandler(self.backend)
        self.transport = transport or FeishuTransport(config)
        self.lark = self.transport.lark
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def on_message(self, event: Any) -> None:
        message = IncomingFeishuMessage.from_sdk_event(event)
        with self._anchor_lock(message):
            if not self.config.permits(message):
                self.transport.reply_text(message, "StarAgent Feishu access denied.")
                return
            response = self.handler.handle(message)
            if response:
                self.transport.reply_text(message, response)

    def _anchor_lock(self, message: IncomingFeishuMessage) -> threading.Lock:
        anchor = message.thread_id or message.root_id or message.chat_id or message.message_id
        with self._locks_guard:
            lock = self._locks.get(anchor)
            if lock is None:
                lock = threading.Lock()
                self._locks[anchor] = lock
            return lock

    def run_forever(self) -> None:
        handler = (
            self.lark.EventDispatcherHandler.builder(
                self.config.encrypt_key,
                self.config.verification_token,
            )
            .register_p2_im_message_receive_v1(self.on_message)
            .build()
        )
        client = self.lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=handler,
            log_level=self.lark.LogLevel.WARNING,
        )
        client.start()


class CommandError(Exception):
    pass


def run_feishu_integration(config: FeishuConfig) -> None:
    FeishuIntegration(config).run_forever()


def import_lark_sdk():
    try:
        import lark_oapi as lark  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Install Feishu support with: pip install -e '.[feishu]'") from exc
    return lark


def ensure_lark_response(response: Any) -> None:
    if response.success():
        return
    message = getattr(response, "msg", None) or "Feishu API request failed"
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


def help_text() -> str:
    return textwrap.dedent(
        """
        StarAgent Feishu commands:
        /sessions
        /status <node/session>
        /tail <node/session> [lines]
        /send <node/session> <message>
        /open <node/session>
        """
    ).strip()
