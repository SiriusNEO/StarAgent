from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import suppress

from staragent.event_log import (
    append_hub_event,
    append_node_outbox_event,
    infer_process_output_level,
    redact_log_text,
)

RESTART_DELAY_SECONDS = 2.0
MAX_RESTART_DELAY_SECONDS = 30.0
STABLE_PROCESS_SECONDS = 60.0


def supervise_service(command: Sequence[str], *, service: str) -> int:
    if service not in {"hub", "node"}:
        raise ValueError(f"Unsupported service: {service}")
    command = [str(part) for part in command]
    stopping = threading.Event()
    child: subprocess.Popen[str] | None = None
    restart_count = 0

    def request_stop(signum, _frame) -> None:  # type: ignore[no-untyped-def]
        stopping.set()
        if child is None or child.poll() is not None:
            return
        with suppress(OSError):
            os.killpg(child.pid, signum)

    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    previous_handlers = {value: signal.getsignal(value) for value in handled_signals}
    for value in handled_signals:
        signal.signal(value, request_stop)

    write_service_event(
        service,
        "info",
        "supervisor.started",
        f"{service_label(service)} supervisor started.",
        details={"pid": os.getpid()},
    )
    try:
        while not stopping.is_set():
            started_at = time.monotonic()
            try:
                child = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            except OSError as exc:
                restart_count += 1
                delay = restart_delay(restart_count)
                write_service_event(
                    service,
                    "error",
                    "service.start_failed",
                    f"{service_label(service)} process could not start: {exc}",
                    details={"restart_in_seconds": delay},
                )
                if stopping.wait(delay):
                    break
                continue

            write_service_event(
                service,
                "info",
                "service.started",
                f"{service_label(service)} process started.",
                details={"pid": child.pid, "restart_count": restart_count},
            )
            if child.stdout is not None:
                for raw_line in child.stdout:
                    line = raw_line.rstrip("\r\n")
                    sys.stdout.write(raw_line)
                    sys.stdout.flush()
                    if line:
                        write_service_event(
                            service,
                            infer_process_output_level(line),
                            "service.output",
                            line,
                            source=f"{service}.process",
                            details={"pid": child.pid},
                        )
            exit_code = child.wait()
            uptime = max(0.0, time.monotonic() - started_at)
            if stopping.is_set():
                write_service_event(
                    service,
                    "info",
                    "service.stopped",
                    f"{service_label(service)} process stopped.",
                    details={"pid": child.pid, "exit_code": exit_code},
                )
                break

            restart_count = 1 if uptime >= STABLE_PROCESS_SECONDS else restart_count + 1
            delay = restart_delay(restart_count)
            level = "warning" if exit_code == 0 else "error"
            write_service_event(
                service,
                level,
                "service.exited",
                f"{service_label(service)} process exited unexpectedly with code {exit_code}.",
                details={
                    "pid": child.pid,
                    "exit_code": exit_code,
                    "uptime_seconds": round(uptime, 3),
                    "restart_in_seconds": delay,
                },
            )
            if stopping.wait(delay):
                break
    finally:
        if child is not None and child.poll() is None:
            with suppress(OSError):
                os.killpg(child.pid, signal.SIGTERM)
        for value, handler in previous_handlers.items():
            signal.signal(value, handler)
        write_service_event(
            service,
            "info",
            "supervisor.stopped",
            f"{service_label(service)} supervisor stopped.",
            details={"pid": os.getpid()},
        )
    return 0


def write_service_event(
    service: str,
    level: str,
    event: str,
    message: str,
    *,
    source: str = "",
    details: dict[str, object] | None = None,
) -> None:
    try:
        if service == "hub":
            append_hub_event(
                level,
                event,
                redact_log_text(message),
                source=source or "hub.supervisor",
                details=details,
            )
        else:
            append_node_outbox_event(
                level,
                event,
                redact_log_text(message),
                source=source or "node.supervisor",
                details=details,
            )
    except (OSError, ValueError, TypeError):
        # Logging must never take down the service supervisor.
        pass


def restart_delay(restart_count: int) -> float:
    exponent = max(0, min(restart_count - 1, 10))
    return min(MAX_RESTART_DELAY_SECONDS, RESTART_DELAY_SECONDS * (2**exponent))


def service_label(service: str) -> str:
    return "Hub" if service == "hub" else "Node"
