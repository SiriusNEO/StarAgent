from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_STATE_DIR_NAME = ".staragent"


def state_dir() -> Path:
    configured = os.environ.get("STARAGENT_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return PROJECT_ROOT / APP_STATE_DIR_NAME
