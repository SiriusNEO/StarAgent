from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn


def default_state_directory() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return root / "StarAgent" / "state"
    return Path.home() / ".staragent"


def prepare_desktop_environment() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    os.environ.setdefault("STARAGENT_STATE_DIR", str(default_state_directory()))
    if os.name == "nt":
        from staragent.native_sessions import augmented_windows_path

        os.environ["PATH"] = augmented_windows_path()
        home = Path.home()
        if home.is_dir():
            os.chdir(home)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Bundled StarAgent Desktop runtime")
    result.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost", "::1"))
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--mode", default="launcher", choices=("launcher",))
    return result


def main() -> None:
    args = parser().parse_args()
    if not 1024 <= args.port <= 65535:
        parser().error("port must be between 1024 and 65535")
    prepare_desktop_environment()
    os.environ["STARAGENT_DASHBOARD_MODE"] = args.mode
    os.environ["STARAGENT_DESKTOP_BUNDLED"] = "1"

    from staragent.dashboard.app import create_app

    uvicorn.run(
        create_app(mode=args.mode),
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
