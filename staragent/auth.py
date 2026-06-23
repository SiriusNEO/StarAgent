from __future__ import annotations

import os
import secrets
from pathlib import Path

from staragent.paths import state_dir

AUTH_TOKEN_FILE_NAME = "auth_token"


def auth_token_path() -> Path:
    return state_dir() / AUTH_TOKEN_FILE_NAME


def read_stored_auth_token() -> str:
    try:
        return auth_token_path().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def write_stored_auth_token(token: str) -> None:
    token = token.strip()
    if not token:
        return
    path = auth_token_path()
    if read_stored_auth_token() == token:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(token + "\n", encoding="utf-8")
    temp_path.chmod(0o600)
    temp_path.replace(path)
    path.chmod(0o600)


def create_stored_auth_token() -> str:
    token = secrets.token_urlsafe(32)
    write_stored_auth_token(token)
    return token


def hub_auth_token() -> str:
    return os.environ.get("STARAGENT_AUTH_TOKEN", "").strip() or read_stored_auth_token()


def node_auth_token() -> str:
    return os.environ.get("STARAGENT_NODE_TOKEN", "").strip() or hub_auth_token()


def hub_auth_token_source() -> str:
    if os.environ.get("STARAGENT_AUTH_TOKEN", "").strip():
        return "environment"
    if read_stored_auth_token():
        return "state"
    return ""
