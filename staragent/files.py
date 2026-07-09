from __future__ import annotations

import mimetypes
from pathlib import Path

SENSITIVE_PATH_PARTS = {".ssh", ".aws", ".gnupg", ".staragent"}
SENSITIVE_FILE_NAMES = {".env", "dashboard.env", "id_rsa", "id_ed25519"}


def directory_listing(
    path: str | None = None,
    include_files: bool = False,
    root: str | None = None,
) -> dict[str, object]:
    root_path = resolve_root(root)
    current = secure_resolve_path(path or str(root_path or Path.cwd()), root_path)
    if not current.exists():
        raise ValueError(f"Path does not exist: {path}")
    if not current.is_dir():
        current = current.parent

    try:
        children = sorted(
            current.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
    except OSError as exc:
        raise ValueError(str(exc)) from exc

    entries = []
    for child in children:
        is_dir = child.is_dir()
        if not is_dir and not include_files:
            continue
        if is_dir and child.name in {".git", "__pycache__", "node_modules", ".venv", "venv"}:
            continue
        if is_sensitive_path(child):
            continue
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "hidden": child.name.startswith("."),
                "type": "directory" if is_dir else "file",
            }
        )

    if root_path:
        roots = [{"label": "Workspace", "path": str(root_path)}]
    else:
        home = Path.home()
        roots = [
            {"label": "Current", "path": str(Path.cwd())},
            {"label": "Home", "path": str(home)},
        ]
        if home != Path("/root") and Path("/root").exists():
            roots.append({"label": "Root Home", "path": "/root"})

    return {
        "path": str(current),
        "parent": parent_path(current, root_path),
        "entries": entries,
        "roots": roots,
    }


def create_directory_payload(path: str, name: str, root: str | None = None) -> dict[str, object]:
    root_path = resolve_root(root)
    parent = secure_resolve_path(path or str(root_path or Path.cwd()), root_path)
    if not parent.exists() or not parent.is_dir():
        raise ValueError(f"Parent directory does not exist: {path}")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Folder name is required")
    if clean_name in {".", ".."} or "/" in clean_name or "\\" in clean_name:
        raise ValueError("Folder name cannot contain path separators")
    target = (parent / clean_name).resolve()
    if target.parent != parent:
        raise ValueError("Folder must be created under the current directory")
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise ValueError(f"Path already exists: {target}") from exc
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    return {"status": "created", "path": str(target), "name": target.name}


def file_preview_payload(
    path: str,
    max_bytes: int = 256 * 1024,
    root: str | None = None,
) -> dict[str, object]:
    root_path = resolve_root(root)
    file_path = secure_resolve_path(path, root_path)
    if not file_path.exists():
        raise ValueError(f"Path does not exist: {path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {path}")
    if is_sensitive_path(file_path):
        raise ValueError(f"Preview is blocked for sensitive path: {file_path.name}")
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    if size > max_bytes:
        return {
            "path": str(file_path),
            "name": file_path.name,
            "size": size,
            "text": "",
            "truncated": True,
            "binary": False,
            "error": f"File is larger than {max_bytes // 1024} KiB.",
        }
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    if b"\x00" in raw:
        return {
            "path": str(file_path),
            "name": file_path.name,
            "size": size,
            "text": "",
            "truncated": False,
            "binary": True,
            "error": "Binary file preview is not supported.",
        }
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return {
        "path": str(file_path),
        "name": file_path.name,
        "size": size,
        "text": text,
        "truncated": False,
        "binary": False,
        "error": "",
    }


def raw_file_metadata(
    path: str,
    max_image_bytes: int = 5 * 1024 * 1024,
    max_pdf_bytes: int = 20 * 1024 * 1024,
    root: str | None = None,
) -> tuple[Path, str, int]:
    root_path = resolve_root(root)
    file_path = secure_resolve_path(path, root_path)
    if not file_path.exists():
        raise ValueError(f"Path does not exist: {path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {path}")
    if is_sensitive_path(file_path):
        raise ValueError(f"Raw file access is blocked for sensitive path: {file_path.name}")
    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    is_image = media_type.startswith("image/")
    is_pdf = media_type == "application/pdf"
    if not is_image and not is_pdf:
        raise ValueError("Raw file access is only supported for images and PDFs.")
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    max_bytes = max_pdf_bytes if is_pdf else max_image_bytes
    if size > max_bytes:
        kind = "PDF" if is_pdf else "Image"
        raise ValueError(f"{kind} is larger than {max_bytes // 1024 // 1024} MiB.")
    return file_path, media_type, size


def file_raw_info_payload(path: str, root: str | None = None) -> dict[str, object]:
    file_path, media_type, size = raw_file_metadata(path, root=root)
    return {
        "path": str(file_path),
        "name": file_path.name,
        "size": size,
        "media_type": media_type,
    }


def file_raw_payload(path: str, root: str | None = None) -> tuple[bytes, str]:
    file_path, media_type, _size = raw_file_metadata(path, root=root)
    try:
        return file_path.read_bytes(), media_type
    except OSError as exc:
        raise ValueError(str(exc)) from exc


def resolve_root(root: str | None) -> Path | None:
    if not root:
        return None
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists() or not root_path.is_dir():
        raise ValueError(f"Workspace root does not exist: {root}")
    return root_path


def secure_resolve_path(path: str, root: Path | None) -> Path:
    resolved = Path(path).expanduser().resolve()
    if root and resolved != root and root not in resolved.parents:
        raise ValueError(f"Path is outside workspace: {path}")
    return resolved


def parent_path(path: Path, root: Path | None) -> str:
    if root and path == root:
        return ""
    parent = path.parent
    if root and parent != root and root not in parent.parents:
        return str(root)
    return str(parent) if parent != path else ""


def is_sensitive_path(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SENSITIVE_PATH_PARTS:
        return True
    name = path.name.lower()
    if name in SENSITIVE_FILE_NAMES:
        return True
    return name.endswith((".pem", ".key")) or name.endswith(".env")
