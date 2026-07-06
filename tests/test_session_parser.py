from __future__ import annotations

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
    monkeypatch.setattr(transcript, "process_descendant_pids", lambda pid, max_depth=4: [20, 30])

    def fake_find_in_process(pid: int) -> str:
        return "/root/.codex/sessions/rollout-demo.jsonl" if pid == 30 else ""

    monkeypatch.setattr(transcript, "find_codex_rollout_in_process", fake_find_in_process)

    assert transcript.find_codex_rollout_by_pid(10) == "/root/.codex/sessions/rollout-demo.jsonl"
