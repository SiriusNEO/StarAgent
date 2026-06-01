from __future__ import annotations

from staragent.adopt import adopted_session, infer_cli_from_pane
from staragent.runtime import capture_tmux_pane_ansi, tmux_active_pane, tmux_session_exists
from staragent.transcript import TokenUsage, TranscriptState, parse_transcript, strip_ansi


def tmux_transcript_state(session: str, lines: int = 500) -> TranscriptState:
    if not tmux_session_exists(session):
        raise ValueError(f"tmux session not found: {session}")
    output = strip_ansi(capture_tmux_pane_ansi(session, lines=max(20, min(lines, 500))))
    adopted = adopted_session(session)
    if adopted:
        return parse_transcript(output, adopted.cli, cli_pid=adopted.cli_pid)
    pane = tmux_active_pane(session)
    cli, cli_pid = infer_cli_from_pane(
        str(pane.get("current_command") or ""), int(pane.get("pane_pid") or 0)
    )
    return parse_transcript(output, cli, cli_pid=cli_pid)


def transcript_state_payload(state: TranscriptState) -> dict[str, object]:
    return {
        "reply": state.reply,
        "completed_reply": state.completed_reply,
        "working": state.working,
        "working_label": state.working_label,
        "working_since_ms": state.working_since_ms,
        "final": state.final,
        "messages": [message.as_dict() for message in state.messages],
        "token_usage": state.token_usage.as_dict() if state.token_usage else None,
    }


def transcript_state_from_payload(payload: dict[str, object]) -> TranscriptState:
    return TranscriptState(
        reply=str(payload.get("reply") or ""),
        completed_reply=str(payload.get("completed_reply") or ""),
        working=bool(payload.get("working")),
        working_label=str(payload.get("working_label") or ""),
        working_since_ms=int(payload.get("working_since_ms") or 0),
        final=bool(payload.get("final")),
        messages=tuple(transcript_messages_from_payload(payload.get("messages"))),
        token_usage=token_usage_from_payload(payload.get("token_usage")),
    )


def token_usage_from_payload(value: object) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    return TokenUsage(
        source=str(value.get("source") or ""),
        model=str(value.get("model") or ""),
        reasoning_effort=str(value.get("reasoning_effort") or ""),
        plan_type=str(value.get("plan_type") or ""),
        primary_rate_used_percent=float(value.get("primary_rate_used_percent") or 0),
        secondary_rate_used_percent=float(value.get("secondary_rate_used_percent") or 0),
        input_tokens=int(value.get("input_tokens") or 0),
        cached_input_tokens=int(value.get("cached_input_tokens") or 0),
        output_tokens=int(value.get("output_tokens") or 0),
        reasoning_output_tokens=int(value.get("reasoning_output_tokens") or 0),
        total_tokens=int(value.get("total_tokens") or 0),
        last_input_tokens=int(value.get("last_input_tokens") or 0),
        last_cached_input_tokens=int(value.get("last_cached_input_tokens") or 0),
        last_output_tokens=int(value.get("last_output_tokens") or 0),
        last_reasoning_output_tokens=int(value.get("last_reasoning_output_tokens") or 0),
        last_total_tokens=int(value.get("last_total_tokens") or 0),
        context_window=int(value.get("context_window") or 0),
        updated_at_ms=int(value.get("updated_at_ms") or 0),
    )


def transcript_messages_from_payload(value: object):
    from staragent.transcript import TranscriptMessage

    if not isinstance(value, list):
        return []
    messages = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        text = str(item.get("text") or "").strip()
        if role not in {"user", "agent", "session"} or not text:
            continue
        messages.append(
            TranscriptMessage(
                role=role,
                text=text,
                timestamp_ms=int(item.get("time") or 0),
                source_id=str(item.get("id") or ""),
            )
        )
    return messages
