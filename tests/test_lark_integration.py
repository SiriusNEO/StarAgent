from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace

from staragent.integrations.lark import (
    PROXY_ENV_NAMES,
    BoundSessionReplyBroadcaster,
    IncomingLarkMessage,
    LarkCommandHandler,
    LarkConfig,
    LarkConversationRoutes,
    LarkIntegration,
    LarkSessionRoute,
    SenderIdentity,
    enable_lark_ws_env_proxy,
    extract_message_text,
)
from staragent.transcript import TranscriptMessage, TranscriptState


@dataclass
class FakeStatus:
    summary: str = ""
    question: str = ""


@dataclass
class FakeView:
    name: str
    session_type: str = "agent"
    status: str = "idle"
    agent: str = "codex"
    repo: str = "/repo/project"
    branch: str = "main"
    task: str = "tmux session"
    status_report: FakeStatus | None = None

    @property
    def repo_name(self) -> str:
        return self.repo.rstrip("/").split("/")[-1]


@dataclass
class FakeSession:
    node_id: str
    view: FakeView

    @property
    def name(self) -> str:
        return self.view.name

    def __getattr__(self, name: str):
        return getattr(self.view, name)


class FakeBackend:
    def __init__(self, sessions):
        self.sessions = sessions
        self.sent: list[tuple[str, str, str]] = []
        self.state = TranscriptState()

    def list_sessions(self):
        return self.sessions

    def send_message(self, node_id: str, session: str, text: str) -> None:
        self.sent.append((node_id, session, text))

    def tail_session(self, node_id: str, session: str, lines: int) -> str:
        return f"tail {node_id}/{session} {lines}"

    def session_url(self, node_id: str, session: str) -> str:
        return f"https://staragent.test/nodes/{node_id}/sessions/{session}"

    def transcript_state(self, node_id: str, session: str, lines: int = 500):
        return self.state


class FakeTransport:
    def __init__(self):
        self.replies: list[tuple[IncomingLarkMessage, str]] = []
        self.sent_texts: list[tuple[str, str]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.deleted_reactions: list[tuple[str, str]] = []
        self.fail_reactions = False
        self.lark = SimpleNamespace()

    def reply_text(self, message: IncomingLarkMessage, text: str) -> None:
        self.replies.append((message, text))

    def send_text(self, chat_id: str, text: str) -> None:
        self.sent_texts.append((chat_id, text))

    def add_reaction(self, message: IncomingLarkMessage, emoji_type: str) -> str:
        if self.fail_reactions:
            raise RuntimeError("reaction unavailable")
        reaction_id = f"react_{len(self.reactions) + 1}"
        self.reactions.append((message.message_id, emoji_type, reaction_id))
        return reaction_id

    def delete_reaction(self, message_id: str, reaction_id: str) -> None:
        if self.fail_reactions:
            raise RuntimeError("reaction unavailable")
        self.deleted_reactions.append((message_id, reaction_id))


def make_message(
    text: str,
    open_id: str = "ou_allowed",
    chat_id: str = "oc_chat",
    chat_type: str = "group",
    thread_id: str = "",
    message_id: str = "om_1",
):
    return IncomingLarkMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,
        text=text,
        sender=SenderIdentity(open_id=open_id),
        thread_id=thread_id,
    )


def make_event(
    text: str,
    *,
    message_id: str = "om_1",
    chat_id: str = "oc_chat",
    chat_type: str = "group",
    open_id: str = "ou_allowed",
):
    return SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id=message_id,
                chat_id=chat_id,
                chat_type=chat_type,
                root_id="",
                thread_id="",
                content=json.dumps({"text": text}),
                mentions=[],
            ),
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=open_id, user_id="", union_id=""),
                sender_type="user",
            ),
        )
    )


def test_lark_config_requires_explicit_access_scope():
    config = LarkConfig(
        app_id="cli_x",
        app_secret="secret",
        allowed_users=frozenset({"ou_allowed"}),
    )

    assert config.permits(make_message("/sessions"))
    assert not config.permits(make_message("/sessions", open_id="ou_denied"))


def test_extract_message_text_strips_leading_mentions():
    mention = SimpleNamespace(key="@_user_1", name="StarAgent")
    message = SimpleNamespace(
        content='{"text":"@_user_1 /sessions"}',
        mentions=[mention],
    )

    assert extract_message_text(message) == "/sessions"


def test_command_handler_lists_sessions():
    backend = FakeBackend([FakeSession("local", FakeView("dev", status="active"))])
    handler = LarkCommandHandler(backend)

    response = handler.handle(make_message("staragent sessions"))

    assert response is not None
    assert "local/dev" in response
    assert "active" in response


def test_command_handler_rejects_system_session_binding(tmp_path):
    backend = FakeBackend([FakeSession("local", FakeView("staragent-hub", session_type="system"))])
    handler = LarkCommandHandler(backend, LarkConversationRoutes(tmp_path / "routes.json"))

    response = handler.handle(make_message("/use local/staragent-hub"))

    assert response == "Only agent sessions can be bound to Lark conversations."
    assert backend.sent == []


def test_command_handler_requires_node_for_ambiguous_session_name():
    backend = FakeBackend(
        [
            FakeSession("local", FakeView("dev")),
            FakeSession("node-a", FakeView("dev")),
        ]
    )
    handler = LarkCommandHandler(backend)

    response = handler.handle(make_message("/status dev"))

    assert response is not None
    assert "Ambiguous session name" in response


def test_command_handler_binds_group_and_forwards_plain_message(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    routes = LarkConversationRoutes(tmp_path / "routes.json")
    handler = LarkCommandHandler(backend, routes)

    response = handler.handle(make_message("/use node-a/dev"))
    forwarded = handler.handle(make_message("fix this bug"))

    assert response is not None
    assert "node-a/dev" in response
    assert forwarded is None
    assert backend.sent == [("node-a", "dev", "fix this bug")]


def test_command_handler_group_history_defaults_to_bound_session(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    backend.state = TranscriptState(
        messages=(
            TranscriptMessage("user", "old request", 1_780_000_000_000, "u1"),
            TranscriptMessage("agent", "old reply", 1_780_000_001_000, "a1"),
            TranscriptMessage("user", "latest request", 1_780_000_002_000, "u2"),
            TranscriptMessage("agent", "latest reply", 1_780_000_003_000, "a2"),
        )
    )
    handler = LarkCommandHandler(backend, LarkConversationRoutes(tmp_path / "routes.json"))
    handler.handle(make_message("/use node-a/dev"))

    response = handler.handle(make_message("/history 2"))

    assert response is not None
    assert "node-a/dev recent conversation (2/4)" in response
    assert "latest request" in response
    assert "latest reply" in response
    assert "old request" not in response


def test_command_handler_bound_group_commands_default_to_binding(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    handler = LarkCommandHandler(backend, LarkConversationRoutes(tmp_path / "routes.json"))
    handler.handle(make_message("/use node-a/dev"))

    current = handler.handle(make_message("/use"))
    status = handler.handle(make_message("/status"))
    tail = handler.handle(make_message("/tail 40"))
    open_url = handler.handle(make_message("/open"))

    assert current == "This Feishu group chat is bound to node-a/dev."
    assert status is not None
    assert "Session: node-a/dev" in status
    assert tail == "node-a/dev tail:\n\ntail node-a/dev 40"
    assert open_url == "https://staragent.test/nodes/node-a/sessions/dev"


def test_command_handler_private_history_requires_session_target():
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    backend.state = TranscriptState(
        messages=(
            TranscriptMessage("user", "fix this", 1_780_000_000_000, "u1"),
            TranscriptMessage("agent", "done", 1_780_000_001_000, "a1"),
        )
    )
    handler = LarkCommandHandler(backend)

    missing = handler.handle(make_message("/history", chat_id="oc_p2p", chat_type="p2p"))
    response = handler.handle(
        make_message("/history node-a/dev 1", chat_id="oc_p2p", chat_type="p2p")
    )

    assert missing == "Usage: /history <node/session> [count]"
    assert response is not None
    assert "node-a/dev recent conversation (1/2)" in response
    assert "done" in response
    assert "fix this" not in response


def test_command_handler_history_without_structured_messages_suggests_tail(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    handler = LarkCommandHandler(backend, LarkConversationRoutes(tmp_path / "routes.json"))
    handler.handle(make_message("/use node-a/dev"))

    response = handler.handle(make_message("/history"))

    assert response == (
        "No structured conversation history found for node-a/dev.\n"
        "Use /tail node-a/dev 120 to inspect raw terminal output."
    )


def test_command_handler_schedules_final_reply_watcher(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    backend.state = TranscriptState(reply="old answer", completed_reply="old answer", final=True)
    calls = []
    handler = LarkCommandHandler(
        backend,
        LarkConversationRoutes(tmp_path / "routes.json"),
        on_agent_message=lambda message, route, baseline: calls.append((message, route, baseline)),
    )
    handler.handle(make_message("/use node-a/dev"))

    response = handler.handle(make_message("fix this bug"))

    assert response is None
    assert len(calls) == 1
    assert calls[0][1] == LarkSessionRoute("node-a", "dev")
    assert calls[0][2].reply == "old answer"


def test_command_handler_unbinds_group_route(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    handler = LarkCommandHandler(backend, LarkConversationRoutes(tmp_path / "routes.json"))
    message = make_message("/use node-a/dev")
    handler.handle(message)

    assert handler.handle(make_message("/use")) == (
        "This Feishu group chat is bound to node-a/dev."
    )
    assert handler.handle(make_message("/unbind")) == (
        "Cleared the StarAgent session binding for this Feishu group chat."
    )
    assert handler.handle(make_message("ignored after unbind")) == (
        "No StarAgent session is bound to this Feishu group chat. Use /use <node/session> first."
    )
    assert backend.sent == []


def test_command_handler_private_chat_is_management_only(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    handler = LarkCommandHandler(backend, LarkConversationRoutes(tmp_path / "routes.json"))

    sessions = handler.handle(make_message("/sessions", chat_id="oc_p2p", chat_type="p2p"))
    bind = handler.handle(make_message("/use node-a/dev", chat_id="oc_p2p", chat_type="p2p"))
    plain = handler.handle(make_message("fix this bug", chat_id="oc_p2p", chat_type="p2p"))

    assert sessions is not None
    assert "node-a/dev" in sessions
    assert bind is not None
    assert "Private chat with StarAgent Bot is for session management only" in bind
    assert plain is not None
    assert "Private chat with StarAgent Bot is for session management only" in plain
    assert backend.sent == []


def test_command_handler_binds_plain_group_chat(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    handler = LarkCommandHandler(backend, LarkConversationRoutes(tmp_path / "routes.json"))

    response = handler.handle(make_message("/use node-a/dev"))
    forwarded = handler.handle(make_message("fix this bug"))

    assert response is not None
    assert "Bound this Feishu group chat to node-a/dev" in response
    assert forwarded is None
    assert backend.sent == [("node-a", "dev", "fix this bug")]


def test_command_handler_group_binding_ignores_thread_id(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    handler = LarkCommandHandler(backend, LarkConversationRoutes(tmp_path / "routes.json"))

    handler.handle(make_message("/use node-a/dev", thread_id="omt_1"))
    response = handler.handle(make_message("same group different thread", thread_id="omt_2"))

    assert response is None
    assert backend.sent == [("node-a", "dev", "same group different thread")]


def test_lark_integration_deduplicates_retried_message_events(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    transport = FakeTransport()
    integration = LarkIntegration(
        LarkConfig(app_id="cli_x", app_secret="secret", allow_all=True),
        backend=backend,
        transport=transport,
    )
    integration.handler.routes = LarkConversationRoutes(tmp_path / "routes.json")

    integration.on_message(make_event("/use node-a/dev", message_id="om_bind"))
    integration.on_message(make_event("fix this bug", message_id="om_plain"))
    integration.on_message(make_event("fix this bug", message_id="om_plain"))

    assert backend.sent == [("node-a", "dev", "fix this bug")]
    assert [reply for _, reply in transport.replies].count("Sent to node-a/dev.") == 0


def test_bound_session_broadcaster_sends_new_final_reply_to_bound_group(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    transport = FakeTransport()
    routes = LarkConversationRoutes(tmp_path / "routes.json")
    message = make_message("/use node-a/dev", chat_id="oc_group")
    route = LarkSessionRoute("node-a", "dev")
    routes.set(message, route)
    broadcaster = BoundSessionReplyBroadcaster(
        backend,
        transport,
        routes,
        poll_interval=0.01,
        pending_reactions_path=tmp_path / "pending.json",
    )

    backend.state = TranscriptState(reply="old answer", completed_reply="old answer", final=True)
    broadcaster.poll_once()
    backend.state = TranscriptState(reply="new answer", completed_reply="new answer", final=True)
    broadcaster.poll_once()

    assert transport.sent_texts == [("oc_group", "new answer")]


def test_bound_session_broadcaster_uses_marked_bind_baseline(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    transport = FakeTransport()
    routes = LarkConversationRoutes(tmp_path / "routes.json")
    message = make_message("/use node-a/dev", chat_id="oc_group")
    route = LarkSessionRoute("node-a", "dev")
    routes.set(message, route)
    broadcaster = BoundSessionReplyBroadcaster(
        backend,
        transport,
        routes,
        poll_interval=0.01,
        pending_reactions_path=tmp_path / "pending.json",
    )

    baseline = TranscriptState(reply="old answer", completed_reply="old answer", final=True)
    broadcaster.mark_baseline(message, route, baseline)
    backend.state = TranscriptState(reply="new answer", completed_reply="new answer", final=True)
    broadcaster.poll_once()

    assert transport.sent_texts == [("oc_group", "new answer")]


def test_bound_session_broadcaster_sends_first_final_after_starting_while_working(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    transport = FakeTransport()
    routes = LarkConversationRoutes(tmp_path / "routes.json")
    routes.set(make_message("/use node-a/dev", chat_id="oc_group"), LarkSessionRoute("node-a", "dev"))
    broadcaster = BoundSessionReplyBroadcaster(
        backend,
        transport,
        routes,
        poll_interval=0.01,
        pending_reactions_path=tmp_path / "pending.json",
    )

    backend.state = TranscriptState(working=True, working_label="Working", final=False)
    broadcaster.poll_once()
    backend.state = TranscriptState(reply="new answer", completed_reply="new answer", final=True)
    broadcaster.poll_once()

    assert transport.sent_texts == [("oc_group", "new answer")]


def test_bound_session_broadcaster_reacts_while_working_and_clears_on_final_reply(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    transport = FakeTransport()
    routes = LarkConversationRoutes(tmp_path / "routes.json")
    broadcaster = BoundSessionReplyBroadcaster(
        backend,
        transport,
        routes,
        poll_interval=0.01,
        pending_reactions_path=tmp_path / "pending.json",
    )
    handler = LarkCommandHandler(
        backend,
        routes,
        on_agent_message=broadcaster.mark_message_working,
        on_bind=broadcaster.mark_baseline,
    )

    backend.state = TranscriptState(reply="old answer", completed_reply="old answer", final=True)
    handler.handle(make_message("/use node-a/dev", chat_id="oc_group", message_id="om_bind"))
    response = handler.handle(
        make_message("fix this bug", chat_id="oc_group", message_id="om_user")
    )
    backend.state = TranscriptState(reply="new answer", completed_reply="new answer", final=True)
    broadcaster.poll_once()

    assert response is None
    assert backend.sent == [("node-a", "dev", "fix this bug")]
    assert transport.reactions == [("om_user", "THUMBSUP", "react_1")]
    assert transport.sent_texts == [("oc_group", "new answer")]
    assert transport.deleted_reactions == [("om_user", "react_1")]


def test_bound_session_broadcaster_persists_pending_reaction_across_restart(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    transport = FakeTransport()
    routes = LarkConversationRoutes(tmp_path / "routes.json")
    pending_path = tmp_path / "pending.json"
    broadcaster = BoundSessionReplyBroadcaster(
        backend,
        transport,
        routes,
        poll_interval=0.01,
        pending_reactions_path=pending_path,
    )
    handler = LarkCommandHandler(
        backend,
        routes,
        on_agent_message=broadcaster.mark_message_working,
        on_bind=broadcaster.mark_baseline,
    )

    backend.state = TranscriptState(reply="old answer", completed_reply="old answer", final=True)
    handler.handle(make_message("/use node-a/dev", chat_id="oc_group", message_id="om_bind"))
    handler.handle(make_message("fix this bug", chat_id="oc_group", message_id="om_user"))

    restarted_transport = FakeTransport()
    restarted = BoundSessionReplyBroadcaster(
        backend,
        restarted_transport,
        routes,
        poll_interval=0.01,
        pending_reactions_path=pending_path,
    )
    backend.state = TranscriptState(reply="new answer", completed_reply="new answer", final=True)
    restarted.poll_once()

    assert restarted_transport.sent_texts == [("oc_group", "new answer")]
    assert restarted_transport.deleted_reactions == [("om_user", "react_1")]
    assert json.loads(pending_path.read_text(encoding="utf-8")) == {"reactions": {}}


def test_bound_session_broadcaster_reaction_failure_does_not_block_forwarding(tmp_path):
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    transport = FakeTransport()
    transport.fail_reactions = True
    routes = LarkConversationRoutes(tmp_path / "routes.json")
    broadcaster = BoundSessionReplyBroadcaster(
        backend,
        transport,
        routes,
        poll_interval=0.01,
        pending_reactions_path=tmp_path / "pending.json",
    )
    handler = LarkCommandHandler(
        backend,
        routes,
        on_agent_message=broadcaster.mark_message_working,
    )
    handler.handle(make_message("/use node-a/dev", chat_id="oc_group", message_id="om_bind"))

    response = handler.handle(
        make_message("fix this bug", chat_id="oc_group", message_id="om_user")
    )

    assert response is None
    assert backend.sent == [("node-a", "dev", "fix this bug")]
    assert transport.reactions == []
    assert transport.deleted_reactions == []


def test_lark_websocket_proxy_patch_uses_environment_proxy(monkeypatch):
    for name in PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
    client_mod = types.ModuleType("lark_oapi.ws.client")
    client_mod._ws_connect_kwargs = lambda: {"proxy": None}
    ws_mod = types.ModuleType("lark_oapi.ws")
    ws_mod.client = client_mod
    lark_mod = types.ModuleType("lark_oapi")
    lark_mod.ws = ws_mod
    monkeypatch.setitem(sys.modules, "lark_oapi", lark_mod)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws", ws_mod)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws.client", client_mod)

    assert enable_lark_ws_env_proxy()
    assert client_mod._ws_connect_kwargs() == {"proxy": True}


def test_lark_websocket_proxy_patch_skips_without_proxy_env(monkeypatch):
    for name in PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    client_mod = types.ModuleType("lark_oapi.ws.client")
    client_mod._ws_connect_kwargs = lambda: {"proxy": None}
    ws_mod = types.ModuleType("lark_oapi.ws")
    ws_mod.client = client_mod
    lark_mod = types.ModuleType("lark_oapi")
    lark_mod.ws = ws_mod
    monkeypatch.setitem(sys.modules, "lark_oapi", lark_mod)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws", ws_mod)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws.client", client_mod)

    assert not enable_lark_ws_env_proxy()
    assert client_mod._ws_connect_kwargs() == {"proxy": None}
