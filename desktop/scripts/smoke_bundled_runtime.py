from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError(f"Unexpected response from {url}")
    return value


def wait_for(check, *, attempts: int, interval: float = 0.25):  # type: ignore[no-untyped-def]
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            value = check()
            if value:
                return value
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(interval)
    if last_error:
        raise RuntimeError(str(last_error)) from last_error
    raise RuntimeError("Bundled runtime smoke check timed out.")


def smoke_runtime(executable: Path, backend: str, port: int) -> None:
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            str(executable),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--mode",
            "launcher",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session = "packaged-native-smoke"
    created = False
    try:
        identity = wait_for(lambda: request_json(f"{base}/api/runtime"), attempts=80)
        if (
            identity.get("desktop_bundled") is not True
            or identity.get("session_backend") != backend
        ):
            raise RuntimeError(f"Unexpected bundled runtime identity: {identity}")
        with tempfile.TemporaryDirectory(prefix="staragent-runtime-smoke-") as cwd:
            request_json(
                f"{base}/api/workers",
                method="POST",
                payload={
                    "node": "local",
                    "name": session,
                    "cwd": cwd,
                    "command": "printf packaged-native-pty-ready",
                },
            )
            created = True

            def terminal_ready() -> bool:
                capture = request_json(
                    f"{base}/api/sessions/{urllib.parse.quote(session)}/output?lines=80"
                )
                return "packaged-native-pty-ready" in str(capture.get("output") or "")

            wait_for(terminal_ready, attempts=40)
    finally:
        if created:
            with contextlib.suppress(OSError, RuntimeError, urllib.error.URLError):
                request_json(
                    f"{base}/api/nodes/local/sessions/{urllib.parse.quote(session)}",
                    method="DELETE",
                )
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test a bundled StarAgent runtime.")
    parser.add_argument("executable", type=Path)
    parser.add_argument("--backend", required=True, choices=("pty", "conpty"))
    parser.add_argument("--port", type=int, default=18765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    smoke_runtime(args.executable.resolve(), args.backend, args.port)


if __name__ == "__main__":
    main()
