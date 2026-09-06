from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

SEMVER = re.compile(
    r"^(?:v)?(?P<version>0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class ManifestError(RuntimeError):
    pass


def release_version(tag: str) -> str:
    match = SEMVER.fullmatch(tag.strip())
    if not match:
        raise ManifestError(f"Release tag is not valid SemVer: {tag!r}")
    return tag.strip().removeprefix("v")


def validate_published_at(value: str) -> str:
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ManifestError(f"Release date is not RFC 3339: {value!r}") from error
    if parsed.tzinfo is None:
        raise ManifestError("Release date must include a timezone.")
    return value


def validate_commit(value: str) -> str:
    value = value.strip()
    if not GIT_OBJECT_ID.fullmatch(value):
        raise ManifestError("Commit must be a full 40- or 64-character Git object ID.")
    return value.lower()


def updater_kind(path: Path) -> str | None:
    name = path.name.lower()
    if name.endswith(".appimage"):
        return "appimage"
    if name.endswith(".deb"):
        return "deb"
    if name.endswith(".rpm"):
        return "rpm"
    if name.endswith("-setup.exe"):
        return "nsis"
    if name.endswith(".msi"):
        return "msi"
    if name.endswith(".app.tar.gz"):
        return "app"
    return None


def platform_keys(kind: str) -> tuple[str, ...]:
    if kind == "app":
        return ("darwin-aarch64-app", "darwin-x86_64-app")
    if kind in {"appimage", "deb", "rpm"}:
        return (f"linux-x86_64-{kind}",)
    if kind in {"nsis", "msi"}:
        return (f"windows-x86_64-{kind}",)
    raise ManifestError(f"Unsupported updater kind: {kind}")


def download_url(repository: str, tag: str, filename: str) -> str:
    parts = repository.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ManifestError(f"Repository must be in owner/name form: {repository!r}")
    return (
        f"https://github.com/{parts[0]}/{parts[1]}/releases/download/"
        f"{quote(tag, safe='')}/{quote(filename, safe='')}"
    )


def signed_updater_assets(asset_dir: Path) -> dict[str, tuple[Path, str]]:
    assets: dict[str, tuple[Path, str]] = {}
    unsigned = [
        path for path in asset_dir.rglob("*") if path.is_file() and updater_kind(path) is not None
    ]
    for artifact in unsigned:
        signature_path = Path(f"{artifact}.sig")
        if not signature_path.is_file():
            raise ManifestError(f"Missing updater signature for {artifact.name}")
        signature = signature_path.read_text(encoding="utf-8").strip()
        if not signature:
            raise ManifestError(f"Updater signature is empty: {signature_path.name}")
        kind = updater_kind(artifact)
        assert kind is not None
        if kind in assets:
            raise ManifestError(
                f"More than one {kind} updater artifact was found: "
                f"{assets[kind][0].name}, {artifact.name}"
            )
        assets[kind] = (artifact, signature)

    required = {"appimage", "deb", "nsis", "app"}
    missing = sorted(required - assets.keys())
    if missing:
        raise ManifestError(f"Missing updater artifacts: {', '.join(missing)}")
    return assets


def build_manifest(
    asset_dir: Path,
    repository: str,
    tag: str,
    notes: str,
    published_at: str,
    *,
    version: str = "",
    commit: str = "",
) -> dict:
    assets = signed_updater_assets(asset_dir)
    platforms: dict[str, dict[str, str]] = {}

    for kind, (artifact, signature) in sorted(assets.items()):
        entry = {
            "signature": signature,
            "url": download_url(repository, tag, artifact.name),
        }
        for key in platform_keys(kind):
            platforms[key] = entry.copy()

    # Generic targets keep compatibility with older updater clients and unknown
    # bundle metadata. Installer-specific targets above preserve deb/rpm/msi.
    primary = {
        "linux-x86_64": "appimage",
        "windows-x86_64": "nsis",
        "darwin-aarch64": "app",
        "darwin-x86_64": "app",
    }
    for key, kind in primary.items():
        artifact, signature = assets[kind]
        platforms[key] = {
            "signature": signature,
            "url": download_url(repository, tag, artifact.name),
        }

    result = {
        "version": release_version(version or tag),
        "notes": notes.strip(),
        "pub_date": validate_published_at(published_at),
        "platforms": dict(sorted(platforms.items())),
    }
    if commit:
        result["commit"] = validate_commit(commit)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a deterministic Tauri updater manifest from signed release assets."
    )
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument(
        "--version",
        default="",
        help="Manifest SemVer when the release tag itself is not a version (for example nightly).",
    )
    parser.add_argument("--commit", default="", help="Full source Git object ID.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = build_manifest(
        args.asset_dir,
        args.repository,
        args.tag,
        os.environ.get("RELEASE_NOTES", ""),
        args.published_at,
        version=args.version,
        commit=args.commit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
