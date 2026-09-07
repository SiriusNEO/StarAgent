from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from staragent.harness_config import harness_config_spec, harness_process_environment

SKILL_SCAN_CACHE_TTL_SECONDS = 30.0
SKILL_SCAN_MAX_ROOTS = 32
SKILL_SCAN_MAX_FILES = 256
SKILL_SCAN_MAX_DEPTH = 6
SKILL_SCAN_MAX_BYTES = 64 * 1024
SKILL_SCAN_MAX_ENTRIES_PER_ROOT = 2_048
SKILL_SCAN_CACHE_MAX_ENTRIES = 64
SKILL_DESCRIPTION_MAX_CHARS = 500
SKILL_NAME_MAX_CHARS = 120
SKILL_SOURCE_MAX_CHARS = 120
SKILL_ID_PATTERN = re.compile(r"[^a-z0-9._:-]+")
SKILL_SCOPES = {"bundled", "personal", "plugin"}
SKILL_KINDS = {"native", "compatible", "plugin"}
SKILL_DIRECTORIES = {
    "$CODEX_HOME/skills/.system",
    "$CODEX_HOME/skills",
    "$CLAUDE_CONFIG_DIR/skills",
    "$OPENCODE_CONFIG_DIR/skills",
    "~/.claude/skills",
    "~/.agents/skills",
    "Codex plugin cache",
    "Claude plugin cache",
}
FRONTMATTER_FIELD_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s*(.*))?$")


@dataclass(frozen=True)
class SkillRoot:
    id: str
    path: Path
    source: str
    directory: str
    scope: str
    kind: str = "native"
    plugin: str = ""
    skip_names: tuple[str, ...] = ()
    name_required: bool = True


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, object]]] = {}


def agent_skills_payload(agent: str, *, force: bool = False) -> dict[str, object]:
    """Return bounded metadata for the skill directories used by one Harness."""
    spec = harness_config_spec(agent)
    environment = harness_process_environment(spec.name)
    cache_key = skill_cache_key(spec.name, environment)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and not force and now - cached[0] < SKILL_SCAN_CACHE_TTL_SECONDS:
            return clone_payload(cached[1])

    roots = skill_roots(spec.name, environment)
    remaining = SKILL_SCAN_MAX_FILES
    skills: list[dict[str, object]] = []
    root_results: list[dict[str, object]] = []
    truncated = False
    for root in roots[:SKILL_SCAN_MAX_ROOTS]:
        result, discovered, root_truncated = scan_skill_root(root, limit=remaining)
        root_results.append(result)
        skills.extend(discovered)
        remaining = max(0, remaining - len(discovered))
        truncated = truncated or root_truncated
        if remaining == 0:
            truncated = True
            break

    skills.sort(
        key=lambda item: (
            skill_scope_order(str(item.get("scope") or "")),
            str(item.get("name") or "").casefold(),
            str(item.get("source") or "").casefold(),
        )
    )
    payload = {
        "supported": True,
        "agent": spec.name,
        "checked_at": utc_timestamp(),
        "cache_ttl_seconds": int(SKILL_SCAN_CACHE_TTL_SECONDS),
        "limits": {
            "max_files": SKILL_SCAN_MAX_FILES,
            "max_depth": SKILL_SCAN_MAX_DEPTH,
            "max_file_bytes": SKILL_SCAN_MAX_BYTES,
            "max_entries_per_root": SKILL_SCAN_MAX_ENTRIES_PER_ROOT,
        },
        "counts": skill_counts(skills),
        "roots": root_results,
        "skills": skills,
        "truncated": truncated or len(roots) > SKILL_SCAN_MAX_ROOTS,
        "error": "",
    }
    with _CACHE_LOCK:
        if len(_CACHE) >= SKILL_SCAN_CACHE_MAX_ENTRIES and cache_key not in _CACHE:
            oldest = min(_CACHE, key=lambda key: _CACHE[key][0])
            _CACHE.pop(oldest, None)
        _CACHE[cache_key] = (now, payload)
    return clone_payload(payload)


def unavailable_agent_skills(agent: str, error: str) -> dict[str, object]:
    spec = harness_config_spec(agent)
    return {
        "supported": False,
        "agent": spec.name,
        "checked_at": utc_timestamp(),
        "cache_ttl_seconds": int(SKILL_SCAN_CACHE_TTL_SECONDS),
        "limits": {
            "max_files": SKILL_SCAN_MAX_FILES,
            "max_depth": SKILL_SCAN_MAX_DEPTH,
            "max_file_bytes": SKILL_SCAN_MAX_BYTES,
            "max_entries_per_root": SKILL_SCAN_MAX_ENTRIES_PER_ROOT,
        },
        "counts": skill_counts([]),
        "roots": [],
        "skills": [],
        "truncated": False,
        "error": clean_text(error, 500),
    }


def normalize_agent_skills_payload(agent: str, payload: object) -> dict[str, object]:
    """Normalize a Node response at the Hub trust boundary."""
    spec = harness_config_spec(agent)
    if not isinstance(payload, dict):
        return unavailable_agent_skills(spec.name, "Node returned an invalid Skills response.")

    raw_roots = payload.get("roots") if isinstance(payload.get("roots"), list) else []
    roots: list[dict[str, object]] = []
    for raw in raw_roots[:SKILL_SCAN_MAX_ROOTS]:
        if not isinstance(raw, dict):
            continue
        scope = clean_choice(raw.get("scope"), SKILL_SCOPES, "personal")
        kind = clean_choice(raw.get("kind"), SKILL_KINDS, "native")
        roots.append(
            {
                "id": clean_identifier(raw.get("id"), fallback=f"root-{len(roots) + 1}"),
                "source": clean_text(raw.get("source"), SKILL_SOURCE_MAX_CHARS),
                "directory": clean_choice(raw.get("directory"), SKILL_DIRECTORIES, ""),
                "scope": scope,
                "kind": kind,
                "plugin": clean_text(raw.get("plugin"), SKILL_SOURCE_MAX_CHARS),
                "exists": bool(raw.get("exists")),
                "count": min(SKILL_SCAN_MAX_FILES, max(0, safe_int(raw.get("count")))),
                "truncated": bool(raw.get("truncated")),
                "error": clean_text(raw.get("error"), 240),
            }
        )

    raw_skills = payload.get("skills") if isinstance(payload.get("skills"), list) else []
    skills: list[dict[str, object]] = []
    for raw in raw_skills[:SKILL_SCAN_MAX_FILES]:
        if not isinstance(raw, dict):
            continue
        name = clean_text(raw.get("name"), SKILL_NAME_MAX_CHARS)
        if not name:
            continue
        scope = clean_choice(raw.get("scope"), SKILL_SCOPES, "personal")
        kind = clean_choice(raw.get("kind"), SKILL_KINDS, "native")
        skills.append(
            {
                "id": clean_identifier(raw.get("id"), fallback=f"skill-{len(skills) + 1}"),
                "name": name,
                "description": clean_text(raw.get("description"), SKILL_DESCRIPTION_MAX_CHARS),
                "source": clean_text(raw.get("source"), SKILL_SOURCE_MAX_CHARS),
                "scope": scope,
                "kind": kind,
                "plugin": clean_text(raw.get("plugin"), SKILL_SOURCE_MAX_CHARS),
                "valid": bool(raw.get("valid")),
                "modified_at": clean_text(raw.get("modified_at"), 80),
            }
        )

    skills.sort(
        key=lambda item: (
            skill_scope_order(str(item["scope"])),
            str(item["name"]).casefold(),
            str(item["source"]).casefold(),
        )
    )
    return {
        "supported": bool(payload.get("supported", True)),
        "agent": spec.name,
        "checked_at": clean_text(payload.get("checked_at"), 80),
        "cache_ttl_seconds": int(SKILL_SCAN_CACHE_TTL_SECONDS),
        "limits": {
            "max_files": SKILL_SCAN_MAX_FILES,
            "max_depth": SKILL_SCAN_MAX_DEPTH,
            "max_file_bytes": SKILL_SCAN_MAX_BYTES,
            "max_entries_per_root": SKILL_SCAN_MAX_ENTRIES_PER_ROOT,
        },
        "counts": skill_counts(skills),
        "roots": roots,
        "skills": skills,
        "truncated": bool(payload.get("truncated")) or len(raw_skills) > len(skills),
        "error": clean_text(payload.get("error"), 500),
    }


def skill_roots(agent: str, environment: dict[str, str]) -> list[SkillRoot]:
    home = harness_home(environment)
    if agent == "codex":
        codex_home = configured_directory(environment.get("CODEX_HOME"), home / ".codex", home)
        roots = [
            SkillRoot(
                "codex-bundled",
                codex_home / "skills" / ".system",
                "Codex built-in",
                "$CODEX_HOME/skills/.system",
                "bundled",
            ),
            SkillRoot(
                "codex-personal",
                codex_home / "skills",
                "Codex personal",
                "$CODEX_HOME/skills",
                "personal",
                skip_names=(".system",),
            ),
            SkillRoot(
                "agents-compatible",
                home / ".agents" / "skills",
                "Agent Skills compatible",
                "~/.agents/skills",
                "personal",
                kind="compatible",
            ),
        ]
        roots.extend(codex_plugin_skill_roots(codex_home))
        return roots
    if agent == "claude":
        claude_home = configured_directory(
            environment.get("CLAUDE_CONFIG_DIR"), home / ".claude", home
        )
        roots = [
            SkillRoot(
                "claude-personal",
                claude_home / "skills",
                "Claude Code personal",
                "$CLAUDE_CONFIG_DIR/skills",
                "personal",
                name_required=False,
            )
        ]
        roots.extend(claude_plugin_skill_roots(claude_home, home))
        return roots

    config_home = configured_directory(
        environment.get("OPENCODE_CONFIG_DIR"),
        configured_directory(environment.get("XDG_CONFIG_HOME"), home / ".config", home)
        / "opencode",
        home,
    )
    return [
        SkillRoot(
            "opencode-personal",
            config_home / "skills",
            "OpenCode personal",
            "$OPENCODE_CONFIG_DIR/skills",
            "personal",
        ),
        SkillRoot(
            "claude-compatible",
            home / ".claude" / "skills",
            "Claude-compatible",
            "~/.claude/skills",
            "personal",
            kind="compatible",
        ),
        SkillRoot(
            "agents-compatible",
            home / ".agents" / "skills",
            "Agent Skills compatible",
            "~/.agents/skills",
            "personal",
            kind="compatible",
        ),
    ]


def codex_plugin_skill_roots(codex_home: Path) -> list[SkillRoot]:
    cache = codex_home / "plugins" / "cache"
    if not cache.is_dir():
        return []
    roots: list[SkillRoot] = []
    for marketplace in safe_child_directories(cache, limit=64):
        for plugin_dir in safe_child_directories(marketplace, limit=64):
            if len(roots) >= SKILL_SCAN_MAX_ROOTS - 3:
                return roots
            marker = plugin_dir / ".codex-remote-plugin-install.json"
            if marker.is_symlink() or not marker.is_file():
                continue
            manifest_info = newest_plugin_manifest(plugin_dir)
            if not manifest_info:
                continue
            version_dir, manifest = manifest_info
            relative_skills = manifest.get("skills")
            if not isinstance(relative_skills, str) or not relative_skills.strip():
                continue
            skills_path = contained_child(version_dir, relative_skills)
            if skills_path is None:
                continue
            plugin_name = clean_text(
                manifest.get("name") or plugin_dir.name, SKILL_SOURCE_MAX_CHARS
            )
            root_id = clean_identifier(
                f"codex-plugin:{marketplace.name}:{plugin_dir.name}",
                fallback=f"codex-plugin-{len(roots) + 1}",
            )
            roots.append(
                SkillRoot(
                    root_id,
                    skills_path,
                    plugin_name or "Codex plugin",
                    "Codex plugin cache",
                    "plugin",
                    kind="plugin",
                    plugin=plugin_name,
                )
            )
    return roots


def claude_plugin_skill_roots(claude_home: Path, home: Path) -> list[SkillRoot]:
    settings = read_small_json(claude_home / "settings.json") or {}
    enabled_payload = settings.get("enabledPlugins")
    enabled = (
        {str(name) for name, value in enabled_payload.items() if value is True}
        if isinstance(enabled_payload, dict)
        else set()
    )
    registry = read_small_json(claude_home / "plugins" / "installed_plugins.json") or {}
    installed = registry.get("plugins")
    if not enabled or not isinstance(installed, dict):
        return []

    cache = claude_home / "plugins" / "cache"
    try:
        resolved_cache = cache.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    roots: list[SkillRoot] = []
    for plugin_id in sorted(enabled, key=str.casefold)[:SKILL_SCAN_MAX_ROOTS]:
        raw_entries = installed.get(plugin_id)
        entries = raw_entries if isinstance(raw_entries, list) else [raw_entries]
        candidates = [
            entry
            for entry in entries[:32]
            if isinstance(entry, dict) and entry.get("scope") in {None, "", "user"}
        ]
        candidates.sort(
            key=lambda item: str(item.get("lastUpdated") or item.get("installedAt") or ""),
            reverse=True,
        )
        install_root = None
        for entry in candidates:
            install_path = entry.get("installPath")
            if not isinstance(install_path, str) or not install_path.strip():
                continue
            candidate = configured_directory(install_path, resolved_cache, home)
            if path_is_within(candidate, resolved_cache) and candidate.is_dir():
                install_root = candidate.resolve()
                break
        if install_root is None:
            continue

        manifest_path = install_root / ".claude-plugin" / "plugin.json"
        manifest = {}
        if path_is_within(manifest_path, install_root):
            manifest = read_small_json(manifest_path) or {}
        plugin_name = clean_text(
            manifest.get("name") or plugin_id.split("@", 1)[0],
            SKILL_SOURCE_MAX_CHARS,
        )
        skill_directories = [install_root / "skills"]
        custom = manifest.get("skills")
        custom_paths = custom if isinstance(custom, list) else [custom]
        for relative in custom_paths[:16]:
            if not isinstance(relative, str) or not relative.startswith("./"):
                continue
            custom_path = contained_child(install_root, relative)
            if custom_path is not None:
                skill_directories.append(custom_path)

        seen: set[Path] = set()
        for index, directory in enumerate(skill_directories):
            try:
                resolved_directory = directory.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved_directory in seen or not path_is_within(resolved_directory, install_root):
                continue
            seen.add(resolved_directory)
            roots.append(
                SkillRoot(
                    clean_identifier(
                        f"claude-plugin:{plugin_id}:{index}",
                        fallback=f"claude-plugin-{len(roots) + 1}",
                    ),
                    resolved_directory,
                    plugin_name or "Claude Code plugin",
                    "Claude plugin cache",
                    "plugin",
                    kind="plugin",
                    plugin=plugin_name,
                    name_required=False,
                )
            )
            if len(roots) >= SKILL_SCAN_MAX_ROOTS - 1:
                return roots
    return roots


def newest_plugin_manifest(plugin_dir: Path) -> tuple[Path, dict[str, object]] | None:
    candidates: list[tuple[int, str, Path, dict[str, object]]] = []
    for version_dir in safe_child_directories(plugin_dir, limit=32):
        manifest_path = version_dir / ".codex-plugin" / "plugin.json"
        if not path_is_within(manifest_path, version_dir):
            continue
        manifest = read_small_json(manifest_path)
        if manifest is None:
            continue
        try:
            modified = manifest_path.stat().st_mtime_ns
        except OSError:
            modified = 0
        candidates.append((modified, version_dir.name, version_dir, manifest))
    if not candidates:
        return None
    _modified, _name, version_dir, manifest = max(candidates, key=lambda item: item[:2])
    return version_dir, manifest


def scan_skill_root(
    root: SkillRoot,
    *,
    limit: int,
) -> tuple[dict[str, object], list[dict[str, object]], bool]:
    result = {
        "id": root.id,
        "source": root.source,
        "directory": root.directory,
        "scope": root.scope,
        "kind": root.kind,
        "plugin": root.plugin,
        "exists": False,
        "count": 0,
        "truncated": False,
        "error": "",
    }
    if limit <= 0:
        result["truncated"] = True
        return result, [], True
    try:
        root_path = root.path.resolve(strict=True)
    except (OSError, RuntimeError):
        return result, [], False
    if not root_path.is_dir():
        return result, [], False
    result["exists"] = True

    skills: list[dict[str, object]] = []
    truncated = False
    scanned_entries = 0
    pending = [(root_path, 0)]
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    if scanned_entries >= SKILL_SCAN_MAX_ENTRIES_PER_ROOT:
                        truncated = True
                        pending.clear()
                        break
                    entries.append(entry)
                    scanned_entries += 1
                entries.sort(key=lambda entry: entry.name.casefold())
        except OSError:
            result["error"] = "One or more skill directories could not be read."
            continue
        for entry in entries:
            if entry.is_symlink() or entry.name in root.skip_names:
                continue
            if entry.is_file(follow_symlinks=False) and entry.name == "SKILL.md":
                if len(skills) >= limit:
                    truncated = True
                    pending.clear()
                    break
                path = Path(entry.path)
                if not path_is_within(path, root_path):
                    continue
                skills.append(skill_metadata(path, root))
            elif entry.is_dir(follow_symlinks=False):
                if depth < SKILL_SCAN_MAX_DEPTH:
                    pending.append((Path(entry.path), depth + 1))
                else:
                    truncated = True
    result["count"] = len(skills)
    result["truncated"] = truncated
    return result, skills, truncated


def skill_metadata(path: Path, root: SkillRoot) -> dict[str, object]:
    frontmatter, complete = read_skill_frontmatter(path)
    name = clean_text(frontmatter.get("name") or path.parent.name, SKILL_NAME_MAX_CHARS)
    description = clean_text(frontmatter.get("description"), SKILL_DESCRIPTION_MAX_CHARS)
    identifier = clean_identifier(f"{root.id}:{path.parent.name}", fallback=root.id)
    return {
        "id": identifier,
        "name": name or "Unnamed skill",
        "description": description,
        "source": root.source,
        "scope": root.scope,
        "kind": root.kind,
        "plugin": root.plugin,
        "valid": bool(
            complete
            and frontmatter.get("description")
            and (frontmatter.get("name") or not root.name_required)
        ),
        "modified_at": file_modified_at(path),
    }


def read_skill_frontmatter(path: Path) -> tuple[dict[str, str], bool]:
    try:
        with path.open("rb") as handle:
            raw_lines = []
            total_bytes = 0
            while True:
                remaining = SKILL_SCAN_MAX_BYTES - total_bytes
                if remaining <= 0:
                    return {}, False
                line = handle.readline(remaining + 1)
                if not line:
                    return {}, False
                total_bytes += len(line)
                if total_bytes > SKILL_SCAN_MAX_BYTES:
                    return {}, False
                raw_lines.append(line)
                marker = line.rstrip(b" \t\r\n")
                if len(raw_lines) == 1 and marker != b"---":
                    return {}, False
                if len(raw_lines) > 1 and marker == b"---":
                    break
    except OSError:
        return {}, False
    text = b"".join(raw_lines).decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.splitlines()
    if not lines or lines[0].rstrip(" \t") != "---":
        return {}, False
    closing = next(
        (index for index, line in enumerate(lines[1:], 1) if line.rstrip(" \t") == "---"),
        -1,
    )
    if closing < 0:
        return {}, False
    fields: dict[str, str] = {}
    index = 1
    while index < closing:
        match = FRONTMATTER_FIELD_PATTERN.match(lines[index])
        if not match:
            index += 1
            continue
        key = match.group(1).lower()
        value = (match.group(2) or "").strip()
        if value in {"|", ">", "|-", ">-", "|+", ">+"}:
            block: list[str] = []
            index += 1
            while index < closing and (not lines[index].strip() or lines[index][:1].isspace()):
                block.append(lines[index].strip())
                index += 1
            value = " ".join(part for part in block if part)
            fields[key] = value
            continue
        fields[key] = unquote_yaml_scalar(value)
        index += 1
    return fields, True


def safe_child_directories(path: Path, *, limit: int) -> list[Path]:
    try:
        with os.scandir(path) as iterator:
            children = []
            for index, entry in enumerate(iterator):
                if index >= max(64, limit * 4):
                    break
                if not entry.is_symlink() and entry.is_dir(follow_symlinks=False):
                    children.append(Path(entry.path))
                    if len(children) >= limit:
                        break
        return sorted(children, key=lambda item: item.name.casefold())
    except OSError:
        return []


def contained_child(parent: Path, relative: str) -> Path | None:
    candidate = Path(relative.strip())
    if candidate.is_absolute():
        return None
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved = (parent / candidate).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_dir() or not path_is_within(resolved, resolved_parent):
        return None
    return resolved


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def read_small_json(path: Path) -> dict[str, object] | None:
    try:
        if path.is_symlink() or path.stat().st_size > SKILL_SCAN_MAX_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def harness_home(environment: dict[str, str]) -> Path:
    configured = environment.get("USERPROFILE") if os.name == "nt" else environment.get("HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home()


def configured_directory(value: str | None, fallback: Path, home: Path) -> Path:
    if not value:
        return fallback
    if value == "~":
        return home
    if value.startswith(("~/", "~\\")):
        return home / value[2:]
    path = Path(value)
    return path if path.is_absolute() else home / path


def skill_cache_key(agent: str, environment: dict[str, str]) -> str:
    relevant_names = {
        "codex": ("HOME", "USERPROFILE", "CODEX_HOME"),
        "claude": ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR"),
        "opencode": (
            "HOME",
            "USERPROFILE",
            "OPENCODE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
        ),
    }[agent]
    return "\0".join([agent, *(environment.get(name, "") for name in relevant_names)])


def skill_counts(skills: list[dict[str, object]]) -> dict[str, int]:
    bundled = sum(item.get("scope") == "bundled" for item in skills)
    personal = sum(item.get("scope") == "personal" for item in skills)
    plugin = sum(item.get("scope") == "plugin" for item in skills)
    return {
        "total": len(skills),
        "bundled": bundled,
        "installed": personal + plugin,
        "personal": personal,
        "plugin": plugin,
    }


def skill_scope_order(scope: str) -> int:
    return {"bundled": 0, "personal": 1, "plugin": 2}.get(scope, 3)


def unquote_yaml_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def clean_identifier(value: object, *, fallback: str) -> str:
    cleaned = SKILL_ID_PATTERN.sub("-", clean_text(value, 180).lower()).strip("-")
    return cleaned or fallback


def clean_choice(value: object, choices: set[str], fallback: str) -> str:
    cleaned = str(value or "").strip().lower()
    return cleaned if cleaned in choices else fallback


def clean_text(value: object, max_chars: int) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text[:max_chars]


def safe_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def file_modified_at(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        return ""


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def clone_payload(payload: dict[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(payload))


def clear_agent_skills_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
