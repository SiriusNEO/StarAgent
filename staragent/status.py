from __future__ import annotations

from staragent.models import SessionConfig, SessionStatus, SessionView
from staragent.runtime import (
    discover_local_tmux_navigation_statuses,
    discover_local_tmux_status,
    discover_local_tmux_statuses,
)


def build_session_view(status: SessionStatus) -> SessionView:
    return SessionView(
        config=SessionConfig(
            name=status.name,
            node=status.node,
            agent=status.agent or "codex",
            repo=status.repo,
            branch=status.branch,
            task=status.task,
        ),
        status_report=status,
    )


def build_session_views(statuses: dict[str, SessionStatus]) -> list[SessionView]:
    return [build_session_view(statuses[name]) for name in sorted(statuses)]


def collect_session_views() -> list[SessionView]:
    return build_session_views(discover_local_tmux_statuses())


def collect_session_navigation_views() -> list[SessionView]:
    return build_session_views(discover_local_tmux_navigation_statuses())


def collect_session_view(name: str) -> SessionView | None:
    status = discover_local_tmux_status(name)
    return build_session_view(status) if status else None
