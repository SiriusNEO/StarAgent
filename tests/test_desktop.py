from __future__ import annotations

import json
import tomllib
from pathlib import Path

from staragent import __version__

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_desktop_versions_follow_staragent_release() -> None:
    package = load_json(DESKTOP / "package.json")
    config = load_json(DESKTOP / "src-tauri" / "tauri.conf.json")
    cargo = tomllib.loads((DESKTOP / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))

    assert package["version"] == __version__
    assert config["version"] == __version__
    assert cargo["package"]["version"] == __version__


def test_remote_dashboard_has_no_desktop_capability() -> None:
    capability = load_json(DESKTOP / "src-tauri" / "capabilities" / "main.json")
    config = load_json(DESKTOP / "src-tauri" / "tauri.conf.json")

    assert capability["windows"] == ["main"]
    assert config["app"]["withGlobalTauri"] is False
    assert "dangerousRemoteDomainIpcAccess" not in config["app"]["security"]


def test_desktop_workflow_covers_three_operating_systems() -> None:
    workflow = (ROOT / ".github" / "workflows" / "desktop.yml").read_text(encoding="utf-8")

    assert "ubuntu-22.04" in workflow
    assert "windows-latest" in workflow
    assert "macos-latest" in workflow
    assert "universal-apple-darwin" in workflow
    assert "release:" in workflow
    assert "types: [published]" in workflow
    assert "gh release upload" in workflow
    assert "contents: write" in workflow
