from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from staragent.integrations.feishu import (
    FeishuCommandHandler,
    FeishuConfig,
    IncomingFeishuMessage,
    SenderIdentity,
    extract_message_text,
)


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

    def list_sessions(self):
        return self.sessions

    def send_message(self, node_id: str, session: str, text: str) -> None:
        self.sent.append((node_id, session, text))

    def tail_session(self, node_id: str, session: str, lines: int) -> str:
        return f"tail {node_id}/{session} {lines}"

    def session_url(self, node_id: str, session: str) -> str:
        return f"https://staragent.test/nodes/{node_id}/sessions/{session}"


def make_message(text: str, open_id: str = "ou_allowed", chat_id: str = "oc_chat"):
    return IncomingFeishuMessage(
        message_id="om_1",
        chat_id=chat_id,
        chat_type="group",
        text=text,
        sender=SenderIdentity(open_id=open_id),
    )


def test_feishu_config_requires_explicit_access_scope():
    config = FeishuConfig(
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
    handler = FeishuCommandHandler(backend)

    response = handler.handle(make_message("staragent sessions"))

    assert response is not None
    assert "local/dev" in response
    assert "active" in response


def test_command_handler_sends_to_agent_session():
    backend = FakeBackend([FakeSession("node-a", FakeView("dev"))])
    handler = FeishuCommandHandler(backend)

    response = handler.handle(make_message("/send node-a/dev fix this bug"))

    assert response == "Sent to node-a/dev."
    assert backend.sent == [("node-a", "dev", "fix this bug")]


def test_command_handler_rejects_system_session_send():
    backend = FakeBackend([FakeSession("local", FakeView("staragent-hub", session_type="system"))])
    handler = FeishuCommandHandler(backend)

    response = handler.handle(make_message("/send local/staragent-hub restart"))

    assert response == "System sessions are read-only; /send only supports agent sessions."
    assert backend.sent == []


def test_command_handler_requires_node_for_ambiguous_session_name():
    backend = FakeBackend(
        [
            FakeSession("local", FakeView("dev")),
            FakeSession("node-a", FakeView("dev")),
        ]
    )
    handler = FeishuCommandHandler(backend)

    response = handler.handle(make_message("/status dev"))

    assert response is not None
    assert "Ambiguous session name" in response
