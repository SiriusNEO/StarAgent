from __future__ import annotations

import importlib.util
import shutil
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
nightly = load_script("prepare_nightly_version")
runtime_builder = load_script("build_runtime")
macos_artifacts = load_script("label_macos_artifacts")


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


def test_updater_manifest_supports_rolling_nightly_release(tmp_path: Path) -> None:
    add_signed_asset(tmp_path, "StarAgent_0.2.0-dev.42_amd64.AppImage", "appimage")
    add_signed_asset(tmp_path, "StarAgent_0.2.0-dev.42_amd64.deb", "deb")
    add_signed_asset(tmp_path, "StarAgent_0.2.0-dev.42_x64-setup.exe", "nsis")
    add_signed_asset(tmp_path, "StarAgent_0.2.0-dev.42_universal.app.tar.gz", "mac")
    commit = "0123456789abcdef0123456789abcdef01234567"

    result = manifest.build_manifest(
        tmp_path,
        "SiriusNEO/StarAgent",
        "nightly",
        "Automated Nightly build.",
        "2026-09-07T12:00:00+00:00",
        version="0.2.0-dev.42",
        commit=commit.upper(),
    )

    assert result["version"] == "0.2.0-dev.42"
    assert result["commit"] == commit
    assert "/releases/download/nightly/" in result["platforms"]["linux-x86_64"]["url"]


def test_updater_manifest_maps_native_macos_architectures_separately(tmp_path: Path) -> None:
    add_signed_asset(tmp_path, "StarAgent_0.2.0_amd64.AppImage", "appimage")
    add_signed_asset(tmp_path, "StarAgent_0.2.0_amd64.deb", "deb")
    add_signed_asset(tmp_path, "StarAgent_0.2.0_x64-setup.exe", "nsis")
    add_signed_asset(tmp_path, "StarAgent_0.2.0_aarch64.app.tar.gz", "mac-arm")
    add_signed_asset(tmp_path, "StarAgent_0.2.0_x64.app.tar.gz", "mac-intel")

    result = manifest.build_manifest(
        tmp_path,
        "SiriusNEO/StarAgent",
        "v0.2.0",
        "Native desktop runtime.",
        "2026-09-07T12:00:00Z",
    )

    assert result["platforms"]["darwin-aarch64"]["signature"] == "mac-arm"
    assert result["platforms"]["darwin-x86_64"]["signature"] == "mac-intel"
    assert result["platforms"]["darwin-aarch64"]["url"].endswith(
        "/StarAgent_0.2.0_aarch64.app.tar.gz"
    )


def test_macos_artifact_label_keeps_architectures_distinct(tmp_path: Path) -> None:
    archive = tmp_path / "StarAgent.app.tar.gz"
    archive.write_bytes(b"app")
    Path(f"{archive}.sig").write_text("signature", encoding="utf-8")
    dmg = tmp_path / "StarAgent.dmg"
    dmg.write_bytes(b"dmg")

    renamed = macos_artifacts.label_artifacts(tmp_path, "aarch64")

    assert {path.name for path in renamed} == {
        "StarAgent_aarch64.app.tar.gz",
        "StarAgent_aarch64.dmg",
    }
    assert (tmp_path / "StarAgent_aarch64.app.tar.gz.sig").read_text() == "signature"


def test_runtime_builder_rejects_cross_architecture_freeze(monkeypatch) -> None:
    monkeypatch.setattr(runtime_builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(runtime_builder.platform, "machine", lambda: "arm64")

    with pytest.raises(runtime_builder.RuntimeBuildError, match="natively for x86_64"):
        runtime_builder.validate_native_target("x86_64-apple-darwin")


def test_updater_manifest_requires_every_shipped_installer_signature(tmp_path: Path) -> None:
    (tmp_path / "StarAgent_0.2.0_amd64.AppImage").write_bytes(b"installer")

    with pytest.raises(manifest.ManifestError, match="Missing updater signature"):
        manifest.signed_updater_assets(tmp_path)


@pytest.mark.parametrize("tag", ["main", "v1.2", "v01.2.3", "v1.2.3 beta"])
def test_updater_manifest_rejects_non_semver_tags(tag: str) -> None:
    with pytest.raises(manifest.ManifestError):
        manifest.release_version(tag)


@pytest.mark.parametrize("commit", ["abcdef", "../latest.json", "g" * 40])
def test_updater_manifest_rejects_untrusted_commit_metadata(commit: str) -> None:
    with pytest.raises(manifest.ManifestError):
        manifest.validate_commit(commit)


def _copy_version_fixture(tmp_path: Path) -> None:
    for relative in (
        "staragent/__init__.py",
        "desktop/package.json",
        "desktop/package-lock.json",
        "desktop/src-tauri/tauri.conf.json",
        "desktop/src-tauri/Cargo.toml",
        "desktop/src-tauri/Cargo.lock",
    ):
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def test_nightly_version_is_unique_and_synchronized(tmp_path: Path) -> None:
    _copy_version_fixture(tmp_path)
    source = next(iter(nightly.configured_versions(tmp_path).values()))

    result = nightly.prepare_nightly(tmp_path, 42, write=True)

    assert result == f"{source.split('-', 1)[0]}-dev.42"
    assert set(nightly.configured_versions(tmp_path).values()) == {result}


def test_nightly_version_refuses_inconsistent_source_versions(tmp_path: Path) -> None:
    _copy_version_fixture(tmp_path)
    package_path = tmp_path / "desktop" / "package.json"
    source = next(iter(nightly.configured_versions(tmp_path).values()))
    package_path.write_text(
        package_path.read_text(encoding="utf-8").replace(source, "9.9.9"),
        encoding="utf-8",
    )

    with pytest.raises(nightly.NightlyVersionError, match="not synchronized"):
        nightly.prepare_nightly(tmp_path, 42)
