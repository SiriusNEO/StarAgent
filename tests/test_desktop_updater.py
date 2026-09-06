from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "desktop" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = load_script("build_updater_manifest")


def add_signed_asset(root: Path, name: str, signature: str) -> None:
    artifact = root / name
    artifact.write_bytes(b"installer")
    Path(f"{artifact}.sig").write_text(signature, encoding="utf-8")


def test_updater_manifest_maps_bundle_specific_and_universal_targets(tmp_path: Path) -> None:
    add_signed_asset(tmp_path, "StarAgent_0.2.0_amd64.AppImage", "appimage-signature")
    add_signed_asset(tmp_path, "StarAgent_0.2.0_amd64.deb", "deb-signature")
    add_signed_asset(tmp_path, "StarAgent_0.2.0_x64-setup.exe", "nsis-signature")
    add_signed_asset(tmp_path, "StarAgent.app.tar.gz", "mac-signature")

    result = manifest.build_manifest(
        tmp_path,
        "SiriusNEO/StarAgent",
        "v0.2.0",
        "- First change\n- Second change\n",
        "2026-09-06T12:00:00Z",
    )

    assert result["version"] == "0.2.0"
    assert result["notes"] == "- First change\n- Second change"
    assert result["platforms"]["linux-x86_64-deb"]["signature"] == "deb-signature"
    assert result["platforms"]["linux-x86_64"]["signature"] == "appimage-signature"
    assert result["platforms"]["windows-x86_64-nsis"]["signature"] == "nsis-signature"
    assert result["platforms"]["darwin-aarch64-app"] == result["platforms"]["darwin-x86_64-app"]
    assert result["platforms"]["darwin-x86_64"]["url"].endswith("/StarAgent.app.tar.gz")


def test_updater_manifest_requires_every_shipped_installer_signature(tmp_path: Path) -> None:
    (tmp_path / "StarAgent_0.2.0_amd64.AppImage").write_bytes(b"installer")

    with pytest.raises(manifest.ManifestError, match="Missing updater signature"):
        manifest.signed_updater_assets(tmp_path)


@pytest.mark.parametrize("tag", ["main", "v1.2", "v01.2.3", "v1.2.3 beta"])
def test_updater_manifest_rejects_non_semver_tags(tag: str) -> None:
    with pytest.raises(manifest.ManifestError):
        manifest.release_version(tag)
