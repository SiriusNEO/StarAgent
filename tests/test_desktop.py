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


def test_native_bundles_include_runtime_without_blocking_cargo_unit_tests() -> None:
    config = load_json(DESKTOP / "src-tauri" / "tauri.conf.json")
    sidecar = load_json(DESKTOP / "src-tauri" / "tauri.sidecar.conf.json")
    updater = load_json(DESKTOP / "src-tauri" / "tauri.updater.conf.json")

    assert "externalBin" not in config["bundle"]
    assert sidecar["bundle"]["externalBin"] == ["binaries/staragent-runtime"]
    assert updater["bundle"]["externalBin"] == ["binaries/staragent-runtime"]


def test_desktop_workflow_covers_three_operating_systems() -> None:
    workflow = (ROOT / ".github" / "workflows" / "desktop.yml").read_text(encoding="utf-8")

    assert "ubuntu-22.04" in workflow
    assert "windows-latest" in workflow
    assert "macos-15" in workflow
    assert "macos-15-intel" in workflow
    assert "aarch64-apple-darwin" in workflow
    assert "x86_64-apple-darwin" in workflow
    assert "build_runtime.py" in workflow
    assert "release:" in workflow
    assert "types: [published]" in workflow
    assert "gh release upload" in workflow
    assert "contents: write" in workflow


def test_desktop_update_channel_requires_signed_release_artifacts() -> None:
    config = load_json(DESKTOP / "src-tauri" / "tauri.conf.json")
    release_config = load_json(DESKTOP / "src-tauri" / "tauri.updater.conf.json")
    workflow = (ROOT / ".github" / "workflows" / "desktop.yml").read_text(encoding="utf-8")

    updater = config["plugins"]["updater"]
    assert updater["pubkey"]
    assert not updater["pubkey"].startswith(("/", ".", "~"))
    assert updater["endpoints"] == [
        "https://github.com/SiriusNEO/StarAgent/releases/latest/download/latest.json"
    ]
    assert release_config["bundle"]["createUpdaterArtifacts"] is True
    assert "TAURI_SIGNING_PRIVATE_KEY" in workflow
    assert "build_updater_manifest.py" in workflow
    assert "latest.json" in workflow
