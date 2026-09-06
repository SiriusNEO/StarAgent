from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from staragent import opencode_install


class FakeResponse:
    def __init__(self, value: bytes, url: str) -> None:
        self._value = io.BytesIO(value)
        self._url = url
        self.headers = {"Content-Length": str(len(value))}

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return None

    def read(self, size: int = -1) -> bytes:
        return self._value.read(size)

    def geturl(self) -> str:
        return self._url


def test_windows_asset_name_covers_supported_architectures() -> None:
    assert (
        opencode_install.windows_asset_name(machine="AMD64", avx2=True)
        == "opencode-windows-x64.zip"
    )
    assert (
        opencode_install.windows_asset_name(machine="x86_64", avx2=False)
        == "opencode-windows-x64-baseline.zip"
    )
    assert opencode_install.windows_asset_name(machine="aarch64") == "opencode-windows-arm64.zip"
    with pytest.raises(opencode_install.OpenCodeInstallError, match="architecture"):
        opencode_install.windows_asset_name(machine="x86")


def test_latest_release_selects_an_exact_asset_with_github_digest(monkeypatch) -> None:
    name = "opencode-windows-x64-baseline.zip"
    digest = "a" * 64
    url = f"{opencode_install.RELEASE_DOWNLOAD_PREFIX}v1.2.3/{name}"
    monkeypatch.setattr(
        opencode_install,
        "download_release_metadata",
        lambda: {
            "tag_name": "v1.2.3",
            "assets": [
                {
                    "name": name,
                    "size": 1024,
                    "digest": f"sha256:{digest}",
                    "browser_download_url": url,
                }
            ],
        },
    )

    asset = opencode_install.latest_opencode_windows_asset(machine="AMD64", avx2=False)

    assert asset == opencode_install.OpenCodeReleaseAsset(
        version="v1.2.3",
        name=name,
        url=url,
        size=1024,
        sha256=digest,
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("digest", "", "SHA-256"),
        ("browser_download_url", "https://example.com/opencode.zip", "unexpected"),
        ("size", opencode_install.OPENCODE_ARCHIVE_MAX_BYTES + 1, "safety limit"),
    ),
)
def test_latest_release_rejects_untrusted_asset_metadata(
    monkeypatch,
    field,
    value,
    message,
) -> None:
    name = "opencode-windows-x64-baseline.zip"
    item = {
        "name": name,
        "size": 1024,
        "digest": f"sha256:{'a' * 64}",
        "browser_download_url": (f"{opencode_install.RELEASE_DOWNLOAD_PREFIX}v1.2.3/{name}"),
    }
    item[field] = value
    monkeypatch.setattr(
        opencode_install,
        "download_release_metadata",
        lambda: {"tag_name": "v1.2.3", "assets": [item]},
    )

    with pytest.raises(opencode_install.OpenCodeInstallError, match=message):
        opencode_install.latest_opencode_windows_asset(machine="AMD64", avx2=False)


def test_download_archive_checks_size_digest_and_redirect_host(monkeypatch, tmp_path) -> None:
    content = b"verified OpenCode archive"
    asset = opencode_install.OpenCodeReleaseAsset(
        version="v1.2.3",
        name="opencode-windows-x64-baseline.zip",
        url=(f"{opencode_install.RELEASE_DOWNLOAD_PREFIX}v1.2.3/opencode-windows-x64-baseline.zip"),
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    monkeypatch.setattr(
        opencode_install.urllib.request,
        "urlopen",
        lambda request, timeout: FakeResponse(
            content,
            "https://release-assets.githubusercontent.com/github-production-release-asset/file",
        ),
    )
    destination = tmp_path / asset.name

    opencode_install.download_verified_archive(asset, destination)

    assert destination.read_bytes() == content
    malicious = FakeResponse(content, "https://downloads.example.com/opencode.zip")
    monkeypatch.setattr(
        opencode_install.urllib.request,
        "urlopen",
        lambda request, timeout: malicious,
    )
    with pytest.raises(opencode_install.OpenCodeInstallError, match="untrusted host"):
        opencode_install.download_verified_archive(asset, destination)


def test_extract_archive_writes_only_the_expected_binary(tmp_path) -> None:
    archive = tmp_path / "opencode.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("opencode.exe", b"native executable")
    destination = tmp_path / "opencode.exe"

    opencode_install.extract_opencode_binary(archive, destination)

    assert destination.read_bytes() == b"native executable"


def test_extract_archive_rejects_an_unexpected_layout(tmp_path) -> None:
    archive = tmp_path / "opencode.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("nested/opencode.exe", b"unexpected")

    with pytest.raises(opencode_install.OpenCodeInstallError, match="unexpected layout"):
        opencode_install.extract_opencode_binary(archive, tmp_path / "opencode.exe")
