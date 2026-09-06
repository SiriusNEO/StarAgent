from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

from build_updater_manifest import ManifestError, release_version

ROOT = Path(__file__).resolve().parents[2]


def configured_versions(root: Path = ROOT) -> dict[str, str]:
    desktop = root / "desktop"
    package = json.loads((desktop / "package.json").read_text(encoding="utf-8"))
    tauri = json.loads((desktop / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((desktop / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    namespace: dict[str, str] = {}
    exec((root / "staragent" / "__init__.py").read_text(encoding="utf-8"), namespace)
    return {
        "staragent": namespace["__version__"],
        "desktop/package.json": package["version"],
        "desktop/tauri.conf.json": tauri["version"],
        "desktop/Cargo.toml": cargo["package"]["version"],
    }


def check_release_version(tag: str, root: Path = ROOT) -> str:
    expected = release_version(tag)
    mismatches = {
        name: version for name, version in configured_versions(root).items() if version != expected
    }
    if mismatches:
        details = ", ".join(f"{name}={version}" for name, version in mismatches.items())
        raise ManifestError(
            f"Release {tag} does not match the configured desktop version {expected}: {details}"
        )
    return expected


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ensure a GitHub release tag matches every StarAgent desktop version."
    )
    parser.add_argument("tag")
    args = parser.parse_args()
    version = check_release_version(args.tag)
    print(f"Release version is synchronized: {version}")


if __name__ == "__main__":
    main()
