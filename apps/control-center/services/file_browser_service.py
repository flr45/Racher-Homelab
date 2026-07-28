import mimetypes
from datetime import datetime, timezone
from pathlib import Path


class FileBrowserError(Exception):
    """Base error for safe file browsing."""


class FileNotAllowedError(FileBrowserError):
    """Raised when a requested path is outside the configured roots."""


class FileTooLargeError(FileBrowserError):
    """Raised when a requested file exceeds the download limit."""


_BLOCKED_NAMES = {
    ".env",
    ".git",
    ".ssh",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "secrets",
}
_BLOCKED_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


def configured_roots(paths):
    roots = []
    for raw_path in paths:
        root = Path(raw_path).expanduser().resolve()
        if root.exists() and root.is_dir():
            roots.append(root)
    return roots


def _is_blocked(path):
    lowered = path.name.lower()
    return lowered in _BLOCKED_NAMES or path.suffix.lower() in _BLOCKED_SUFFIXES


def resolve_path(root, relative_path=""):
    root = Path(root).resolve()
    relative = Path(str(relative_path or ""))
    if relative.is_absolute():
        raise FileNotAllowedError("Stien er ikke tilladt.")

    current = root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise FileNotAllowedError("Stien er ikke tilladt.")
        current = current / part
        if current.is_symlink():
            raise FileNotAllowedError("Symbolske links er ikke tilladt.")
        if _is_blocked(current) or current.name.startswith("."):
            raise FileNotAllowedError("Filen er beskyttet.")

    candidate = current.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FileNotAllowedError("Stien er ikke tilladt.") from exc
    return candidate


def _entry(path, root):
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path.relative_to(root)),
        "type": "directory" if path.is_dir() else "file",
        "size_bytes": None if path.is_dir() else stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "mime_type": None if path.is_dir() else mimetypes.guess_type(path.name)[0],
    }


def list_directory(root, relative_path="", *, limit=500):
    root = Path(root).resolve()
    directory = resolve_path(root, relative_path)
    if not directory.exists():
        raise FileNotFoundError(relative_path)
    if not directory.is_dir():
        raise NotADirectoryError(relative_path)

    entries = []
    for child in directory.iterdir():
        if child.is_symlink() or _is_blocked(child) or child.name.startswith("."):
            continue
        entries.append(_entry(child, root))
    entries.sort(
        key=lambda item: (item["type"] != "directory", item["name"].casefold())
    )
    bounded_limit = max(1, min(int(limit), 500))
    return {
        "root": root.name,
        "path": str(directory.relative_to(root)),
        "entries": entries[:bounded_limit],
        "count": min(len(entries), bounded_limit),
        "truncated": len(entries) > bounded_limit,
        "read_only": True,
    }


def downloadable_file(root, relative_path, *, max_bytes):
    path = resolve_path(root, relative_path)
    if not path.exists():
        raise FileNotFoundError(relative_path)
    if not path.is_file():
        raise FileNotAllowedError("Kun almindelige filer kan downloades.")
    size = path.stat().st_size
    if size > int(max_bytes):
        raise FileTooLargeError("Filen er for stor til download.")
    return path
