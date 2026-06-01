from __future__ import annotations

# The structured transcript readers in this module are Python ports of the
# corresponding MIT-licensed botmux readers:
#   https://github.com/deepcoldy/botmux
# StarAgent keeps the same principle: CLI-native transcript files are authoritative;
# terminal text is only a last-resort display fallback.
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKING_STATUS_PATTERN = re.compile(r"^\s*(?:[◦∙·○●⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*)?Working\b", re.IGNORECASE)


@dataclass(frozen=True)
class TranscriptMessage:
    role: str
    text: str
    timestamp_ms: int = 0
    source_id: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "text": self.text,
            "time": self.timestamp_ms,
            "id": self.source_id,
        }


@dataclass(frozen=True)
class TokenUsage:
    source: str = ""
    model: str = ""
    reasoning_effort: str = ""
    plan_type: str = ""
    primary_rate_used_percent: float = 0
    secondary_rate_used_percent: float = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    last_input_tokens: int = 0
    last_cached_input_tokens: int = 0
    last_output_tokens: int = 0
    last_reasoning_output_tokens: int = 0
    last_total_tokens: int = 0
    context_window: int = 0
    updated_at_ms: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "plan_type": self.plan_type,
            "primary_rate_used_percent": self.primary_rate_used_percent,
            "secondary_rate_used_percent": self.secondary_rate_used_percent,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
            "last_input_tokens": self.last_input_tokens,
            "last_cached_input_tokens": self.last_cached_input_tokens,
            "last_output_tokens": self.last_output_tokens,
            "last_reasoning_output_tokens": self.last_reasoning_output_tokens,
            "last_total_tokens": self.last_total_tokens,
            "context_window": self.context_window,
            "updated_at_ms": self.updated_at_ms,
        }


@dataclass(frozen=True)
class TranscriptState:
    reply: str = ""
    completed_reply: str = ""
    working: bool = False
    working_label: str = ""
    working_since_ms: int = 0
    final: bool = False
    messages: tuple[TranscriptMessage, ...] = ()
    token_usage: TokenUsage | None = None


def parse_transcript(text: str, cli: str = "", cli_pid: int = 0) -> TranscriptState:
    cli = cli.lower().strip()
    if cli in {"codex", "codex-cli"}:
        return parse_codex_transcript(text, cli_pid=cli_pid)
    if cli in {"claude", "claude-code"}:
        return parse_claude_transcript(text, cli_pid=cli_pid)
    if cli == "gemini":
        return parse_gemini_transcript(text)
    if cli == "opencode":
        return parse_opencode_transcript(text)
    return parse_generic_transcript(text)


def parse_codex_transcript(text: str, cli_pid: int = 0) -> TranscriptState:
    rollout = codex_events_from_pid(cli_pid)
    if rollout:
        messages = codex_messages_from_events(rollout)
        user_index = latest_event_index(rollout, "user")
        assistant_index = latest_event_index(rollout, "assistant_final")
        reply = rollout[assistant_index]["text"] if assistant_index >= 0 else ""
        working = user_index > assistant_index
        return TranscriptState(
            reply=reply,
            completed_reply=reply if reply else "",
            working=working,
            working_label="Working" if working else "",
            working_since_ms=event_timestamp_at(rollout, user_index) if working else 0,
            final=bool(reply and not working),
            messages=tuple(messages),
            token_usage=codex_token_usage_from_events(rollout),
        )

    lines = clean_transcript_lines(text)
    extract_session_meta(lines)
    completed_reply = extract_latest_completed_codex_reply(lines)
    reply = completed_reply or extract_last_codex_reply(lines)
    working = codex_is_working(lines)
    final = codex_has_final_report(lines)
    return TranscriptState(
        reply=reply,
        completed_reply=completed_reply,
        working=working,
        working_label=latest_working_label(lines) if working else "",
        final=final,
    )


def parse_claude_transcript(text: str, cli_pid: int = 0) -> TranscriptState:
    events = claude_events_from_pid(cli_pid)
    if events:
        messages = claude_messages_from_events(events)
        user_index = latest_claude_turn_start_index(events)
        assistant_index = latest_claude_assistant_index(events)
        reply = latest_claude_assistant_text(events)
        working = user_index > assistant_index
        return TranscriptState(
            reply=reply,
            completed_reply=reply if reply else "",
            working=working,
            working_label="Working" if working else "",
            working_since_ms=event_timestamp_at(events, user_index) if working else 0,
            final=bool(reply and not working),
            messages=tuple(messages),
            token_usage=claude_token_usage_from_events(events),
        )
    lines = clean_transcript_lines(text)
    reply = extract_last_block_after_prompt(lines, (">", "Human:", "User:"))
    return TranscriptState(
        reply=reply, completed_reply=reply, working=False, working_label="", final=bool(reply)
    )


def parse_gemini_transcript(text: str) -> TranscriptState:
    lines = clean_transcript_lines(text)
    reply = extract_last_block_after_prompt(lines, (">", "User:"))
    return TranscriptState(
        reply=reply, completed_reply=reply, working=False, working_label="", final=bool(reply)
    )


def parse_opencode_transcript(text: str) -> TranscriptState:
    lines = clean_transcript_lines(text)
    reply = extract_last_block_after_prompt(lines, (">", "User:", "user:"))
    return TranscriptState(
        reply=reply, completed_reply=reply, working=False, working_label="", final=bool(reply)
    )


def parse_generic_transcript(text: str) -> TranscriptState:
    lines = clean_transcript_lines(text)
    reply = "\n".join(lines[-20:]).strip()
    return TranscriptState(
        reply=reply, completed_reply=reply, working=False, working_label="", final=bool(reply)
    )


def codex_events_from_pid(pid: int) -> list[dict[str, object]]:
    path = find_codex_rollout_by_pid(pid)
    if not path:
        return []
    events: list[dict[str, object]] = []
    line_number = 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line_number += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = codex_event_from_json(obj)
                if event:
                    event["id"] = f"{path}:{line_number}"
                    events.append(event)
    except OSError:
        return []
    return events


def find_codex_rollout_by_pid(pid: int) -> str:
    if pid <= 0:
        return ""
    fd_dir = Path(f"/proc/{pid}/fd")
    if fd_dir.exists():
        try:
            for fd in fd_dir.iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if is_codex_rollout_path(target):
                    return target
        except OSError:
            return ""
    return ""


def is_codex_rollout_path(path: str) -> bool:
    return (
        path.endswith(".jsonl")
        and "/.codex/sessions/" in path
        and re.search(
            r"rollout-.*-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$",
            path,
            re.IGNORECASE,
        )
        is not None
    )


def codex_event_from_json(obj: object) -> dict[str, object] | None:
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == "event_msg":
        payload = obj.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            return {
                "kind": "token_count",
                "info": payload.get("info"),
                "rate_limits": payload.get("rate_limits"),
                "plan_type": payload.get("plan_type"),
                "timestamp_ms": event_timestamp_ms(obj),
            }
        return None
    if obj.get("type") == "turn_context":
        payload = obj.get("payload")
        if isinstance(payload, dict):
            collaboration_mode = payload.get("collaboration_mode")
            settings = (
                collaboration_mode.get("settings") if isinstance(collaboration_mode, dict) else {}
            )
            reasoning_effort = (
                settings.get("reasoning_effort") if isinstance(settings, dict) else ""
            )
            return {
                "kind": "metadata",
                "model": str(payload.get("model") or ""),
                "reasoning_effort": str(reasoning_effort or ""),
                "timestamp_ms": event_timestamp_ms(obj),
            }
        return None
    if obj.get("type") != "response_item":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    if payload.get("role") == "user":
        text = join_codex_text_blocks(payload.get("content"), "input_text")
        return (
            {"kind": "user", "text": text, "timestamp_ms": event_timestamp_ms(obj)}
            if text
            else None
        )
    if payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
        text = join_codex_text_blocks(payload.get("content"), "output_text")
        return (
            {"kind": "assistant_final", "text": text, "timestamp_ms": event_timestamp_ms(obj)}
            if text
            else None
        )
    return None


def codex_token_usage_from_events(events: list[dict[str, object]]) -> TokenUsage | None:
    for event in reversed(events):
        if event.get("kind") != "token_count":
            continue
        info = event.get("info")
        if not isinstance(info, dict):
            return None
        total = usage_mapping(info.get("total_token_usage"))
        last = usage_mapping(info.get("last_token_usage"))
        if not total and not last:
            return None
        metadata = latest_codex_metadata(events)
        rate_limits = event.get("rate_limits")
        return TokenUsage(
            source="codex",
            model=metadata.get("model", ""),
            reasoning_effort=metadata.get("reasoning_effort", ""),
            plan_type=codex_plan_type(event),
            primary_rate_used_percent=rate_used_percent(rate_limits, "primary"),
            secondary_rate_used_percent=rate_used_percent(rate_limits, "secondary"),
            input_tokens=usage_int(total, "input_tokens"),
            cached_input_tokens=usage_int(total, "cached_input_tokens"),
            output_tokens=usage_int(total, "output_tokens"),
            reasoning_output_tokens=usage_int(total, "reasoning_output_tokens"),
            total_tokens=usage_int(total, "total_tokens"),
            last_input_tokens=usage_int(last, "input_tokens"),
            last_cached_input_tokens=usage_int(last, "cached_input_tokens"),
            last_output_tokens=usage_int(last, "output_tokens"),
            last_reasoning_output_tokens=usage_int(last, "reasoning_output_tokens"),
            last_total_tokens=usage_int(last, "total_tokens"),
            context_window=usage_int(info, "model_context_window"),
            updated_at_ms=int(event.get("timestamp_ms") or 0),
        )
    return None


def codex_plan_type(event: dict[str, object]) -> str:
    if event.get("plan_type"):
        return str(event.get("plan_type") or "")
    rate_limits = event.get("rate_limits")
    if isinstance(rate_limits, dict) and rate_limits.get("plan_type"):
        return str(rate_limits.get("plan_type") or "")
    return ""


def latest_codex_metadata(events: list[dict[str, object]]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for event in events:
        if event.get("kind") != "metadata":
            continue
        model = str(event.get("model") or "")
        if model:
            metadata["model"] = model
        reasoning_effort = str(event.get("reasoning_effort") or "")
        metadata["reasoning_effort"] = reasoning_effort or "default"
    return metadata


def rate_used_percent(rate_limits: object, key: str) -> float:
    if not isinstance(rate_limits, dict):
        return 0
    bucket = rate_limits.get(key)
    if not isinstance(bucket, dict):
        return 0
    value = bucket.get("used_percent")
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value))
        except ValueError:
            return 0
    return 0


def event_timestamp_ms(obj: object) -> int:
    if not isinstance(obj, dict):
        return 0
    timestamp = obj.get("timestamp")
    if isinstance(timestamp, str):
        from datetime import datetime

        try:
            return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def codex_messages_from_events(events: list[dict[str, object]]) -> list[TranscriptMessage]:
    messages = []
    for event in events:
        kind = event.get("kind")
        text = str(event.get("text") or "").strip()
        if not text:
            continue
        timestamp_ms = int(event.get("timestamp_ms") or 0)
        if kind == "user":
            messages.append(
                TranscriptMessage("user", text, timestamp_ms, str(event.get("id") or ""))
            )
        elif kind == "assistant_final":
            messages.append(
                TranscriptMessage("agent", text, timestamp_ms, str(event.get("id") or ""))
            )
    return messages


def join_codex_text_blocks(content: object, kind: str) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == kind
            and isinstance(block.get("text"), str)
        ):
            parts.append(block["text"])
    return "".join(parts)


def latest_event_index(events: list[dict[str, object]], kind: str) -> int:
    for index in range(len(events) - 1, -1, -1):
        if events[index].get("kind") == kind:
            return index
    return -1


def event_timestamp_at(events: list[dict[str, object]], index: int) -> int:
    if index < 0 or index >= len(events):
        return 0
    return int(events[index].get("timestamp_ms") or 0)


def claude_events_from_pid(pid: int) -> list[dict[str, Any]]:
    path = find_claude_jsonl_by_pid(pid)
    if not path:
        return []
    events = read_jsonl_objects(path)
    return events


def find_claude_jsonl_by_pid(pid: int) -> str:
    if pid <= 0:
        return ""
    state = read_claude_pid_state(pid)
    if state:
        session_id = str(state.get("sessionId") or "")
        cwd = str(state.get("cwd") or "")
        if session_id and SESSION_UUID_RE.fullmatch(session_id) and cwd:
            path = claude_jsonl_path_for_session(session_id, cwd)
            if Path(path).exists():
                return path
    for session_id in find_open_claude_session_ids(pid):
        cwd = str(state.get("cwd") or "") if state else read_process_cwd(pid)
        if not cwd:
            continue
        path = claude_jsonl_path_for_session(session_id, cwd)
        if Path(path).exists():
            return path
    return ""


SESSION_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def read_claude_pid_state(pid: int) -> dict[str, Any] | None:
    path = Path.home() / ".claude" / "sessions" / f"{pid}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("pid") != pid:
        return None
    proc_start = data.get("procStart")
    if isinstance(proc_start, str):
        live = read_proc_starttime(pid)
        if live is not None and live != proc_start:
            return None
    return data


def read_proc_starttime(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    close_paren = raw.rfind(")")
    if close_paren < 0:
        return None
    fields = raw[close_paren + 2 :].strip().split()
    return fields[19] if len(fields) > 19 else None


def claude_jsonl_path_for_session(session_id: str, cwd: str) -> str:
    try:
        real_cwd = str(Path(cwd).resolve())
    except OSError:
        real_cwd = cwd
    project_hash = re.sub(r"[^A-Za-z0-9-]", "-", real_cwd)
    return str(Path.home() / ".claude" / "projects" / project_hash / f"{session_id}.jsonl")


def find_open_claude_session_ids(pid: int) -> list[str]:
    fd_dir = Path(f"/proc/{pid}/fd")
    if not fd_dir.exists():
        return []
    tasks_prefix = str(Path.home() / ".claude" / "tasks") + "/"
    project_marker = "/.claude/projects/"
    session_ids: set[str] = set()
    try:
        fds = list(fd_dir.iterdir())
    except OSError:
        return []
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith(tasks_prefix):
            session_id = target[len(tasks_prefix) :].split("/", 1)[0]
            if SESSION_UUID_RE.fullmatch(session_id):
                session_ids.add(session_id)
            continue
        if target.endswith(".jsonl") and project_marker in target:
            session_id = Path(target).name.removesuffix(".jsonl")
            if SESSION_UUID_RE.fullmatch(session_id):
                session_ids.add(session_id)
    return sorted(session_ids)


def read_process_cwd(pid: int) -> str:
    try:
        return str(Path(f"/proc/{pid}/cwd").resolve())
    except OSError:
        return ""


def read_jsonl_objects(path: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
    except OSError:
        return []
    return events


def latest_claude_turn_start_index(events: list[dict[str, Any]]) -> int:
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if is_meaningful_claude_user_event(event) or is_meaningful_claude_queued_command(event):
            return index
    return -1


def latest_claude_assistant_index(events: list[dict[str, Any]]) -> int:
    for index in range(len(events) - 1, -1, -1):
        if claude_assistant_text(events[index]):
            return index
    return -1


def latest_claude_assistant_text(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    seen_assistant = False
    for event in reversed(events):
        if is_meaningful_claude_user_event(event) or is_meaningful_claude_queued_command(event):
            if seen_assistant:
                break
            continue
        text = claude_assistant_text(event)
        if text:
            seen_assistant = True
            parts.append(text)
    return "\n\n".join(reversed(parts)).strip()


def claude_messages_from_events(events: list[dict[str, Any]]) -> list[TranscriptMessage]:
    messages: list[TranscriptMessage] = []
    assistant_parts: list[str] = []
    assistant_time = 0
    assistant_source_id = ""

    def flush_assistant() -> None:
        nonlocal assistant_parts, assistant_time, assistant_source_id
        text = "\n\n".join(part for part in assistant_parts if part).strip()
        if text:
            messages.append(TranscriptMessage("agent", text, assistant_time, assistant_source_id))
        assistant_parts = []
        assistant_time = 0
        assistant_source_id = ""

    for event in events:
        if is_meaningful_claude_user_event(event) or is_meaningful_claude_queued_command(event):
            flush_assistant()
            text = claude_turn_start_text(event).strip()
            if text:
                messages.append(
                    TranscriptMessage(
                        "user", text, event_timestamp_ms(event), str(event.get("uuid") or "")
                    )
                )
            continue
        text = claude_assistant_text(event)
        if text:
            assistant_parts.append(text)
            assistant_time = event_timestamp_ms(event) or assistant_time
            assistant_source_id = str(event.get("uuid") or assistant_source_id)
    flush_assistant()
    return messages


def claude_token_usage_from_events(events: list[dict[str, Any]]) -> TokenUsage | None:
    total_input = 0
    total_cached = 0
    total_output = 0
    total_reasoning = 0
    total_tokens = 0
    last_usage: dict[str, Any] = {}
    updated_at_ms = 0

    for event in events:
        usage = first_usage_mapping(event)
        if not usage:
            continue
        input_tokens = usage_int(usage, "input_tokens")
        cached_tokens = (
            usage_int(usage, "cached_input_tokens")
            or usage_int(usage, "cache_read_input_tokens")
            or usage_int(usage, "cache_creation_input_tokens")
        )
        output_tokens = usage_int(usage, "output_tokens")
        reasoning_tokens = usage_int(usage, "reasoning_output_tokens")
        event_total = usage_int(usage, "total_tokens") or input_tokens + output_tokens
        total_input += input_tokens
        total_cached += cached_tokens
        total_output += output_tokens
        total_reasoning += reasoning_tokens
        total_tokens += event_total
        last_usage = usage
        updated_at_ms = event_timestamp_ms(event) or updated_at_ms

    if not last_usage:
        return None
    return TokenUsage(
        source="claude",
        input_tokens=total_input,
        cached_input_tokens=total_cached,
        output_tokens=total_output,
        reasoning_output_tokens=total_reasoning,
        total_tokens=total_tokens,
        last_input_tokens=usage_int(last_usage, "input_tokens"),
        last_cached_input_tokens=(
            usage_int(last_usage, "cached_input_tokens")
            or usage_int(last_usage, "cache_read_input_tokens")
            or usage_int(last_usage, "cache_creation_input_tokens")
        ),
        last_output_tokens=usage_int(last_usage, "output_tokens"),
        last_reasoning_output_tokens=usage_int(last_usage, "reasoning_output_tokens"),
        last_total_tokens=usage_int(last_usage, "total_tokens")
        or usage_int(last_usage, "input_tokens") + usage_int(last_usage, "output_tokens"),
        updated_at_ms=updated_at_ms,
    )


def first_usage_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    direct = usage_mapping(value.get("usage"))
    if direct:
        return direct
    message = value.get("message")
    if isinstance(message, dict):
        nested = usage_mapping(message.get("usage"))
        if nested:
            return nested
    return {}


def usage_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    if any(
        key in value
        for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens")
    ):
        return value
    return {}


def usage_int(mapping: dict[str, Any], key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def claude_turn_start_text(event: dict[str, Any]) -> str:
    if is_meaningful_claude_queued_command(event):
        attachment = event.get("attachment")
        prompt = attachment.get("prompt") if isinstance(attachment, dict) else ""
        return prompt if isinstance(prompt, str) else stringify_claude_user_content(prompt)
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return stringify_claude_user_content(content)


def is_meaningful_claude_user_event(event: dict[str, Any]) -> bool:
    message = event.get("message")
    role = message.get("role") if isinstance(message, dict) else event.get("type")
    if role != "user":
        return False
    if (
        event.get("isMeta") is True
        or event.get("isCompactSummary") is True
        or event.get("isSidechain") is True
    ):
        return False
    content = message.get("content") if isinstance(message, dict) else None
    if is_pure_tool_result_user_event(content):
        return False
    text = normalize_fingerprint(stringify_claude_user_content(content))
    if not text:
        return False
    return not any(text.startswith(prefix) for prefix in SYNTHETIC_CLAUDE_USER_PREFIXES)


def is_meaningful_claude_queued_command(event: dict[str, Any]) -> bool:
    if event.get("type") != "attachment" or event.get("isSidechain") is True:
        return False
    attachment = event.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return False
    prompt = attachment.get("prompt")
    text = normalize_fingerprint(
        prompt if isinstance(prompt, str) else stringify_claude_user_content(prompt)
    )
    if not text:
        return False
    return not any(text.startswith(prefix) for prefix in SYNTHETIC_CLAUDE_USER_PREFIXES)


SYNTHETIC_CLAUDE_USER_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)


def is_pure_tool_result_user_event(content: object) -> bool:
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)
    )


def stringify_claude_user_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif isinstance(block.get("content"), str):
            parts.append(block["content"])
    return "\n".join(parts)


def normalize_fingerprint(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def claude_assistant_text(event: dict[str, Any]) -> str:
    if event.get("isSidechain") is True:
        return ""
    message = event.get("message")
    role = message.get("role") if isinstance(message, dict) else event.get("type")
    if role != "assistant" or not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            parts.append(block["text"])
    return "\n\n".join(parts).strip()


def clean_transcript_lines(text: str) -> list[str]:
    return [line.rstrip() for line in strip_ansi(text).splitlines() if line.strip()]


def extract_session_meta(lines: list[str]) -> str:
    for index in range(len(lines) - 1, max(-1, len(lines) - 13), -1):
        line = lines[index].strip()
        if (
            re.search(r"(gpt|codex|claude|gemini|sonnet|opus|haiku)", line, re.IGNORECASE)
            and re.search(r"[·•]", line)
            and re.search(r"(~|/)", line)
        ):
            lines.pop(index)
            return line
    return ""


def extract_last_codex_reply(lines: list[str]) -> str:
    while lines and re.search(r"^(›|◦)", lines[-1].strip()):
        lines.pop()
    worked_index = -1
    for index, line in enumerate(lines):
        if "Worked for" in line:
            worked_index = index
    if worked_index >= 0:
        start = -1
        for index in range(worked_index - 1, -1, -1):
            if re.search(r"─{6,}", lines[index]):
                start = index
                break
        return "\n".join(lines[start + 1 : worked_index]).strip()
    prompt_index = -1
    for index in range(len(lines) - 1, -1, -1):
        if re.search(r"^›", lines[index].strip()):
            prompt_index = index
            break
    return "\n".join(lines[prompt_index + 1 :]).strip()


def extract_latest_completed_codex_reply(lines: list[str]) -> str:
    prompt_indices = [index for index, line in enumerate(lines) if re.search(r"^›", line.strip())]
    separator_indices = [index for index, line in enumerate(lines) if re.search(r"─{6,}", line)]

    for prompt_index in reversed(prompt_indices):
        closing = latest_index_before(separator_indices, prompt_index)
        if closing < 0:
            continue
        opening = latest_index_before(separator_indices, closing)
        if opening < 0:
            continue
        reply = "\n".join(lines[opening + 1 : closing]).strip()
        if looks_like_completed_agent_reply(reply):
            return reply
    return extract_latest_worked_report(lines)


def extract_last_block_after_prompt(lines: list[str], prompts: tuple[str, ...]) -> str:
    prompt_index = -1
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if any(stripped.startswith(prompt) for prompt in prompts):
            prompt_index = index
            break
    block = lines[prompt_index + 1 :] if prompt_index >= 0 else lines[-20:]
    return "\n".join(line for line in block if not is_status_line(line)).strip()


def extract_latest_worked_report(lines: list[str]) -> str:
    worked_index = latest_matching_line(lines, r"Worked for")
    if worked_index < 0:
        return ""
    start = -1
    for index in range(worked_index - 1, -1, -1):
        if re.search(r"─{6,}", lines[index]):
            start = index
            break
    return "\n".join(lines[start + 1 : worked_index]).strip()


def codex_has_final_report(lines: list[str]) -> bool:
    tail = lines[-120:]
    worked_index = latest_matching_line(tail, r"Worked for")
    working_index = latest_working_line_index(tail)
    return worked_index >= 0 and worked_index > working_index


def codex_is_working(lines: list[str]) -> bool:
    tail = lines[-120:]
    worked_index = latest_matching_line(tail, r"Worked for")
    working_index = latest_working_line_index(tail)
    return working_index >= 0 and working_index > worked_index


def latest_working_label(lines: list[str]) -> str:
    index = latest_working_line_index(lines[-120:])
    if index < 0:
        return ""
    return normalize_working_label(lines[-120:][index])


def latest_working_line_index(lines: list[str]) -> int:
    for index in range(len(lines) - 1, -1, -1):
        if WORKING_STATUS_PATTERN.search(lines[index]):
            return index
    return -1


def normalize_working_label(line: str) -> str:
    match = WORKING_STATUS_PATTERN.search(line.strip())
    if not match:
        return "Working"
    label = line.strip()[match.start() :]
    return re.sub(r"\s+", " ", label).strip() or "Working"


def latest_matching_line(lines: list[str], pattern: str) -> int:
    for index in range(len(lines) - 1, -1, -1):
        if re.search(pattern, lines[index], re.IGNORECASE):
            return index
    return -1


def latest_index_before(indices: list[int], value: int) -> int:
    for index in reversed(indices):
        if index < value:
            return index
    return -1


def looks_like_completed_agent_reply(text: str) -> bool:
    if not text:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    if any(is_status_line(line) for line in lines[:3]):
        return False
    return any(
        line.startswith(("• ", "- ")) or re.search(r"[\u4e00-\u9fff]", line) for line in lines[:4]
    )


def is_status_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(
        ("• Ran ", "• Edited ", "• Explored", "• Read ", "◦ Working", "Working")
    )


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
