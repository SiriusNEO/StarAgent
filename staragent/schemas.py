from __future__ import annotations

from pydantic import BaseModel


class SendMessage(BaseModel):
    text: str


class TerminalInput(BaseModel):
    data: str


class CreateWorker(BaseModel):
    name: str
    cwd: str
    command: str


class CreateDirectory(BaseModel):
    path: str
    name: str


class SessionPayload(BaseModel):
    name: str
    agent: str = ""
    node: str = ""
    repo: str = ""
    branch: str = ""
    task: str = ""
    status: str = "unknown"
    summary: str = ""
    needs_attention: bool = False
    question: str = ""
    source: str = "tmux"
    last_updated: str | None = None
