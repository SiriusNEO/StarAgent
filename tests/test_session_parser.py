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
