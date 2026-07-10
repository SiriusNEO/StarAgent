from __future__ import annotations

from staragent import session_seen
from staragent.hub import HubSession, mark_hub_session_seen
from staragent.models import (
    SessionConfig,
    SessionStatus,
    SessionView,
    normalize_session_status,
)


def make_hub_session(*, question: str = "") -> HubSession:
    report = SessionStatus.from_dict(
        {
            "name": "dev",
            "status": "review",
            "status_revision": "completion-1",
            "question": question,
        }
    )
    return HubSession(
        node_id="local",
        view=SessionView(SessionConfig(name="dev", node="local"), report),
    )


def test_legacy_tmux_activity_statuses_fall_back_to_idle() -> None:
    assert normalize_session_status("active") == "idle"
    assert normalize_session_status("attached") == "idle"
    assert normalize_session_status("unknown") == "idle"
    assert normalize_session_status("running") == "working"
    assert normalize_session_status("review") == "review"
    assert normalize_session_status("waiting") == "review"


def test_attention_flag_always_normalizes_to_review() -> None:
    status = SessionStatus.from_dict({"name": "dev", "status": "active", "needs_attention": True})

    assert status.status == "review"
    assert status.needs_attention is True


def test_completed_session_becomes_idle_after_it_is_seen(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(session_seen, "SESSION_SEEN_PATH", tmp_path / "seen.json")
    session = make_hub_session()

    assert session.status == "review"
    assert session.needs_attention is True
    assert mark_hub_session_seen(session) is True
    assert session.status == "idle"
    assert session.needs_attention is False


def test_input_prompt_stays_in_review_after_session_is_seen(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(session_seen, "SESSION_SEEN_PATH", tmp_path / "seen.json")
    session = make_hub_session(question="Do you want to proceed?")

    assert mark_hub_session_seen(session) is False
    assert session.status == "review"
