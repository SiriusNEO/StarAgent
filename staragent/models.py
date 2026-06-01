from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SessionConfig:
    name: str
    node: str
    agent: str = "codex"
    session: str = ""
    repo: str = ""
    branch: str = ""
    task: str = ""


@dataclass(frozen=True)
class SessionStatus:
    name: str
    agent: str = ""
    node: str = ""
    repo: str = ""
    branch: str = ""
    task: str = ""
    status: str = "unknown"
    summary: str = ""
    next_step: str = ""
    needs_attention: bool = False
    question: str = ""
    changed_files: tuple[str, ...] = ()
    test_command: str = ""
    test_result: str = ""
    recent_output: str = ""
    source: str = "status"
    session_type: str = "agent"
    last_updated: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionStatus:
        changed_files = data.get("changed_files") or ()
        last_updated = parse_datetime(data.get("last_updated"))
        return cls(
            name=str(data.get("name") or ""),
            agent=str(data.get("agent") or ""),
            node=str(data.get("node") or ""),
            repo=str(data.get("repo") or ""),
            branch=str(data.get("branch") or ""),
            task=str(data.get("task") or ""),
            status=str(data.get("status") or "unknown"),
            summary=str(data.get("summary") or ""),
            next_step=str(data.get("next_step") or ""),
            needs_attention=bool(data.get("needs_attention")),
            question=str(data.get("question") or ""),
            changed_files=tuple(str(item) for item in changed_files),
            test_command=str(data.get("test_command") or ""),
            test_result=str(data.get("test_result") or ""),
            recent_output=str(data.get("recent_output") or ""),
            source=str(data.get("source") or "status"),
            session_type=str(data.get("session_type") or "agent"),
            last_updated=last_updated,
        )


@dataclass(frozen=True)
class SessionView:
    config: SessionConfig
    status_report: SessionStatus | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def agent(self) -> str:
        return (
            self.status_report.agent or self.config.agent
            if self.status_report
            else self.config.agent
        )

    @property
    def backend(self) -> str:
        return "tmux"

    @property
    def node_name(self) -> str:
        return (
            self.status_report.node or self.config.node if self.status_report else self.config.node
        )

    @property
    def repo(self) -> str:
        return (
            self.status_report.repo or self.config.repo if self.status_report else self.config.repo
        )

    @property
    def repo_name(self) -> str:
        repo = self.repo.rstrip("/")
        return Path(repo).name if repo else "-"

    @property
    def branch(self) -> str:
        return (
            self.status_report.branch or self.config.branch
            if self.status_report
            else self.config.branch
        )

    @property
    def task(self) -> str:
        return (
            self.status_report.task or self.config.task if self.status_report else self.config.task
        )

    @property
    def status(self) -> str:
        return self.status_report.status if self.status_report else "missing"

    @property
    def session_type(self) -> str:
        return self.status_report.session_type if self.status_report else "agent"

    @property
    def is_agent_session(self) -> bool:
        return self.session_type == "agent"

    @property
    def needs_attention(self) -> bool:
        return bool(self.status_report and self.status_report.needs_attention)

    @property
    def last_updated(self) -> datetime | None:
        return self.status_report.last_updated if self.status_report else None


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
