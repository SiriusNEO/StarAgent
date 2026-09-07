from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from staragent import __version__
from staragent.windows import background_process_kwargs, windows_process_argv

_APP_SERVER_EOF = object()


def codex_app_server_request(
    executable: str,
    method: str,
    *,
    timeout: float,
    params: object = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return codex_app_server_requests(
        executable,
        {method: params},
        timeout=timeout,
        env=env,
    )[method]


def codex_app_server_requests(
    executable: str,
    requests: Mapping[str, object],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    if not requests:
        return {}
    environment = dict(env) if env is not None else None
    command = [executable, "app-server", "--stdio"]
    if os.name == "nt":
        command = windows_process_argv(
            command,
            environment if environment is not None else os.environ,
            require_executable=True,
        )
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=environment,
        **background_process_kwargs(),
    )
    deadline = time.monotonic() + timeout
    try:
        send_app_server_message(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "staragent",
                        "title": "StarAgent",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        initialized = read_app_server_response(process, 1, deadline)
        raise_for_app_server_error(initialized)
        send_app_server_message(process, {"method": "initialized"})

        results: dict[str, dict[str, Any]] = {}
        for request_id, (method, params) in enumerate(requests.items(), start=2):
            send_app_server_message(
                process,
                {"id": request_id, "method": method, "params": params},
            )
            response = read_app_server_response(process, request_id, deadline)
            raise_for_app_server_error(response)
            result = response.get("result")
            if not isinstance(result, dict):
                raise ValueError(f"Codex returned an invalid {method} response.")
            results[method] = result
        return results
    finally:
        stop_app_server(process)


def send_app_server_message(
    process: subprocess.Popen[str],
    payload: Mapping[str, object],
) -> None:
    if process.stdin is None:
        raise RuntimeError("Codex app-server stdin is unavailable.")
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()


def read_app_server_response(
    process: subprocess.Popen[str],
    request_id: int,
    deadline: float,
) -> dict[str, Any]:
    stdout = process.stdout
    if stdout is None:
        raise RuntimeError("Codex app-server stdout is unavailable.")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"Codex app-server request {request_id} timed out.")

    messages: queue.Queue[object] = queue.Queue(maxsize=1)

    def read_response() -> None:
        try:
            for line in stdout:
                message = json.loads(line)
                if isinstance(message, dict) and message.get("id") == request_id:
                    messages.put(message)
                    return
        except (OSError, ValueError) as exc:
            messages.put(exc)
            return
        messages.put(_APP_SERVER_EOF)

    # Windows select() accepts Winsock sockets only, not anonymous process pipes.
    threading.Thread(
        target=read_response,
        name=f"staragent-codex-app-server-{request_id}",
        daemon=True,
    ).start()
    try:
        message = messages.get(timeout=remaining)
    except queue.Empty as exc:
        raise TimeoutError(f"Codex app-server request {request_id} timed out.") from exc
    if message is _APP_SERVER_EOF:
        raise RuntimeError(f"Codex app-server exited before request {request_id} completed.")
    if isinstance(message, Exception):
        raise message
    assert isinstance(message, dict)
    return message


def raise_for_app_server_error(response: Mapping[str, Any]) -> None:
    error = response.get("error")
    if not error:
        return
    if isinstance(error, Mapping):
        message = error.get("message") or error.get("code") or "unknown error"
    else:
        message = error
    raise RuntimeError(f"Codex app-server request failed: {clean_rpc_text(message)}")


def stop_app_server(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None:
        with suppress(OSError, ValueError):
            process.stdin.close()
    if process.poll() is None:
        with suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
    if process.stdout is not None:
        with suppress(OSError, ValueError):
            process.stdout.close()


def clean_rpc_text(value: object, *, max_chars: int = 240) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return f"{text[:max_chars]}…" if len(text) > max_chars else text
