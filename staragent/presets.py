from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandPreset:
    name: str
    label: str
    command: str
    agent: str
    ops_compatible: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "command": self.command,
            "agent": self.agent,
            "ops_compatible": self.ops_compatible,
        }


COMMAND_PRESETS = [
    CommandPreset("codex-yolo", "Codex YOLO", "codex --yolo", "codex"),
    CommandPreset("codex", "Codex", "codex", "codex"),
    CommandPreset(
        "claude-skip",
        "Claude Skip Permissions",
        "claude --dangerously-skip-permissions",
        "claude",
    ),
    CommandPreset("claude", "Claude", "claude", "claude"),
    CommandPreset("opencode", "OpenCode", "opencode", "opencode"),
    CommandPreset("shell", "Shell", "bash", "shell", ops_compatible=False),
]


def command_presets_payload(*, ops_only: bool = False) -> list[dict[str, object]]:
    return [preset.as_dict() for preset in COMMAND_PRESETS if not ops_only or preset.ops_compatible]


def preset_command(name: str) -> str:
    normalized = name.strip().lower()
    for preset in COMMAND_PRESETS:
        if preset.name == normalized:
            return preset.command
    raise KeyError(name)


def preset_names() -> str:
    return ", ".join(preset.name for preset in COMMAND_PRESETS)
