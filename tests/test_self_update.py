from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from staragent import self_update
from staragent.node import app as node_app


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def create_checkout(tmp_path: Path, *, branch: str = "dev", remote: str | None = None) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", branch)
    git(repo, "config", "user.name", "StarAgent Tests")
    git(repo, "config", "user.email", "tests@staragent.local")
    (repo / "README.md").write_text("first\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "First")
    git(repo, "remote", "add", "origin", remote or self_update.OFFICIAL_REPOSITORY_URL)
    git(repo, "update-ref", f"refs/remotes/origin/{branch}", git(repo, "rev-parse", "HEAD"))
    return repo


def make_remote_commit(repo: Path) -> tuple[str, str]:
    before = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("second\n", encoding="utf-8")
    git(repo, "commit", "-am", "Second")
    latest = git(repo, "rev-parse", "HEAD")
    branch = git(repo, "branch", "--show-current")
    git(repo, "update-ref", f"refs/remotes/origin/{branch}", latest)
    git(repo, "reset", "--hard", before)
    return before, latest


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/SiriusNEO/StarAgent.git",
        "https://github.com/siriusneo/staragent/",
        "git@github.com:SiriusNEO/StarAgent.git",
        "ssh://git@github.com/SiriusNEO/StarAgent.git",
    ],
)
def test_official_remote_recognizes_supported_github_urls(remote: str) -> None:
    assert self_update.is_official_remote(remote)


@pytest.mark.parametrize(
    "remote",
    [
        "http://github.com/SiriusNEO/StarAgent.git",
        "https://github.com/another/StarAgent.git",
        "https://example.com/SiriusNEO/StarAgent.git",
        "/srv/staragent.git",
    ],
)
def test_official_remote_rejects_untrusted_urls(remote: str) -> None:
    assert not self_update.is_official_remote(remote)


def test_update_status_reports_current_preview_checkout(tmp_path: Path) -> None:
    repo = create_checkout(tmp_path)

    status = self_update.official_update_status(project_root=repo)

    assert status["status"] == "up_to_date"
    assert status["channel"] == "preview"
    assert status["branch"] == "dev"
    assert status["current_commit"] == status["latest_commit"]
    assert status["dirty"] is False
    assert status["can_check"] is True
    assert status["can_update"] is False


def test_installation_info_is_lightweight_and_normalized(tmp_path: Path) -> None:
    repo = create_checkout(tmp_path)
    commit = git(repo, "rev-parse", "HEAD")

    info = self_update.installation_info(project_root=repo)
    normalized = self_update.normalize_installation_info(
        {
            **info,
            "version": "0.1.2\x00 preview",
            "branch": "../../unsafe branch",
            "short_commit": "not-a-commit",
        },
        update_capability="1",
    )

    assert info == {
        "schema": 1,
        "version": self_update.__version__,
        "branch": "dev",
        "channel": "preview",
        "commit": commit,
        "short_commit": commit[:7],
    }
    assert normalized["reported"] is True
    assert normalized["version"] == "0.1.2 preview"
    assert normalized["branch"] == ""
    assert normalized["commit"] == commit
    assert normalized["short_commit"] == commit[:7]
    assert normalized["update_supported"] is True


def test_remote_update_payloads_are_bounded_and_allowlisted() -> None:
    status = self_update.normalize_update_status(
        {
            "ok": True,
            "status": "update_available",
            "branch": "dev",
            "channel": "preview",
            "current_version": "0.1.1",
            "current_commit": "a" * 40,
            "current_short_commit": "aaaaaaa",
            "latest_commit": "not-a-commit",
            "latest_subject": "x" * 500,
            "behind": 2,
            "can_update": True,
            "secret": "must not cross the Hub boundary",
        }
    )

    assert status["status"] == "update_available"
    assert status["current_commit"] == "a" * 40
    assert status["latest_commit"] == ""
    assert len(status["latest_subject"]) == 160
    assert status["behind"] == 2
    assert status["can_update"] is True
    assert "secret" not in status


def test_update_fast_forwards_to_known_official_commit(tmp_path: Path) -> None:
    repo = create_checkout(tmp_path)
    before, latest = make_remote_commit(repo)

    status = self_update.official_update_status(project_root=repo)
    result = self_update.apply_official_update(project_root=repo, refresh=False)

    assert status["status"] == "update_available"
    assert status["behind"] == 1
    assert status["can_update"] is True
    assert result["updated"] is True
    assert result["before"]["current_commit"] == before
    assert result["after"]["current_commit"] == latest
    assert git(repo, "rev-parse", "HEAD") == latest


def test_update_never_overwrites_dirty_checkout(tmp_path: Path) -> None:
    repo = create_checkout(tmp_path)
    before, _latest = make_remote_commit(repo)
    (repo / "README.md").write_text("my local work\n", encoding="utf-8")

    status = self_update.official_update_status(project_root=repo)

    assert status["status"] == "update_available"
    assert status["blocked_reason"] == "dirty_worktree"
    assert status["can_update"] is False
    with pytest.raises(self_update.StarAgentUpdateError) as error:
        self_update.apply_official_update(project_root=repo, refresh=False)
    assert error.value.code == "dirty_worktree"
    assert git(repo, "rev-parse", "HEAD") == before


def test_update_rejects_non_official_remote_and_unsupported_branch(tmp_path: Path) -> None:
    unofficial = create_checkout(
        tmp_path / "unofficial",
        remote="https://github.com/example/StarAgent.git",
    )
    feature = create_checkout(tmp_path / "feature", branch="feature")

    unofficial_status = self_update.official_update_status(project_root=unofficial)
    feature_status = self_update.official_update_status(project_root=feature)

    assert unofficial_status["status"] == "unavailable"
    assert unofficial_status["reason"] == "unofficial_remote"
    assert feature_status["status"] == "unavailable"
    assert feature_status["reason"] == "unsupported_branch"


def test_supervised_dashboard_restart_is_delayed(monkeypatch) -> None:
    callbacks = []
    killed = []

    class FakeTimer:
        daemon = False

        def __init__(self, delay, callback):  # type: ignore[no-untyped-def]
            assert delay == 1.5
            callbacks.append(callback)

        def start(self) -> None:
            callbacks[0]()

    monkeypatch.setenv("STARAGENT_TMUX_CHILD", "hub")
    monkeypatch.setattr(self_update.threading, "Timer", FakeTimer)
    monkeypatch.setattr(self_update.os, "getpid", lambda: 1234)
    monkeypatch.setattr(self_update.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert self_update.schedule_dashboard_restart(1.5) is True
    assert killed == [(1234, self_update.signal.SIGTERM)]


def test_unsupervised_dashboard_is_not_stopped(monkeypatch) -> None:
    monkeypatch.delenv("STARAGENT_TMUX_CHILD", raising=False)

    assert self_update.schedule_dashboard_restart() is False


def test_supervised_node_restart_targets_only_the_node_child(monkeypatch) -> None:
    callbacks = []
    killed = []

    class FakeTimer:
        daemon = False

        def __init__(self, delay, callback):  # type: ignore[no-untyped-def]
            assert delay == 0.5
            callbacks.append(callback)

        def start(self) -> None:
            callbacks[0]()

    monkeypatch.setenv("STARAGENT_TMUX_CHILD", "node")
    monkeypatch.setattr(self_update.threading, "Timer", FakeTimer)
    monkeypatch.setattr(self_update.os, "getpid", lambda: 4321)
    monkeypatch.setattr(self_update.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert self_update.schedule_node_restart(0.5) is True
    assert self_update.schedule_dashboard_restart(0.5) is False
    assert killed == [(4321, self_update.signal.SIGTERM)]


def test_node_reports_version_and_owns_its_update_lifecycle(monkeypatch, tmp_path: Path) -> None:
    calls = []
    before = {
        "status": "update_available",
        "branch": "dev",
        "channel": "preview",
        "current_commit": "a" * 40,
        "current_short_commit": "aaaaaaa",
        "latest_commit": "b" * 40,
        "latest_short_commit": "bbbbbbb",
        "behind": 1,
        "can_update": True,
    }
    after = {
        **before,
        "status": "up_to_date",
        "current_commit": "b" * 40,
        "current_short_commit": "bbbbbbb",
        "behind": 0,
        "can_update": False,
    }

    monkeypatch.setenv("STARAGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("STARAGENT_NODE_TOKEN", "node-secret")
    monkeypatch.setattr(node_app, "collect_session_views", lambda: [])
    monkeypatch.setattr(
        node_app,
        "current_installation_info",
        lambda: {
            "schema": 1,
            "version": "0.1.1",
            "branch": "dev",
            "channel": "preview",
            "commit": "a" * 40,
            "short_commit": "aaaaaaa",
        },
    )

    def status(*, refresh=False, service="hub"):  # type: ignore[no-untyped-def]
        calls.append(("check", refresh, service))
        return before

    monkeypatch.setattr(node_app, "official_update_status", status)
    monkeypatch.setattr(
        node_app,
        "apply_official_update",
        lambda *, service="hub": {
            "ok": True,
            "updated": True,
            "before": before,
            "after": after,
        },
    )
    monkeypatch.setattr(node_app, "schedule_node_restart", lambda: True)
    monkeypatch.setattr(node_app, "append_node_outbox_event", lambda *args, **kwargs: None)
    client = TestClient(node_app.create_app())
    headers = {"Authorization": "Bearer node-secret"}

    sessions = client.get("/api/sessions", headers=headers).json()
    checked = client.post("/api/staragent-update/check", headers=headers)
    installed = client.post("/api/staragent-update/apply", headers=headers)

    assert sessions["node"]["version"] == "0.1.1"
    assert sessions["capabilities"]["staragent_update"] == 1
    assert checked.status_code == 200
    assert checked.json()["latest_short_commit"] == "bbbbbbb"
    assert calls == [("check", True, "node")]
    assert installed.status_code == 200
    assert installed.json()["restart_scheduled"] is True
    assert installed.json()["after"]["current_short_commit"] == "bbbbbbb"
