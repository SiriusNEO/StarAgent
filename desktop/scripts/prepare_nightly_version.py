from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
PYTHON_VERSION = re.compile(r'(?m)^__version__\s*=\s*"([^"]+)"[ \t]*$')


class NightlyVersionError(RuntimeError):
    pass


def _single_match(pattern: re.Pattern[str], value: str, label: str) -> str:
    matches = pattern.findall(value)
    if len(matches) != 1:
        raise NightlyVersionError(f"Expected exactly one version in {label}; found {len(matches)}.")
    return matches[0]


def _cargo_lock_version(path: Path) -> str:
    packages = tomllib.loads(path.read_text(encoding="utf-8")).get("package", [])
    versions = [item.get("version") for item in packages if item.get("name") == "staragent-desktop"]
    if len(versions) != 1 or not isinstance(versions[0], str):
        raise NightlyVersionError(
            f"Expected exactly one staragent-desktop package in {path.relative_to(path.parents[2])}."
        )
    return versions[0]


def configured_versions(root: Path = ROOT) -> dict[str, str]:
    desktop = root / "desktop"
    package = json.loads((desktop / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((desktop / "package-lock.json").read_text(encoding="utf-8"))
    tauri = json.loads((desktop / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((desktop / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    init_path = root / "staragent" / "__init__.py"
    return {
        "staragent/__init__.py": _single_match(
            PYTHON_VERSION,
            init_path.read_text(encoding="utf-8"),
            "staragent/__init__.py",
        ),
        "desktop/package.json": package["version"],
        "desktop/package-lock.json": package_lock["version"],
        "desktop/package-lock.json packages['']": package_lock["packages"][""]["version"],
        "desktop/src-tauri/tauri.conf.json": tauri["version"],
        "desktop/src-tauri/Cargo.toml": cargo["package"]["version"],
        "desktop/src-tauri/Cargo.lock": _cargo_lock_version(desktop / "src-tauri" / "Cargo.lock"),
    }


def nightly_version(source_version: str, run_number: int) -> str:
    match = SEMVER.fullmatch(source_version.strip())
    if not match:
        raise NightlyVersionError(f"Configured version is not valid SemVer: {source_version!r}")
    if run_number < 1:
        raise NightlyVersionError("GitHub Actions run number must be positive.")
    core = ".".join(match.group(part) for part in ("major", "minor", "patch"))
    # `-dev.N` is accepted by SemVer consumers (npm, Cargo, and Tauri) and
    # normalizes to the equivalent PEP 440 development release in Python.
    return f"{core}-dev.{run_number}"


def _replace_version_line(lines: list[str], start: int, end: int, version: str, label: str) -> None:
    indexes = [
        index
        for index in range(start, end)
        if re.match(r'^version\s*=\s*"[^"]+"\s*$', lines[index].rstrip("\r\n"))
    ]
    if len(indexes) != 1:
        raise NightlyVersionError(f"Expected exactly one package version in {label}.")
    index = indexes[0]
    lines[index] = re.sub(
        r'(?<=")([^"]+)(?="\s*$)',
        version,
        lines[index].rstrip("\r\n"),
    ) + ("\n" if lines[index].endswith("\n") else "")


def _write_cargo_toml(path: Path, version: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.strip() == "[package]"]
    if len(starts) != 1:
        raise NightlyVersionError("Expected exactly one [package] section in Cargo.toml.")
    start = starts[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].lstrip().startswith("[")),
        len(lines),
    )
    _replace_version_line(lines, start, end, version, "Cargo.toml")
    path.write_text("".join(lines), encoding="utf-8")


def _write_cargo_lock(path: Path, version: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.strip() == "[[package]]"]
    matching: list[tuple[int, int]] = []
    for offset, section_start in enumerate(starts):
        section_end = starts[offset + 1] if offset + 1 < len(starts) else len(lines)
        if any(
            line.strip() == 'name = "staragent-desktop"'
            for line in lines[section_start:section_end]
        ):
            matching.append((section_start + 1, section_end))
    if len(matching) != 1:
        raise NightlyVersionError("Expected exactly one staragent-desktop package in Cargo.lock.")
    _replace_version_line(lines, *matching[0], version, "Cargo.lock")
    path.write_text("".join(lines), encoding="utf-8")


def _write_json_version(path: Path, version: str, *, lockfile: bool = False) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    value["version"] = version
    if lockfile:
        value["packages"][""]["version"] = version
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_versions(root: Path, version: str) -> None:
    desktop = root / "desktop"
    init_path = root / "staragent" / "__init__.py"
    init_value = init_path.read_text(encoding="utf-8")
    init_value, replacements = PYTHON_VERSION.subn(f'__version__ = "{version}"', init_value)
    if replacements != 1:
        raise NightlyVersionError("Expected exactly one version in staragent/__init__.py.")
    init_path.write_text(init_value, encoding="utf-8")
    _write_json_version(desktop / "package.json", version)
    _write_json_version(desktop / "package-lock.json", version, lockfile=True)
    _write_json_version(desktop / "src-tauri" / "tauri.conf.json", version)
    _write_cargo_toml(desktop / "src-tauri" / "Cargo.toml", version)
    _write_cargo_lock(desktop / "src-tauri" / "Cargo.lock", version)


def prepare_nightly(root: Path, run_number: int, *, write: bool = False) -> str:
    versions = configured_versions(root)
    distinct = set(versions.values())
    if len(distinct) != 1:
        details = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise NightlyVersionError(f"Configured versions are not synchronized: {details}")
    version = nightly_version(next(iter(distinct)), run_number)
    if write:
        write_versions(root, version)
        remaining = {
            name: value for name, value in configured_versions(root).items() if value != version
        }
        if remaining:
            raise NightlyVersionError(f"Could not synchronize Nightly versions: {remaining}")
    return version


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Derive and optionally apply a unique SemVer for a Nightly desktop build."
    )
    parser.add_argument("--run-number", type=int, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    print(prepare_nightly(args.root.resolve(), args.run_number, write=args.write))


if __name__ == "__main__":
    main()
