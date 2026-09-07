from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BINARY_ROOT = ROOT / "desktop" / "src-tauri" / "binaries"


class RuntimeBuildError(RuntimeError):
    pass


def host_target_triple() -> str:
    try:
        result = subprocess.run(
            ["rustc", "--print", "host-tuple"],
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeBuildError("Could not determine the Rust host target triple.") from exc
    target = result.stdout.strip()
    if not target:
        raise RuntimeBuildError("rustc returned an empty host target triple.")
    return target


def validate_native_target(target: str) -> None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    expected_system = (
        "windows" if "windows" in target else "darwin" if "apple-darwin" in target else "linux"
    )
    actual_system = "darwin" if system == "darwin" else system
    if expected_system != actual_system:
        raise RuntimeBuildError(
            f"PyInstaller must run on the target operating system ({target} on {system})."
        )
    if target.startswith("universal-"):
        raise RuntimeBuildError(
            "Build separate aarch64 and x86_64 macOS packages; a frozen Python runtime "
            "cannot be cross-compiled as a Tauri universal sidecar."
        )
    target_arch = target.split("-", 1)[0]
    normalized_arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}
    if normalized_arch.get(machine, machine) != normalized_arch.get(target_arch, target_arch):
        raise RuntimeBuildError(
            f"PyInstaller must run natively for {target_arch}; this host is {machine}."
        )


def runtime_destination(target: str) -> Path:
    suffix = ".exe" if "windows" in target else ""
    return BINARY_ROOT / f"staragent-runtime-{target}{suffix}"


def build_runtime(target: str) -> Path:
    validate_native_target(target)
    executable_name = "staragent-runtime.exe" if os.name == "nt" else "staragent-runtime"
    with tempfile.TemporaryDirectory(prefix="staragent-pyinstaller-") as temporary:
        build_root = Path(temporary)
        dist_root = build_root / "dist"
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--onefile",
            "--name",
            "staragent-runtime",
            "--collect-all",
            "staragent",
            "--collect-submodules",
            "uvicorn",
        ]
        if os.name == "nt":
            command.extend(("--noconsole", "--collect-all", "winpty"))
        command.extend(
            (
                "--distpath",
                str(dist_root),
                "--workpath",
                str(build_root / "work"),
                "--specpath",
                str(build_root / "spec"),
                str(ROOT / "staragent" / "desktop_runtime.py"),
            )
        )
        subprocess.run(command, cwd=ROOT, check=True)
        source = dist_root / executable_name
        if not source.is_file():
            raise RuntimeBuildError(f"PyInstaller did not produce {source}.")
        destination = runtime_destination(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if os.name != "nt":
            destination.chmod(destination.stat().st_mode | 0o111)
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the bundled StarAgent Desktop runtime.")
    parser.add_argument("--target-triple", default="", help="Tauri/Rust target triple.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    destination = build_runtime(args.target_triple.strip() or host_target_triple())
    print(f"Bundled runtime: {destination}")


if __name__ == "__main__":
    try:
        main()
    except RuntimeBuildError as exc:
        raise SystemExit(str(exc)) from exc
