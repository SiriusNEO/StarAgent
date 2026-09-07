from __future__ import annotations

from pydantic import BaseModel, SecretStr


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


class HarnessConfigRequest(BaseModel):
    content: str


class HarnessEnvironmentRequest(BaseModel):
    variables: dict[str, str]


class HarnessModelPreferenceRequest(BaseModel):
    model: str = ""
    reasoning_effort: str = ""


class CodexApiKeyLoginRequest(BaseModel):
    api_key: SecretStr
