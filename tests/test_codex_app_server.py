from __future__ import annotations

import io
import json
import subprocess

import pytest

from staragent import codex_app_server


class RecordingInput(io.StringIO):
    def close(self) -> None:
        self.was_closed = True


class FakeProcess:
    def __init__(self, output: str) -> None:
        self.stdin = RecordingInput()
        self.stdout = io.StringIO(output)

    def poll(self) -> int:
        return 0

    def terminate(self) -> None:
        raise AssertionError("A completed process must not be terminated.")

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        raise AssertionError("A completed process must not be killed.")


def test_app_server_reads_process_pipes_without_socket_select(monkeypatch) -> None:
    output = "\n".join(
        json.dumps(message)
        for message in (
            {"method": "account/updated", "params": {}},
            {"id": 1, "result": {}},
            {"id": 2, "result": {"rateLimitsByLimitId": {"codex": {}}}},
        )
    )
    process = FakeProcess(f"{output}\n")
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(command, **kwargs):  # type: ignore[no-untyped-def]
        popen_calls.append((command, kwargs))
        return process

    monkeypatch.setattr(codex_app_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        codex_app_server,
        "background_process_kwargs",
        lambda: {"creationflags": 0x0800_0000},
    )

    result = codex_app_server.codex_app_server_request(
        "/tools/codex",
        "account/rateLimits/read",
        timeout=1,
    )

    assert result == {"rateLimitsByLimitId": {"codex": {}}}
    assert popen_calls[0][0] == ["/tools/codex", "app-server", "--stdio"]
    assert popen_calls[0][1]["creationflags"] == 0x0800_0000
    sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert sent[0]["method"] == "initialize"
    assert sent[1] == {"method": "initialized"}
    assert sent[2]["method"] == "account/rateLimits/read"
    assert process.stdin.was_closed is True


def test_app_server_reports_eof_before_response(monkeypatch) -> None:
    process = FakeProcess("")
    monkeypatch.setattr(codex_app_server.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(RuntimeError, match="exited before request 1 completed"):
        codex_app_server.codex_app_server_request(
            "/tools/codex",
            "account/rateLimits/read",
            timeout=1,
        )


def test_stop_app_server_escalates_after_timeout() -> None:
    class RunningProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__("")
            self.terminated = False
            self.killed = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            if not self.killed:
                raise subprocess.TimeoutExpired("codex", timeout)
            return 0

        def kill(self) -> None:
            self.killed = True

    process = RunningProcess()

    codex_app_server.stop_app_server(process)  # type: ignore[arg-type]

    assert process.terminated is True
    assert process.killed is True
    assert process.stdin.was_closed is True
    assert process.stdout.closed is True
