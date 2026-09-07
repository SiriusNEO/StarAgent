from __future__ import annotations

import os
import subprocess
from pathlib import Path

from staragent import desktop_runtime


def test_desktop_login_path_merges_shell_and_gui_environments(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/gui/bin:/usr/bin")
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(
        desktop_runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "/login/bin:/usr/bin", ""),
    )

    paths = desktop_runtime.desktop_login_path().split(os.pathsep)

    assert paths[:3] == ["/login/bin", "/usr/bin", "/gui/bin"]
    assert str(tmp_path / ".local" / "bin") in paths
    assert "/opt/homebrew/bin" in paths
    assert len(paths) == len(set(paths))
