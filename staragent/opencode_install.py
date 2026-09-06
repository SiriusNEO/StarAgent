from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

OPENCODE_RELEASE_API = "https://api.github.com/repos/anomalyco/opencode/releases/latest"
OPENCODE_RELEASE_MAX_BYTES = 1024 * 1024
OPENCODE_ARCHIVE_MAX_BYTES = 128 * 1024 * 1024
OPENCODE_BINARY_MAX_BYTES = 256 * 1024 * 1024
OPENCODE_VERSION_PATTERN = re.compile(r"v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
RELEASE_DOWNLOAD_PREFIX = "https://github.com/anomalyco/opencode/releases/download/"
RELEASE_DOWNLOAD_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com"})


class OpenCodeInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenCodeReleaseAsset:
    version: str
    name: str
    url: str
    size: int
    sha256: str


def install_opencode_windows(destination: Path | None = None) -> OpenCodeReleaseAsset:
    asset = latest_opencode_windows_asset()
    target = destination or opencode_windows_executable()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix="opencode-install-", dir=target.parent
        ) as temporary:
            staging = Path(temporary)
            archive = staging / asset.name
            binary = staging / "opencode.exe"
            download_verified_archive(asset, archive)
            extract_opencode_binary(archive, binary)
            os.replace(binary, target)
    except OpenCodeInstallError:
        raise
    except OSError as exc:
        raise OpenCodeInstallError(f"Could not install OpenCode: {exc}") from exc
    return asset


def opencode_windows_executable() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home()).expanduser()
    return home / ".opencode" / "bin" / "opencode.exe"


def latest_opencode_windows_asset(
    *,
    machine: str | None = None,
    avx2: bool | None = None,
) -> OpenCodeReleaseAsset:
    payload = download_release_metadata()
    version = str(payload.get("tag_name") or "") if isinstance(payload, dict) else ""
    if not OPENCODE_VERSION_PATTERN.fullmatch(version):
        raise OpenCodeInstallError("OpenCode returned an invalid release version.")
    name = windows_asset_name(machine=machine, avx2=avx2)
    assets = payload.get("assets") if isinstance(payload, dict) else None
    candidates = [
        item
        for item in assets or []
        if isinstance(item, dict) and str(item.get("name") or "") == name
    ]
    if len(candidates) != 1:
        raise OpenCodeInstallError(f"OpenCode release {version} does not provide {name}.")
    item = candidates[0]
    try:
        size = int(item.get("size") or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OpenCodeInstallError("OpenCode returned an invalid release asset size.") from exc
    digest = str(item.get("digest") or "").lower()
    sha256 = digest.removeprefix("sha256:")
    url = str(item.get("browser_download_url") or "")
    expected_url = f"{RELEASE_DOWNLOAD_PREFIX}{version}/{name}"
    if url != expected_url:
        raise OpenCodeInstallError("OpenCode returned an unexpected release asset URL.")
    if not 0 < size <= OPENCODE_ARCHIVE_MAX_BYTES:
        raise OpenCodeInstallError("OpenCode release archive exceeds the safety limit.")
    if not SHA256_PATTERN.fullmatch(sha256):
        raise OpenCodeInstallError("OpenCode release is missing its GitHub SHA-256 digest.")
    return OpenCodeReleaseAsset(version, name, url, size, sha256)


def windows_asset_name(*, machine: str | None = None, avx2: bool | None = None) -> str:
    architecture = (machine or platform.machine()).strip().lower()
    if architecture in {"arm64", "aarch64"}:
        return "opencode-windows-arm64.zip"
    if architecture not in {"amd64", "x86_64"}:
        raise OpenCodeInstallError(
            f"OpenCode has no supported Windows binary for architecture: {architecture}"
        )
    optimized = windows_has_avx2() if avx2 is None else avx2
    suffix = "" if optimized else "-baseline"
    return f"opencode-windows-x64{suffix}.zip"


def windows_has_avx2() -> bool:
    try:
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
    except (AttributeError, OSError):
        return False


def download_release_metadata() -> dict[str, object]:
    request = urllib.request.Request(
        OPENCODE_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "StarAgent harness installer",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            require_https_host(response.geturl(), {"api.github.com"})
            length = response.headers.get("Content-Length")
            if length and int(length) > OPENCODE_RELEASE_MAX_BYTES:
                raise OpenCodeInstallError("OpenCode release metadata is too large.")
            data = response.read(OPENCODE_RELEASE_MAX_BYTES + 1)
    except OpenCodeInstallError:
        raise
    except (OSError, ValueError) as exc:
        raise OpenCodeInstallError(f"Could not download OpenCode release metadata: {exc}") from exc
    if len(data) > OPENCODE_RELEASE_MAX_BYTES:
        raise OpenCodeInstallError("OpenCode release metadata is too large.")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenCodeInstallError("OpenCode release metadata is invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise OpenCodeInstallError("OpenCode release metadata is invalid.")
    return payload


def download_verified_archive(asset: OpenCodeReleaseAsset, destination: Path) -> None:
    request = urllib.request.Request(
        asset.url, headers={"User-Agent": "StarAgent harness installer"}
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            require_https_host(response.geturl(), RELEASE_DOWNLOAD_HOSTS)
            length = response.headers.get("Content-Length")
            if length and int(length) > OPENCODE_ARCHIVE_MAX_BYTES:
                raise OpenCodeInstallError("OpenCode release archive exceeds the safety limit.")
            with destination.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > OPENCODE_ARCHIVE_MAX_BYTES:
                        raise OpenCodeInstallError(
                            "OpenCode release archive exceeds the safety limit."
                        )
                    digest.update(chunk)
                    output.write(chunk)
    except OpenCodeInstallError:
        raise
    except (OSError, ValueError) as exc:
        raise OpenCodeInstallError(f"Could not download OpenCode: {exc}") from exc
    if size != asset.size:
        raise OpenCodeInstallError(
            f"OpenCode archive size mismatch (expected {asset.size}, received {size})."
        )
    if digest.hexdigest() != asset.sha256:
        raise OpenCodeInstallError(
            "OpenCode archive checksum did not match GitHub's SHA-256 digest."
        )


def extract_opencode_binary(archive: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            matches = [entry for entry in entries if entry.filename == "opencode.exe"]
            if len(entries) > 8 or len(matches) != 1:
                raise OpenCodeInstallError("OpenCode archive has an unexpected layout.")
            binary = matches[0]
            mode = binary.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise OpenCodeInstallError(
                    "OpenCode archive contains an unsupported symbolic link."
                )
            if not 0 < binary.file_size <= OPENCODE_BINARY_MAX_BYTES:
                raise OpenCodeInstallError("OpenCode binary exceeds the safety limit.")
            with bundle.open(binary) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            if destination.stat().st_size != binary.file_size:
                raise OpenCodeInstallError("OpenCode binary was not extracted completely.")
            destination.chmod(0o700)
    except OpenCodeInstallError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OpenCodeInstallError(f"Could not extract OpenCode: {exc}") from exc


def require_https_host(url: str, allowed_hosts: set[str] | frozenset[str]) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed_hosts:
        raise OpenCodeInstallError("OpenCode download redirected to an untrusted host.")
