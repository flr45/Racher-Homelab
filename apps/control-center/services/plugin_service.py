import json
import re
from pathlib import Path

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$")
_ALLOWED_PERMISSIONS = {
    "audit.read",
    "containers.read",
    "database.read",
    "events.read",
    "files.read",
    "metrics.read",
    "notifications.read",
}
_MAX_MANIFEST_BYTES = 64 * 1024


class PluginManifestError(ValueError):
    pass


def _version_tuple(value):
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(value or ""))
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def _safe_text(value, field, limit):
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise PluginManifestError(f"Ugyldigt felt: {field}")
    return text


def validate_manifest(payload, *, platform_version="1.0.0"):
    if not isinstance(payload, dict):
        raise PluginManifestError("Manifestet skal være et JSON-objekt.")

    plugin_id = _safe_text(payload.get("id"), "id", 64)
    version = _safe_text(payload.get("version"), "version", 64)
    if not _PLUGIN_ID.fullmatch(plugin_id):
        raise PluginManifestError("Ugyldigt plugin-id.")
    if not _VERSION.fullmatch(version):
        raise PluginManifestError("Ugyldig plugin-version.")

    permissions = payload.get("permissions", [])
    if not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions):
        raise PluginManifestError("Permissions skal være en liste af tekstværdier.")
    normalized_permissions = sorted(set(permissions))
    unknown = sorted(set(normalized_permissions) - _ALLOWED_PERMISSIONS)
    if unknown:
        raise PluginManifestError("Ukendte permissions: " + ", ".join(unknown))

    minimum_version = str(payload.get("minimum_racher_os", "1.0.0")).strip()
    if not _VERSION.fullmatch(minimum_version):
        raise PluginManifestError("Ugyldig minimum_racher_os-version.")

    return {
        "id": plugin_id,
        "name": _safe_text(payload.get("name"), "name", 100),
        "version": version,
        "description": str(payload.get("description", "")).strip()[:500],
        "author": str(payload.get("author", "")).strip()[:100],
        "homepage": str(payload.get("homepage", "")).strip()[:300],
        "permissions": normalized_permissions,
        "minimum_racher_os": minimum_version,
        "compatible": _version_tuple(platform_version) >= _version_tuple(minimum_version),
        "execution_enabled": False,
    }


def discover_plugins(root, *, platform_version="1.0.0"):
    root = Path(root).expanduser().resolve()
    if not root.exists():
        return {"root_configured": True, "plugins": [], "invalid": [], "execution_enabled": False}
    if not root.is_dir():
        raise PluginManifestError("Plugin-stien er ikke en mappe.")

    plugins = []
    invalid = []
    seen = set()
    for manifest_path in sorted(root.glob("*/plugin.json")):
        try:
            if manifest_path.is_symlink() or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
                raise PluginManifestError("Manifestet er ikke tilladt.")
            manifest_path.resolve().relative_to(root)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            plugin = validate_manifest(payload, platform_version=platform_version)
            if plugin["id"] in seen:
                raise PluginManifestError("Duplikeret plugin-id.")
            seen.add(plugin["id"])
            plugin["directory"] = manifest_path.parent.name
            plugins.append(plugin)
        except (OSError, UnicodeError, json.JSONDecodeError, PluginManifestError, ValueError) as exc:
            invalid.append({"directory": manifest_path.parent.name, "error": str(exc)[:200]})

    return {
        "root_configured": True,
        "plugins": plugins,
        "invalid": invalid,
        "count": len(plugins),
        "compatible": sum(1 for plugin in plugins if plugin["compatible"]),
        "execution_enabled": False,
        "allowed_permissions": sorted(_ALLOWED_PERMISSIONS),
    }
