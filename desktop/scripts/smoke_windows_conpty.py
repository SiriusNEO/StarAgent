from __future__ import annotations

import time
from pathlib import Path

from staragent.native_sessions import NativeSessionRegistry


def main() -> None:
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
