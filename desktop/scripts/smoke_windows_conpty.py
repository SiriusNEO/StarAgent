from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from staragent.native_sessions import NativeSessionRegistry
from staragent.windows import windows_process_argv


def check_command_shim() -> None:
    with tempfile.TemporaryDirectory(prefix="staragent-shim-smoke-") as directory:
        shim_directory = Path(directory) / "command shims"
        shim_directory.mkdir()
        shim = shim_directory / "staragent-shim-smoke.cmd"
        shim.write_text(
            '@echo off\r\nif not "%~1"=="shim argument" exit /b 9\r\necho shim-ready\r\n',
            encoding="utf-8",
        )
        environment = {
            **os.environ,
            "PATH": f"{shim_directory}{os.pathsep}{os.environ['PATH']}",
        }
        command = windows_process_argv(
            ["staragent-shim-smoke", "shim argument"],
            environment,
            require_executable=True,
        )
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
        if result.returncode != 0 or "shim-ready" not in result.stdout:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                "Windows command shim did not execute through cmd.exe"
                f" (exit {result.returncode}): {detail}"
            )


def main() -> None:
    check_command_shim()
    registry = NativeSessionRegistry()
    session = registry.create(
        "native-smoke",
        str(Path.cwd()),
        "Write-Output native-ready",
        keep_shell_on_exit=False,
    )
    try:
        deadline = time.monotonic() + 10
        while b"native-ready" not in session.snapshot() and time.monotonic() < deadline:
            time.sleep(0.1)
        if b"native-ready" not in session.snapshot():
            raise RuntimeError("ConPTY session did not produce the expected output.")
    finally:
        if registry.exists("native-smoke"):
            registry.kill("native-smoke")


if __name__ == "__main__":
    main()
