from __future__ import annotations

import argparse
from pathlib import Path

ARCHITECTURES = ("aarch64", "x64")


def labeled_name(name: str, arch: str) -> str:
    lower = name.lower()
    if f"_{arch}" in lower:
        return name
    for suffix in (".app.tar.gz", ".dmg"):
        if lower.endswith(suffix):
            return f"{name[: -len(suffix)]}_{arch}{name[-len(suffix) :]}"
    return name


def label_artifacts(root: Path, arch: str) -> list[Path]:
    if arch not in ARCHITECTURES:
        raise ValueError(f"Unsupported macOS architecture label: {arch}")
    renamed: list[Path] = []
    artifacts = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not path.name.endswith(".sig")
        and path.name.lower().endswith((".app.tar.gz", ".dmg"))
    )
    for source in artifacts:
        destination = source.with_name(labeled_name(source.name, arch))
        if destination == source:
            continue
        source.rename(destination)
        signature = Path(f"{source}.sig")
        if signature.is_file():
            signature.rename(Path(f"{destination}.sig"))
        renamed.append(destination)
    return renamed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Give macOS bundles explicit architecture names.")
    parser.add_argument("root", type=Path)
    parser.add_argument("--arch", required=True, choices=ARCHITECTURES)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for path in label_artifacts(args.root, args.arch):
        print(path)


if __name__ == "__main__":
    main()
