from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from staragent.event_log import redact_log_text
from staragent.text import strip_ansi
from staragent.windows import (
    augmented_windows_path,
    background_process_kwargs,
    windows_process_argv,
)

DEPENDENCY_CACHE_TTL_SECONDS = 60.0
DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 300.0
DEPENDENCY_INSTALL_SCRIPT_MAX_BYTES = 1024 * 1024
DEPENDENCY_OUTPUT_MAX_CHARS = 4_000
DEPENDENCY_PLATFORMS = {"linux", "macos", "windows"}
DEPENDENCY_STATUSES = {"available", "missing", "error", "unknown"}


@dataclass(frozen=True)
class Dependency:
    name: str
    label: str
    commands: tuple[str, ...]
    required: bool
    note: str
    docs_url: str
    mirror_url: str = ""
    builtin: bool = False


@dataclass(frozen=True)
class DependencyInstallOption:
    id: str
    method: str
    source: str
    provider: str
    command: str
    argv: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    recommended: bool = False
    source_url: str = ""
    elevated: bool = False
    script_url: str = ""
    interpreter: str = ""


class DependencyInstallBusyError(RuntimeError):
    pass


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
_INSTALL_LOCKS: dict[str, threading.Lock] = {
    name: threading.Lock() for name in ("conpty", "tmux", "tailscale", "nodejs")
}


def current_dependency_platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def normalize_dependency_platform(value: object, *, fallback: str | None = None) -> str:
    platform_name = str(value or "").strip().lower()
    if platform_name in DEPENDENCY_PLATFORMS:
        return platform_name
    return fallback or current_dependency_platform()


def dependency_specs(platform_name: str | None = None) -> tuple[Dependency, ...]:
    platform_name = normalize_dependency_platform(platform_name)
    terminal = (
        Dependency(
            name="conpty",
            label="Windows ConPTY",
            commands=(),
            required=True,
            note="Bundled native terminal backend; WSL and tmux are not required.",
            docs_url="https://learn.microsoft.com/windows/console/creating-a-pseudoconsole-session",
            builtin=True,
        )
        if platform_name == "windows"
        else Dependency(
            name="tmux",
            label="tmux",
            commands=("tmux",),
            required=True,
            note="Persistent terminal backend used by StarAgent Sessions.",
            docs_url="https://github.com/tmux/tmux/wiki/Installing",
        )
    )
    return (
        terminal,
        Dependency(
            name="tailscale",
            label="Tailscale",
            commands=("tailscale",),
            required=False,
            note="Optional private networking for Nodes outside the local network.",
            docs_url=f"https://tailscale.com/docs/install/{'mac' if platform_name == 'macos' else platform_name}",
        ),
        Dependency(
            name="nodejs",
            label="Node.js + npm",
            commands=("node", "npm"),
            required=False,
            note="Optional runtime for npm-based Harness installation and updates.",
            docs_url="https://nodejs.org/en/download",
            mirror_url="https://npmmirror.com/mirrors/node",
        ),
    )


def dependency_spec(name: str, platform_name: str | None = None) -> Dependency | None:
    normalized = str(name or "").strip().lower()
    return next(
        (item for item in dependency_specs(platform_name) if item.name == normalized),
        None,
    )


def known_dependency_names() -> set[str]:
    return {
        item.name
        for platform_name in DEPENDENCY_PLATFORMS
        for item in dependency_specs(platform_name)
    }


def dependencies_status(*, force: bool = False) -> dict[str, object]:
    platform_name = current_dependency_platform()
    environment = dependency_environment()
    cache_key = "\0".join((platform_name, environment.get("PATH", "")))
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and not force and now - cached[0] < DEPENDENCY_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])

    payload: dict[str, object] = {
        "supported": True,
        "installs_supported": True,
        "platform": platform_name,
        "checked_at": utc_timestamp(),
        "cache_ttl_seconds": int(DEPENDENCY_CACHE_TTL_SECONDS),
        "dependencies": [
            dependency_status(item, platform_name=platform_name, environment=environment)
            for item in dependency_specs(platform_name)
        ],
        "error": "",
    }
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic(), payload)
    return copy.deepcopy(payload)


def clear_dependencies_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def dependency_status(
    dependency: Dependency,
    *,
    platform_name: str | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    platform_name = normalize_dependency_platform(platform_name)
    environment = environment or dependency_environment()
    if dependency.builtin:
        from staragent.native_sessions import native_session_backend_available

        installed = native_session_backend_available()
        return dependency_status_payload(
            dependency,
            platform_name=platform_name,
            status="available" if installed else "error",
            installed=installed,
            version="Bundled" if installed else "",
            error="" if installed else "Bundled ConPTY component is unavailable.",
        )

    executables = {
        command: find_dependency_executable(command, environment) for command in dependency.commands
    }
    installed = all(executables.values())
    version = dependency_version(dependency, executables, environment) if installed else ""
    return dependency_status_payload(
        dependency,
        platform_name=platform_name,
        status="available" if installed else "missing",
        installed=installed,
        version=version,
        executable=str(executables.get(dependency.commands[0]) or ""),
    )


def dependency_status_payload(
    dependency: Dependency,
    *,
    platform_name: str,
    status: str,
    installed: bool,
    version: str = "",
    executable: str = "",
    error: str = "",
    install_options: object = None,
) -> dict[str, object]:
    options = install_options_payload(
        dependency,
        install_options,
        platform_name=platform_name,
    )
    resources = dependency_resources(dependency)
    return {
        "name": dependency.name,
        "label": dependency.label,
        "required": dependency.required,
        "status": normalize_dependency_status(status),
        "installed": bool(installed),
        "version": clean_dependency_text(version, max_chars=160),
        "executable": clean_dependency_text(executable, max_chars=300),
        "install_options": options,
        "resources": resources,
        "note": dependency.note,
        "error": clean_dependency_output(error),
    }


def dependency_resources(dependency: Dependency) -> list[dict[str, object]]:
    resources: list[dict[str, object]] = [
        {
            "id": "official",
            "label": "Official",
            "url": dependency.docs_url,
            "source": "official",
            "china": False,
        }
    ]
    if dependency.mirror_url:
        resources.append(
            {
                "id": "npmmirror",
                "label": "npmmirror",
                "url": dependency.mirror_url,
                "source": "mirror",
                "china": True,
            }
        )
    return resources


def dependency_install_options(
    dependency: Dependency,
    *,
    platform_name: str | None = None,
) -> tuple[DependencyInstallOption, ...]:
    platform_name = normalize_dependency_platform(platform_name)
    if dependency.builtin:
        return ()
    if dependency.name == "tmux":
        return package_manager_options("tmux", platform_name)
    if dependency.name == "nodejs":
        return package_manager_options("nodejs", platform_name)
    if dependency.name == "tailscale":
        if platform_name == "windows":
            return (
                winget_option(
                    "winget",
                    "Tailscale.Tailscale",
                    "Tailscale",
                    "https://tailscale.com/download/windows",
                ),
            )
        if platform_name == "macos":
            return (
                package_option(
                    id="homebrew",
                    provider="Homebrew",
                    argv=("brew", "install", "tailscale"),
                    command="brew install tailscale",
                    source_url="https://formulae.brew.sh/formula/tailscale",
                    recommended=True,
                ),
            )
        return (
            DependencyInstallOption(
                id="official-script",
                method="install_script",
                source="official",
                provider="Tailscale",
                command="curl -fsSL https://tailscale.com/install.sh | sh",
                requirements=("sh",),
                recommended=True,
                source_url="https://tailscale.com/docs/install/linux",
                elevated=True,
                script_url="https://tailscale.com/install.sh",
                interpreter="sh",
            ),
            package_option(
                id="homebrew",
                provider="Homebrew",
                argv=("brew", "install", "tailscale"),
                command="brew install tailscale",
                source_url="https://formulae.brew.sh/formula/tailscale",
            ),
            package_option(
                id="pacman",
                provider="pacman",
                argv=("pacman", "-S", "--noconfirm", "tailscale"),
                command="pacman -S --noconfirm tailscale",
                elevated=True,
            ),
            package_option(
                id="apk",
                provider="APK",
                argv=("apk", "add", "tailscale"),
                command="apk add tailscale",
                elevated=True,
            ),
        )
    return ()


def package_manager_options(
    dependency_name: str,
    platform_name: str,
) -> tuple[DependencyInstallOption, ...]:
    package_names = {
        "tmux": {
            "apt": ("tmux",),
            "dnf": ("tmux",),
            "yum": ("tmux",),
            "apk": ("tmux",),
            "pacman": ("tmux",),
            "zypper": ("tmux",),
            "homebrew": ("tmux",),
            "macports": ("tmux",),
        },
        "nodejs": {
            "apt": ("nodejs", "npm"),
            "dnf": ("nodejs", "npm"),
            "yum": ("nodejs", "npm"),
            "apk": ("nodejs", "npm"),
            "pacman": ("nodejs", "npm"),
            "zypper": ("nodejs", "npm"),
            "homebrew": ("node",),
        },
    }[dependency_name]
    if platform_name == "windows":
        if dependency_name != "nodejs":
            return ()
        return (
            winget_option(
                "winget",
                "OpenJS.NodeJS.LTS",
                "Windows Package Manager",
                "https://github.com/microsoft/winget-pkgs/tree/master/manifests/o/OpenJS/NodeJS/LTS",
            ),
        )

    options: list[DependencyInstallOption] = []
    if platform_name == "macos":
        managers = ("homebrew", "macports") if dependency_name == "tmux" else ("homebrew",)
    else:
        managers = ("apt", "dnf", "yum", "apk", "pacman", "zypper", "homebrew")
    for manager in managers:
        packages = package_names.get(manager)
        if not packages:
            continue
        options.append(system_package_option(manager, packages, recommended=not options))
    return tuple(options)


def system_package_option(
    manager: str,
    packages: tuple[str, ...],
    *,
    recommended: bool,
) -> DependencyInstallOption:
    definitions = {
        "apt": ("APT", "apt-get", ("install", "-y"), "https://packages.debian.org/"),
        "dnf": ("DNF", "dnf", ("install", "-y"), "https://packages.fedoraproject.org/"),
        "yum": ("YUM", "yum", ("install", "-y"), "https://rpmfind.net/"),
        "apk": ("APK", "apk", ("add",), "https://pkgs.alpinelinux.org/"),
        "pacman": ("pacman", "pacman", ("-S", "--noconfirm"), "https://archlinux.org/packages/"),
        "zypper": (
            "Zypper",
            "zypper",
            ("--non-interactive", "install"),
            "https://software.opensuse.org/",
        ),
        "homebrew": ("Homebrew", "brew", ("install",), "https://formulae.brew.sh/"),
        "macports": ("MacPorts", "port", ("install",), "https://ports.macports.org/"),
    }
    provider, executable, arguments, source_url = definitions[manager]
    argv = (executable, *arguments, *packages)
    elevated = manager not in {"homebrew"}
    command = " ".join(argv)
    return package_option(
        id=manager,
        provider=provider,
        argv=argv,
        command=command,
        source_url=source_url,
        elevated=elevated,
        recommended=recommended,
    )


def package_option(
    *,
    id: str,
    provider: str,
    argv: tuple[str, ...],
    command: str,
    source_url: str = "",
    elevated: bool = False,
    recommended: bool = False,
) -> DependencyInstallOption:
    return DependencyInstallOption(
        id=id,
        method="package_manager",
        source="system",
        provider=provider,
        command=command,
        argv=argv,
        requirements=(argv[0],),
        recommended=recommended,
        source_url=source_url,
        elevated=elevated,
    )


def winget_option(
    id: str,
    package_id: str,
    provider: str,
    source_url: str,
) -> DependencyInstallOption:
    argv = (
        "winget",
        "install",
        "--id",
        package_id,
        "--exact",
        "--source",
        "winget",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--silent",
    )
    return DependencyInstallOption(
        id=id,
        method="package_manager",
        source="official",
        provider=provider,
        command=" ".join(argv),
        argv=argv,
        requirements=("winget",),
        recommended=True,
        source_url=source_url,
        elevated=True,
    )


def install_options_payload(
    dependency: Dependency,
    reported: object = None,
    *,
    platform_name: str | None = None,
) -> list[dict[str, object]]:
    platform_name = normalize_dependency_platform(platform_name)
    options = dependency_install_options(dependency, platform_name=platform_name)
    if isinstance(reported, list):
        by_id = {str(item.get("id") or ""): item for item in reported if isinstance(item, dict)}
        return [
            install_option_payload(
                option,
                by_id[option.id],
                platform_name=platform_name,
            )
            for option in options
            if option.id in by_id
        ]
    payloads = [install_option_payload(option, platform_name=platform_name) for option in options]
    available = [item for item in payloads if item["available"]]
    if available and not any(item["recommended"] for item in available):
        available[0] = {**available[0], "recommended": True}
    return available


def install_option_payload(
    option: DependencyInstallOption,
    reported: object = None,
    *,
    platform_name: str | None = None,
) -> dict[str, object]:
    platform_name = normalize_dependency_platform(platform_name)
    allowed_requirements = set(option.requirements)
    if option.elevated and platform_name != "windows" and not is_root():
        allowed_requirements.add("sudo")
    if isinstance(reported, dict):
        raw_missing = reported.get("missing_requirements")
        missing = (
            sorted(str(item) for item in raw_missing if str(item) in allowed_requirements)
            if isinstance(raw_missing, list)
            else sorted(allowed_requirements)
        )
        available = bool(reported.get("available")) and not missing
        reported_command = clean_dependency_text(reported.get("command"), max_chars=400)
        command = (
            reported_command
            if reported_command in allowed_dependency_commands(option, platform_name=platform_name)
            else manual_dependency_command(option, platform_name=platform_name)
        )
    else:
        missing = sorted(
            command
            for command in allowed_requirements
            if not find_dependency_executable(command, dependency_environment())
        )
        available = not missing
        command = manual_dependency_command(option, platform_name=platform_name)
    return {
        "id": option.id,
        "method": option.method,
        "source": option.source,
        "provider": option.provider,
        "command": command,
        "recommended": option.recommended,
        "source_url": option.source_url,
        "available": available,
        "missing_requirements": missing,
        "elevated": option.elevated,
    }


def manual_dependency_command(
    option: DependencyInstallOption,
    *,
    platform_name: str | None = None,
) -> str:
    platform_name = normalize_dependency_platform(platform_name)
    if option.elevated and option.argv and platform_name != "windows" and not is_root():
        return f"sudo {option.command}"
    return option.command


def allowed_dependency_commands(
    option: DependencyInstallOption,
    *,
    platform_name: str | None = None,
) -> set[str]:
    platform_name = normalize_dependency_platform(platform_name)
    commands = {option.command}
    if option.elevated and option.argv and platform_name != "windows":
        commands.add(f"sudo {option.command}")
    return commands


def dependency_install_option(
    dependency: Dependency,
    option_id: str,
    *,
    platform_name: str | None = None,
) -> DependencyInstallOption:
    normalized = str(option_id or "").strip().lower()
    option = next(
        (
            item
            for item in dependency_install_options(dependency, platform_name=platform_name)
            if item.id == normalized
        ),
        None,
    )
    if option is None:
        raise ValueError(f"Unsupported install option for {dependency.label}: {option_id}")
    return option


def known_dependency_install_option(name: str, option_id: str) -> bool:
    for platform_name in DEPENDENCY_PLATFORMS:
        dependency = dependency_spec(name, platform_name)
        if dependency and any(
            option.id == option_id
            for option in dependency_install_options(
                dependency,
                platform_name=platform_name,
            )
        ):
            return True
    return False


def install_dependency(name: str, option_id: str) -> dict[str, object]:
    platform_name = current_dependency_platform()
    dependency = dependency_spec(name, platform_name)
    if dependency is None:
        raise ValueError(f"Unsupported dependency: {name}")
    option = dependency_install_option(dependency, option_id, platform_name=platform_name)
    lock = _INSTALL_LOCKS[dependency.name]
    if not lock.acquire(blocking=False):
        raise DependencyInstallBusyError(f"{dependency.label} is already being installed.")
    try:
        before = dependency_status(dependency, platform_name=platform_name)
        result: dict[str, object] = {
            "ok": False,
            "dependency": dependency.name,
            "label": dependency.label,
            "platform": platform_name,
            "option": option.id,
            "source": option.source,
            "provider": option.provider,
            "command": manual_dependency_command(option, platform_name=platform_name),
            "before_status": before["status"],
            "after_status": before["status"],
            "after_version": before["version"],
            "changed": False,
            "output": "",
            "error": "",
            "checked_at": utc_timestamp(),
        }
        if before["installed"]:
            result["ok"] = True
            return normalize_dependency_install_result(dependency.name, result)

        option_state = install_option_payload(option)
        if not option_state["available"]:
            missing = ", ".join(option_state["missing_requirements"])
            result["error"] = f"Required command not found: {missing}"
            return normalize_dependency_install_result(dependency.name, result)

        returncode, output = run_dependency_install_option(option)
        result["output"] = output
        if returncode != 0:
            result["error"] = output or f"Install command exited with code {returncode}."
            return normalize_dependency_install_result(dependency.name, result)

        clear_dependencies_cache()
        after = dependency_status(dependency, platform_name=platform_name)
        installed = bool(after["installed"])
        result.update(
            {
                "ok": installed,
                "after_status": after["status"],
                "after_version": after["version"],
                "changed": installed,
                "error": ""
                if installed
                else (
                    "The installer completed, but the dependency is still missing from the "
                    "Node service PATH. Restart StarAgent or run the copied command in Terminal."
                ),
                "checked_at": utc_timestamp(),
            }
        )
        return normalize_dependency_install_result(dependency.name, result)
    finally:
        lock.release()


def run_dependency_install_option(option: DependencyInstallOption) -> tuple[int, str]:
    if option.script_url:
        try:
            script = download_dependency_install_script(option.script_url)
        except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
            return 1, clean_dependency_output(exc)
        try:
            with tempfile.TemporaryDirectory(prefix="staragent-dependency-") as directory:
                path = Path(directory) / "install.sh"
                path.write_bytes(script)
                path.chmod(0o700)
                return run_dependency_command(
                    (option.interpreter, str(path)),
                    elevated=option.elevated,
                )
        except OSError as exc:
            return 1, clean_dependency_output(exc)
    if option.argv:
        return run_dependency_command(option.argv, elevated=option.elevated)
    return 2, "Install option is not executable."


def run_dependency_command(
    argv: tuple[str, ...],
    *,
    elevated: bool,
) -> tuple[int, str]:
    environment = dependency_environment()
    command = list(argv)
    if elevated and os.name != "nt" and not is_root():
        sudo = find_dependency_executable("sudo", environment)
        if not sudo:
            return 127, "sudo is required; run the copied command in an interactive Terminal."
        command = [sudo, "-n", *command]
    try:
        if os.name == "nt":
            command = windows_process_argv(command, environment, require_executable=True)
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
            env=environment,
            **background_process_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        output = clean_dependency_output(exc.stdout, exc.stderr)
        return 124, f"{output}\nInstall timed out.".strip()
    except OSError as exc:
        return 127, clean_dependency_output(exc)
    output = clean_dependency_output(result.stdout, result.stderr)
    if result.returncode != 0 and elevated and "sudo" in command[0].lower():
        output = (
            f"{output}\nAdministrator approval is required; run the copied command in an "
            "interactive Terminal."
        ).strip()
    return result.returncode, output


def download_dependency_install_script(url: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "tailscale.com"
        or parsed.path != "/install.sh"
    ):
        raise ValueError("Installer URL is not allowlisted.")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "StarAgent dependency installer"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        final = urllib.parse.urlparse(response.geturl())
        if final.scheme != "https" or final.hostname not in {"tailscale.com", "pkgs.tailscale.com"}:
            raise ValueError("Installer endpoint redirected to an untrusted host.")
        content_length = safe_int(response.headers.get("Content-Length"))
        if content_length > DEPENDENCY_INSTALL_SCRIPT_MAX_BYTES:
            raise RuntimeError("Installer script is too large.")
        script = response.read(DEPENDENCY_INSTALL_SCRIPT_MAX_BYTES + 1)
    if len(script) > DEPENDENCY_INSTALL_SCRIPT_MAX_BYTES:
        raise RuntimeError("Installer script is too large.")
    stripped = script.lstrip()
    if not stripped or stripped[:16].lower().startswith((b"<!doctype", b"<html")):
        raise RuntimeError("Installer endpoint did not return a shell script.")
    return script


def dependency_environment() -> dict[str, str]:
    environment = os.environ.copy()
    if os.name == "nt":
        environment["PATH"] = augmented_windows_path(environment)
    return environment


def find_dependency_executable(command: str, environment: dict[str, str]) -> str | None:
    return shutil.which(command, path=environment.get("PATH"))


def dependency_version(
    dependency: Dependency,
    executables: dict[str, str | None],
    environment: dict[str, str],
) -> str:
    versions = []
    for command in dependency.commands:
        executable = executables.get(command)
        if not executable:
            return ""
        args = [executable, "-V" if command == "tmux" else "--version"]
        if command == "tailscale":
            args = [executable, "version"]
        try:
            if os.name == "nt":
                args = windows_process_argv(args, environment, require_executable=True)
            result = subprocess.run(
                args,
                check=False,
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=5,
                env=environment,
                **background_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        value = (result.stdout or result.stderr).strip().splitlines()
        if not value:
            return ""
        prefix = f"{command} " if len(dependency.commands) > 1 else ""
        versions.append(f"{prefix}{value[0]}")
    return " · ".join(versions)


def normalize_dependencies_payload(value: object) -> dict[str, object]:
    payload = value if isinstance(value, dict) else {}
    platform_name = normalize_dependency_platform(payload.get("platform"))
    raw_items = payload.get("dependencies")
    by_name = (
        {str(item.get("name") or ""): item for item in raw_items if isinstance(item, dict)}
        if isinstance(raw_items, list)
        else {}
    )
    dependencies = []
    for dependency in dependency_specs(platform_name):
        raw = by_name.get(dependency.name)
        if raw is None:
            dependencies.append(
                dependency_status_payload(
                    dependency,
                    platform_name=platform_name,
                    status="unknown",
                    installed=False,
                    error="Node did not report this dependency.",
                    install_options=[],
                )
            )
            continue
        status = normalize_dependency_status(raw.get("status"))
        installed = bool(raw.get("installed")) and status == "available"
        dependencies.append(
            dependency_status_payload(
                dependency,
                platform_name=platform_name,
                status=status,
                installed=installed,
                version=raw.get("version", ""),
                executable=raw.get("executable", ""),
                error=raw.get("error", ""),
                install_options=raw.get("install_options", []),
            )
        )
    return {
        "supported": bool(payload.get("supported")),
        "installs_supported": bool(payload.get("installs_supported")),
        "platform": platform_name,
        "checked_at": clean_dependency_text(payload.get("checked_at"), max_chars=80),
        "cache_ttl_seconds": max(0, min(safe_int(payload.get("cache_ttl_seconds")), 3600)),
        "dependencies": dependencies,
        "error": clean_dependency_output(payload.get("error")),
    }


def unknown_dependencies_payload(message: str, *, stale: bool = False) -> dict[str, object]:
    platform_name = current_dependency_platform()
    return {
        "supported": False,
        "installs_supported": False,
        "platform": platform_name,
        "checked_at": utc_timestamp(),
        "cache_ttl_seconds": 0,
        "dependencies": [
            dependency_status_payload(
                dependency,
                platform_name=platform_name,
                status="unknown",
                installed=False,
                error=message,
                install_options=[],
            )
            for dependency in dependency_specs(platform_name)
        ],
        "error": clean_dependency_output(message),
        "stale": bool(stale),
    }


def normalize_dependency_install_result(name: str, value: object) -> dict[str, object]:
    payload = value if isinstance(value, dict) else {}
    fallback_platform = "windows" if name == "conpty" else ("linux" if name == "tmux" else None)
    platform_name = normalize_dependency_platform(
        payload.get("platform"),
        fallback=fallback_platform,
    )
    dependency = dependency_spec(name, platform_name)
    if dependency is None:
        raise ValueError(f"Unsupported dependency: {name}")
    options = {
        item.id: item
        for item in dependency_install_options(dependency, platform_name=platform_name)
    }
    option = options.get(str(payload.get("option") or ""))
    command = clean_dependency_text(payload.get("command"), max_chars=400)
    valid_option = option is not None and command in allowed_dependency_commands(
        option,
        platform_name=platform_name,
    )
    error = clean_dependency_output(payload.get("error"))
    if payload.get("ok") and not valid_option and not error:
        error = "Node returned an invalid dependency installation result."
    return {
        "ok": bool(payload.get("ok")) and valid_option,
        "dependency": dependency.name,
        "label": dependency.label,
        "platform": platform_name,
        "option": option.id if option else "",
        "source": option.source if option else "",
        "provider": option.provider if option else "",
        "command": command if valid_option else "",
        "before_status": normalize_dependency_status(payload.get("before_status")),
        "after_status": normalize_dependency_status(payload.get("after_status")),
        "after_version": clean_dependency_text(payload.get("after_version"), max_chars=160),
        "changed": bool(payload.get("changed")),
        "output": clean_dependency_output(payload.get("output")),
        "error": error,
        "checked_at": clean_dependency_text(payload.get("checked_at"), max_chars=80),
    }


def ensure_dependencies() -> dict[str, object]:
    """Best-effort bootstrap for required CLI startup dependencies."""
    rows = []
    platform_name = current_dependency_platform()
    for dependency in dependency_specs(platform_name):
        before = dependency_status(dependency, platform_name=platform_name)
        if not dependency.required or before["installed"]:
            rows.append({**before, "changed": False, "ok": True, "log": ""})
            continue
        options = dependency_install_options(dependency, platform_name=platform_name)
        option = next((item for item in options if install_option_payload(item)["available"]), None)
        if option is None:
            rows.append(
                {
                    **before,
                    "changed": False,
                    "ok": False,
                    "error": before.get("error") or "No automatic installer is available.",
                    "log": "",
                }
            )
            continue
        result = install_dependency(dependency.name, option.id)
        after = dependency_status(dependency, platform_name=platform_name)
        rows.append(
            {
                **after,
                "changed": bool(result.get("changed")),
                "ok": bool(result.get("ok")),
                "error": result.get("error") or after.get("error") or "",
                "log": result.get("output") or "",
            }
        )
    return {"dependencies": rows}


def normalize_dependency_status(value: object) -> str:
    status = str(value or "unknown").strip().lower()
    return status if status in DEPENDENCY_STATUSES else "unknown"


def clean_dependency_text(value: object, *, max_chars: int) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return f"{text[:max_chars]}…" if len(text) > max_chars else text


def clean_dependency_output(*values: object) -> str:
    parts = []
    for value in values:
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        else:
            text = str(value or "")
        text = strip_ansi(text).strip()
        if text:
            parts.append(text)
    return redact_log_text("\n".join(parts), max_chars=DEPENDENCY_OUTPUT_MAX_CHARS)


def is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
