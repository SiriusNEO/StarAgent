from __future__ import annotations

from staragent.models import SessionConfig, SessionStatus, SessionView
from staragent.runtime import discover_local_tmux_statuses


def build_session_views(statuses: dict[str, SessionStatus]) -> list[SessionView]:
    views: list[SessionView] = []
    for name in sorted(statuses):
        status = statuses[name]
        views.append(
            SessionView(
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
        )
    return views


def collect_session_views() -> list[SessionView]:
    return build_session_views(discover_local_tmux_statuses())
