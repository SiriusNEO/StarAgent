from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_STATE_DIR_NAME = "staragent"


def state_dir() -> Path:
    configured = os.environ.get("STARAGENT_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / APP_STATE_DIR_NAME
    return Path.home() / ".local" / "state" / APP_STATE_DIR_NAME
