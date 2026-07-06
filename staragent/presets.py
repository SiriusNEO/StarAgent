from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandPreset:
    name: str
    label: str
    command: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "label": self.label, "command": self.command}


COMMAND_PRESETS = [
    CommandPreset("codex-yolo", "Codex YOLO", "codex --yolo"),
    CommandPreset("codex", "Codex", "codex"),
    CommandPreset(
        "claude-skip", "Claude Skip Permissions", "claude --dangerously-skip-permissions"
    ),
    CommandPreset("claude", "Claude", "claude"),
    CommandPreset("gemini", "Gemini", "gemini"),
    CommandPreset("opencode", "OpenCode", "opencode"),
    CommandPreset("shell", "Shell", "bash"),
]


def command_presets_payload() -> list[dict[str, str]]:
    return [preset.as_dict() for preset in COMMAND_PRESETS]


def preset_command(name: str) -> str:
    normalized = name.strip().lower()
    for preset in COMMAND_PRESETS:
        if preset.name == normalized:
            return preset.command
    raise KeyError(name)


def preset_names() -> str:
    return ", ".join(preset.name for preset in COMMAND_PRESETS)
