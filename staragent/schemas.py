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
