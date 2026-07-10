from __future__ import annotations

import json

from staragent import session_parser, transcript
from staragent.adopt import AdoptedSession
from staragent.transcript import TranscriptState


def test_adopted_session_uses_live_cli_pid(monkeypatch) -> None:
    calls: dict[str, object] = {}

    monkeypatch.setattr(session_parser, "tmux_session_exists", lambda session: True)
    monkeypatch.setattr(session_parser, "capture_tmux_pane_ansi", lambda session, lines: "")
    monkeypatch.setattr(
        session_parser,
        "adopted_session",
        lambda session: AdoptedSession(
            name=session,
            target=session,
            cli="codex",
            cwd="/repo",
            pane_pid=100,
            cli_pid=1,
        ),
    )
    monkeypatch.setattr(session_parser, "infer_cli_from_pane", lambda command, pid: ("codex", 200))

    def fake_parse_transcript(text: str, cli: str = "", cli_pid: int = 0) -> TranscriptState:
        calls["cli"] = cli
        calls["cli_pid"] = cli_pid
        return TranscriptState()

    monkeypatch.setattr(session_parser, "parse_transcript", fake_parse_transcript)

    session_parser.tmux_transcript_state("dev")

    assert calls == {"cli": "codex", "cli_pid": 200}


def test_find_codex_rollout_by_pid_checks_descendants(monkeypatch) -> None:
    transcript.clear_transcript_caches()
    monkeypatch.setattr(transcript, "process_descendant_pids", lambda pid, max_depth=4: [20, 30])

    def fake_find_in_process(pid: int) -> str:
        return "/root/.codex/sessions/rollout-demo.jsonl" if pid == 30 else ""

    monkeypatch.setattr(transcript, "find_codex_rollout_in_process", fake_find_in_process)

    assert transcript.find_codex_rollout_by_pid(10) == "/root/.codex/sessions/rollout-demo.jsonl"


def test_jsonl_cache_only_parses_appended_lines(tmp_path, monkeypatch) -> None:
    transcript.clear_transcript_caches()
    path = tmp_path / "session.jsonl"
    path.write_text('{"turn": 1}\n{"turn": 2}\n', encoding="utf-8")
    real_loads = json.loads
    parsed_lines: list[bytes] = []

    def tracking_loads(value):  # type: ignore[no-untyped-def]
        parsed_lines.append(value)
        return real_loads(value)

    monkeypatch.setattr(transcript.json, "loads", tracking_loads)

    assert transcript.read_jsonl_objects(str(path)) == [{"turn": 1}, {"turn": 2}]
    assert len(parsed_lines) == 2

    with path.open("ab") as handle:
        handle.write(b'{"turn": 3}\n')

    assert transcript.read_jsonl_objects(str(path)) == [
        {"turn": 1},
        {"turn": 2},
        {"turn": 3},
    ]
    assert len(parsed_lines) == 3


def test_jsonl_cache_waits_for_complete_appended_line(tmp_path) -> None:
    transcript.clear_transcript_caches()
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"turn": 1}')

    assert transcript.read_jsonl_objects(str(path)) == []

    with path.open("ab") as handle:
        handle.write(b"\n")

    assert transcript.read_jsonl_objects(str(path)) == [{"turn": 1}]


def test_codex_lifecycle_reads_newest_turn_without_full_transcript(tmp_path, monkeypatch) -> None:
    transcript.clear_transcript_caches()
    path = tmp_path / "codex.jsonl"
    user = {
        "type": "response_item",
        "timestamp": "2026-07-10T10:00:00Z",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Implement it"}],
        },
    }
    final = {
        "type": "response_item",
        "timestamp": "2026-07-10T10:01:00Z",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Done"}],
        },
    }
    trailing = [
        {"type": "event_msg", "payload": {"type": "token_count", "info": {}}} for _ in range(25)
    ]
    path.write_text(
        "\n".join(json.dumps(item) for item in [user, final, *trailing]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(transcript, "find_codex_rollout_by_pid", lambda pid: str(path))
    real_loads = json.loads
    parsed = 0

    def tracking_loads(value):  # type: ignore[no-untyped-def]
        nonlocal parsed
        parsed += 1
        return real_loads(value)

    monkeypatch.setattr(transcript.json, "loads", tracking_loads)

    completed = transcript.detect_transcript_lifecycle("", "codex", cli_pid=42)

    assert completed.final is True
    assert completed.working is False
    assert completed.lifecycle_id
    assert parsed < 28

    first_revision = completed.lifecycle_id
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(trailing[0]) + "\n")
    completed_again = transcript.detect_transcript_lifecycle("", "codex", cli_pid=42)
    assert completed_again.lifecycle_id == first_revision

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(user) + "\n")
    working = transcript.detect_transcript_lifecycle("", "codex", cli_pid=42)
    assert working.working is True
    assert working.final is False


def test_claude_tool_use_is_working_until_end_turn(monkeypatch) -> None:
    events = [
        {
            "type": "user",
            "uuid": "user-1",
            "timestamp": "2026-07-10T10:00:00Z",
            "message": {"role": "user", "content": "Check the tests"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-tool",
            "timestamp": "2026-07-10T10:00:01Z",
            "message": {
                "role": "assistant",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "I will inspect them."},
                    {"type": "tool_use", "name": "Read", "input": {}},
                ],
            },
        },
    ]
    monkeypatch.setattr(transcript, "claude_events_from_pid", lambda pid: list(events))

    working = transcript.parse_claude_transcript("", cli_pid=42)

    assert working.working is True
    assert working.final is False
    assert working.completed_reply == ""

    events.append(
        {
            "type": "assistant",
            "uuid": "assistant-final",
            "timestamp": "2026-07-10T10:00:02Z",
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "All tests pass."}],
            },
        }
    )
    completed = transcript.parse_claude_transcript("", cli_pid=42)

    assert completed.working is False
    assert completed.final is True
    assert completed.completed_reply
