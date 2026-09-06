from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypeVar
from urllib.parse import urlsplit

from staragent import __version__
from staragent.paths import PROJECT_ROOT

OFFICIAL_REPOSITORY = "SiriusNEO/StarAgent"
OFFICIAL_REPOSITORY_URL = "https://github.com/SiriusNEO/StarAgent"
SUPPORTED_BRANCHES = {"main": "stable", "dev": "preview"}
GIT_TIMEOUT_SECONDS = 30.0
RESTART_DELAY_SECONDS = 1.0
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_UPDATE_LOCK = threading.Lock()
T = TypeVar("T")
ServiceKind = Literal["hub", "node"]
INSTALLATION_INFO_SCHEMA = 1
STARAGENT_UPDATE_CONFLICTS = frozenset(
    {
        "ahead",
        "branch_changed",
        "detached_head",
        "dirty_worktree",
        "diverged",
        "git_missing",
        "missing_remote",
        "not_git_checkout",
        "unofficial_remote",
        "unchecked",
        "unsupported_branch",
        "update_blocked",
        "update_busy",
    }
)
UPDATE_STATUSES = frozenset(
    {"ahead", "diverged", "unavailable", "unchecked", "up_to_date", "update_available"}
)


class StarAgentUpdateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StarAgentUpdateBusyError(StarAgentUpdateError):
    def __init__(self) -> None:
        super().__init__("update_busy", "Another StarAgent update operation is already running.")


def official_update_status(
    *,
    refresh: bool = False,
    project_root: Path | None = None,
    service: ServiceKind = "hub",
) -> dict[str, object]:
    root = (project_root or PROJECT_ROOT).resolve()
    return _run_locked(
        lambda: _collect_update_status(root, refresh=refresh, service=service),
        wait=GIT_TIMEOUT_SECONDS + 5.0,
    )


def apply_official_update(
    *,
    project_root: Path | None = None,
    refresh: bool = True,
    service: ServiceKind = "hub",
) -> dict[str, object]:
    root = (project_root or PROJECT_ROOT).resolve()

    def apply() -> dict[str, object]:
        before = _collect_update_status(root, refresh=refresh, service=service)
        status = str(before["status"])
        if status == "up_to_date":
            return {"ok": True, "updated": False, "before": before, "after": before}
        if status != "update_available":
            reason = str(before.get("blocked_reason") or before.get("reason") or status)
            raise StarAgentUpdateError(reason, update_blocked_message(reason))
        if not before.get("can_update"):
            reason = str(before.get("blocked_reason") or "update_blocked")
            raise StarAgentUpdateError(reason, update_blocked_message(reason))

        target = str(before.get("latest_commit") or "")
        if not _COMMIT_PATTERN.fullmatch(target):
            raise StarAgentUpdateError(
                "invalid_remote_commit",
                "The official update reference did not resolve to a valid commit.",
            )

        # Re-check the two mutable safety conditions immediately before changing the checkout.
        if _current_branch(root) != before["branch"]:
            raise StarAgentUpdateError(
                "branch_changed",
                "The checked-out branch changed while the update was being prepared.",
            )
        if _working_tree_dirty(root):
            raise StarAgentUpdateError(
                "dirty_worktree",
                "Commit, stash, or remove local changes before updating StarAgent.",
            )

        _git(root, "merge", "--ff-only", "--no-edit", target)
        after = _collect_update_status(root, refresh=False, service=service)
        if after.get("current_commit") != target:
            raise StarAgentUpdateError(
                "update_verification_failed",
                "Git completed, but the StarAgent checkout did not reach the expected commit.",
            )
        return {"ok": True, "updated": True, "before": before, "after": after}

    return _run_locked(apply)


def dashboard_restart_supported() -> bool:
    return service_restart_supported("hub")


def schedule_dashboard_restart(delay: float = RESTART_DELAY_SECONDS) -> bool:
    return schedule_service_restart("hub", delay)


def node_restart_supported() -> bool:
    return service_restart_supported("node")


def schedule_node_restart(delay: float = RESTART_DELAY_SECONDS) -> bool:
    return schedule_service_restart("node", delay)


def service_restart_supported(service: ServiceKind) -> bool:
    return os.environ.get("STARAGENT_TMUX_CHILD") == service


def schedule_service_restart(service: ServiceKind, delay: float = RESTART_DELAY_SECONDS) -> bool:
    if not service_restart_supported(service):
        return False

    def stop_service() -> None:
        with suppress(OSError):
            os.kill(os.getpid(), signal.SIGTERM)

    timer = threading.Timer(max(0.1, delay), stop_service)
    timer.daemon = True
    timer.start()
    return True


def installation_info(
    *,
    project_root: Path | None = None,
) -> dict[str, object]:
    root = (project_root or PROJECT_ROOT).resolve()
    payload: dict[str, object] = {
        "schema": INSTALLATION_INFO_SCHEMA,
        "version": __version__,
        "branch": "",
        "channel": "",
        "commit": "",
        "short_commit": "",
    }
    if not shutil.which("git") or not (root / ".git").exists():
        return payload
    branch = _current_branch(root)
    commit = _git_optional(root, "rev-parse", "HEAD")
    payload["branch"] = branch
    payload["channel"] = SUPPORTED_BRANCHES.get(branch, "")
    if _COMMIT_PATTERN.fullmatch(commit):
        payload["commit"] = commit
        payload["short_commit"] = commit[:7]
    return payload


@lru_cache(maxsize=1)
def _cached_installation_info() -> tuple[tuple[str, object], ...]:
    return tuple(installation_info().items())


def current_installation_info() -> dict[str, object]:
    """Return process-stable install metadata without running Git on every heartbeat."""
    return dict(_cached_installation_info())


def normalize_installation_info(
    value: object,
    *,
    update_capability: object = 0,
) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    version = _clean_metadata(source.get("version"), max_chars=40)
    branch = _clean_branch(source.get("branch"))
    commit = _clean_commit(source.get("commit"))
    short_commit = _clean_short_commit(source.get("short_commit"))
    if commit and not short_commit:
        short_commit = commit[:7]
    channel = str(source.get("channel") or "").strip().lower()
    if channel not in SUPPORTED_BRANCHES.values():
        channel = SUPPORTED_BRANCHES.get(branch, "")
    try:
        capability = max(0, min(int(update_capability or 0), 100))
    except (TypeError, ValueError, OverflowError):
        capability = 0
    return {
        "reported": bool(version or commit),
        "schema": INSTALLATION_INFO_SCHEMA if source.get("schema") == 1 else 0,
        "version": version,
        "branch": branch,
        "channel": channel,
        "commit": commit,
        "short_commit": short_commit,
        "update_capability": capability,
        "update_supported": capability >= 1,
    }


def normalize_update_status(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    status = _clean_code(source.get("status"))
    if status not in UPDATE_STATUSES:
        status = "unavailable"
    branch = _clean_branch(source.get("branch"))
    channel = str(source.get("channel") or "").strip().lower()
    if channel not in SUPPORTED_BRANCHES.values():
        channel = SUPPORTED_BRANCHES.get(branch, "")
    return {
        "ok": source.get("ok", True) is True,
        "status": status,
        "reason": _clean_code(source.get("reason")),
        "blocked_reason": _clean_code(source.get("blocked_reason")),
        "error": _clean_code(source.get("error")),
        "detail": _clean_metadata(source.get("detail"), max_chars=300),
        "repository": OFFICIAL_REPOSITORY,
        "repository_url": OFFICIAL_REPOSITORY_URL,
        "branch": branch,
        "channel": channel,
        "current_version": _clean_metadata(source.get("current_version"), max_chars=40),
        "current_commit": _clean_commit(source.get("current_commit")),
        "current_short_commit": _clean_short_commit(source.get("current_short_commit")),
        "current_subject": _clean_metadata(source.get("current_subject"), max_chars=160),
        "current_committed_at": _clean_metadata(source.get("current_committed_at"), max_chars=40),
        "latest_commit": _clean_commit(source.get("latest_commit")),
        "latest_short_commit": _clean_short_commit(source.get("latest_short_commit")),
        "latest_subject": _clean_metadata(source.get("latest_subject"), max_chars=160),
        "latest_committed_at": _clean_metadata(source.get("latest_committed_at"), max_chars=40),
        "ahead": _bounded_nonnegative_int(source.get("ahead")),
        "behind": _bounded_nonnegative_int(source.get("behind")),
        "dirty": source.get("dirty") is True,
        "can_check": source.get("can_check") is True,
        "can_update": source.get("can_update") is True,
        "refreshed": source.get("refreshed") is True,
        "restart_supported": source.get("restart_supported") is True,
        "checked_at": _clean_metadata(source.get("checked_at"), max_chars=40),
    }


def normalize_update_result(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    if source.get("ok") is not True:
        return {
            "ok": False,
            "error": _clean_code(source.get("error")) or "update_failed",
            "detail": _clean_metadata(source.get("detail"), max_chars=300),
            "updated": False,
            "restart_scheduled": False,
        }
    return {
        "ok": True,
        "updated": source.get("updated") is True,
        "before": normalize_update_status(source.get("before")),
        "after": normalize_update_status(source.get("after")),
        "restart_scheduled": source.get("restart_scheduled") is True,
    }


def is_official_remote(remote_url: str) -> bool:
    value = remote_url.strip()
    if not value:
        return False

    scp_match = re.fullmatch(
        r"(?:[^@/\s]+@)?github\.com:(?P<path>[^\s]+)",
        value,
        flags=re.IGNORECASE,
    )
    if scp_match:
        path = scp_match.group("path")
    else:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"https", "ssh", "git+ssh"}:
            return False
        if (parsed.hostname or "").lower() != "github.com":
            return False
        path = parsed.path

    slug = path.strip("/")
    if slug.lower().endswith(".git"):
        slug = slug[:-4]
    return slug.lower() == OFFICIAL_REPOSITORY.lower()


def update_blocked_message(reason: str) -> str:
    messages = {
        "dirty_worktree": "Commit, stash, or remove local changes before updating StarAgent.",
        "detached_head": "Check out the main or dev branch before updating StarAgent.",
        "unsupported_branch": "Official updates are available only on the main and dev branches.",
        "unofficial_remote": "The origin remote is not the official StarAgent repository.",
        "missing_remote": "The StarAgent checkout does not have an origin remote.",
        "not_git_checkout": "This StarAgent installation is not a Git checkout.",
        "git_missing": "Git is not installed or is not available in PATH.",
        "ahead": "This checkout contains commits that are not on the official branch.",
        "diverged": "This checkout has diverged from the official branch.",
        "unchecked": "Check for updates before installing one.",
    }
    return messages.get(reason, "StarAgent cannot safely apply this update.")


def _run_locked(operation: Callable[[], T], *, wait: float = 0.0) -> T:
    acquired = _UPDATE_LOCK.acquire(timeout=wait) if wait else _UPDATE_LOCK.acquire(blocking=False)
    if not acquired:
        raise StarAgentUpdateBusyError()
    try:
        return operation()
    finally:
        _UPDATE_LOCK.release()


def _collect_update_status(
    root: Path,
    *,
    refresh: bool,
    service: ServiceKind,
) -> dict[str, object]:
    payload = _base_status(service)
    if not shutil.which("git"):
        return _unavailable(payload, "git_missing")
    if not (root / ".git").exists():
        return _unavailable(payload, "not_git_checkout")

    inside = _git_optional(root, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return _unavailable(payload, "not_git_checkout")

    branch = _current_branch(root)
    if not branch:
        return _unavailable(payload, "detached_head")
    payload["branch"] = branch
    payload["channel"] = SUPPORTED_BRANCHES.get(branch, "")
    if branch not in SUPPORTED_BRANCHES:
        return _unavailable(payload, "unsupported_branch")

    remote_url = _git_optional(root, "remote", "get-url", "origin")
    if not remote_url:
        return _unavailable(payload, "missing_remote")
    if not is_official_remote(remote_url):
        return _unavailable(payload, "unofficial_remote")

    payload["can_check"] = True
    payload["current_version"] = __version__
    payload.update(_commit_fields("current", _commit_info(root, "HEAD")))
    payload["dirty"] = _working_tree_dirty(root)

    remote_ref = f"refs/remotes/origin/{branch}"
    if refresh:
        _git(
            root,
            "fetch",
            "--quiet",
            "--no-tags",
            "origin",
            f"refs/heads/{branch}:{remote_ref}",
            timeout=GIT_TIMEOUT_SECONDS,
        )
        payload["refreshed"] = True

    if not _git_optional(root, "rev-parse", "--verify", remote_ref):
        payload.update(status="unchecked", reason="unchecked", blocked_reason="unchecked")
        return payload

    payload.update(_commit_fields("latest", _commit_info(root, remote_ref)))
    counts = _git(root, "rev-list", "--left-right", "--count", f"HEAD...{remote_ref}")
    try:
        ahead, behind = (int(value) for value in counts.split())
    except (TypeError, ValueError) as exc:
        raise StarAgentUpdateError(
            "invalid_git_response",
            "Git returned an unexpected commit comparison.",
        ) from exc
    payload["ahead"] = ahead
    payload["behind"] = behind

    if ahead == 0 and behind == 0:
        payload.update(status="up_to_date", reason="", blocked_reason="")
    elif ahead == 0:
        blocked_reason = "dirty_worktree" if payload["dirty"] else ""
        payload.update(
            status="update_available",
            reason="",
            blocked_reason=blocked_reason,
            can_update=not payload["dirty"],
        )
    elif behind == 0:
        payload.update(status="ahead", reason="ahead", blocked_reason="ahead")
    else:
        payload.update(status="diverged", reason="diverged", blocked_reason="diverged")
    return payload


def _base_status(service: ServiceKind) -> dict[str, object]:
    return {
        "status": "unavailable",
        "reason": "",
        "blocked_reason": "",
        "repository": OFFICIAL_REPOSITORY,
        "repository_url": OFFICIAL_REPOSITORY_URL,
        "branch": "",
        "channel": "",
        "current_version": __version__,
        "current_commit": "",
        "current_short_commit": "",
        "current_subject": "",
        "current_committed_at": "",
        "latest_commit": "",
        "latest_short_commit": "",
        "latest_subject": "",
        "latest_committed_at": "",
        "ahead": 0,
        "behind": 0,
        "dirty": False,
        "can_check": False,
        "can_update": False,
        "refreshed": False,
        "restart_supported": service_restart_supported(service),
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _unavailable(payload: dict[str, object], reason: str) -> dict[str, object]:
    payload.update(status="unavailable", reason=reason, blocked_reason=reason)
    return payload


def _current_branch(root: Path) -> str:
    return _git_optional(root, "symbolic-ref", "--quiet", "--short", "HEAD")


def _working_tree_dirty(root: Path) -> bool:
    return bool(_git(root, "status", "--porcelain=v1", "--untracked-files=normal"))


def _commit_info(root: Path, ref: str) -> dict[str, str]:
    output = _git(root, "show", "-s", "--format=%H%x00%h%x00%ct%x00%s", ref)
    values = output.split("\0", 3)
    if len(values) != 4 or not _COMMIT_PATTERN.fullmatch(values[0]):
        raise StarAgentUpdateError(
            "invalid_git_response",
            "Git returned unexpected commit metadata.",
        )
    try:
        committed_at = datetime.fromtimestamp(int(values[2]), UTC).isoformat(timespec="seconds")
    except (OverflowError, ValueError) as exc:
        raise StarAgentUpdateError(
            "invalid_git_response",
            "Git returned an invalid commit timestamp.",
        ) from exc
    return {
        "commit": values[0],
        "short_commit": values[1],
        "subject": values[3],
        "committed_at": committed_at.replace("+00:00", "Z"),
    }


def _commit_fields(prefix: str, info: dict[str, str]) -> dict[str, str]:
    return {f"{prefix}_{key}": value for key, value in info.items()}


def _git_optional(root: Path, *args: str) -> str:
    try:
        return _git(root, *args)
    except StarAgentUpdateError:
        return ""


def _git(root: Path, *args: str, timeout: float = 10.0) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise StarAgentUpdateError("git_missing", "Git is not available in PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise StarAgentUpdateError("git_timeout", "The Git operation timed out.") from exc
    if result.returncode != 0:
        message = _safe_git_error(result.stderr or result.stdout)
        raise StarAgentUpdateError("git_failed", message or "The Git operation failed.")
    return result.stdout.strip()


def _safe_git_error(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    message = lines[-1] if lines else ""
    message = re.sub(r"(https?://)[^/@\s]+@", r"\1", message)
    return message[:300]


def _clean_metadata(value: object, *, max_chars: int) -> str:
    return " ".join(str(value or "").replace("\x00", "").split())[:max_chars]


def _clean_branch(value: object) -> str:
    branch = str(value or "").strip()
    return branch if re.fullmatch(r"[A-Za-z0-9._/-]{1,80}", branch) else ""


def _clean_commit(value: object) -> str:
    commit = str(value or "").strip().lower()
    return commit if _COMMIT_PATTERN.fullmatch(commit) else ""


def _clean_short_commit(value: object) -> str:
    commit = str(value or "").strip().lower()
    return commit if re.fullmatch(r"[0-9a-f]{7,12}", commit) else ""


def _clean_code(value: object) -> str:
    code = str(value or "").strip().lower()
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) else ""


def _bounded_nonnegative_int(value: object) -> int:
    try:
        return max(0, min(int(value or 0), 1_000_000))
    except (TypeError, ValueError, OverflowError):
        return 0
