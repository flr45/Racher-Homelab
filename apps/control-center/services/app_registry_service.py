import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

APP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SERVICE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
ENV_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
ALLOWED_CATEGORIES = {"apps", "infrastructure", "emergency", "platform"}
MAX_MANIFESTS = 100
MAX_MANIFEST_BYTES = 32 * 1024


class AppRegistryError(ValueError):
    """Raised when an App Center manifest is invalid."""


def _text(value, field, *, maximum=200):
    value = str(value or "").strip()
    if not value or len(value) > maximum:
        raise AppRegistryError(f"{field} er ugyldigt")
    return value


def _safe_url(value):
    value = str(value or "#").strip()
    if value == "#":
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "#"
    return value


def _validate_manifest(raw, source_name):
    if not isinstance(raw, dict):
        raise AppRegistryError("manifestet skal være et JSON-objekt")

    app_id = _text(raw.get("id"), "id", maximum=63)
    service = _text(raw.get("service"), "service", maximum=128)
    category = _text(raw.get("category", "apps"), "category", maximum=32)
    url_env = str(raw.get("url_env", "")).strip()

    if not APP_ID_PATTERN.fullmatch(app_id):
        raise AppRegistryError("id har ugyldigt format")
    if not SERVICE_PATTERN.fullmatch(service):
        raise AppRegistryError("service har ugyldigt format")
    if category not in ALLOWED_CATEGORIES:
        raise AppRegistryError("category er ikke tilladt")
    if url_env and not ENV_KEY_PATTERN.fullmatch(url_env):
        raise AppRegistryError("url_env har ugyldigt format")

    compose_file = str(raw.get("compose_file", "")).strip()
    if compose_file and (compose_file.startswith("/") or ".." in Path(compose_file).parts):
        raise AppRegistryError("compose_file skal være en relativ sikker sti")

    return {
        "id": app_id,
        "name": _text(raw.get("name"), "name", maximum=80),
        "description": str(raw.get("description", "")).strip()[:240],
        "icon": str(raw.get("icon", "▦")).strip()[:8] or "▦",
        "category": category,
        "service": service,
        "image": str(raw.get("image", "")).strip()[:240],
        "compose_file": compose_file,
        "url_env": url_env,
        "url_default": _safe_url(raw.get("url_default", "#")),
        "backup": bool(raw.get("backup", False)),
        "auto_update": bool(raw.get("auto_update", False)),
        "source": source_name,
    }


def load_app_registry(root):
    root = Path(root)
    apps = []
    errors = []
    seen_ids = set()
    seen_services = set()

    if not root.is_dir() or root.is_symlink():
        return apps, ["App registry-mappen mangler eller er ugyldig."]

    paths = sorted(root.glob("*.json"))
    if len(paths) > MAX_MANIFESTS:
        errors.append(f"App registry indeholder flere end {MAX_MANIFESTS} manifests.")
        paths = paths[:MAX_MANIFESTS]

    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                raise AppRegistryError("symlinks understøttes ikke")
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                raise AppRegistryError("manifestet er for stort")
            raw = json.loads(path.read_text(encoding="utf-8"))
            manifest = _validate_manifest(raw, path.name)
            if manifest["id"] in seen_ids:
                raise AppRegistryError("dublet app-id")
            if manifest["service"] in seen_services:
                raise AppRegistryError("dublet service")
            seen_ids.add(manifest["id"])
            seen_services.add(manifest["service"])
            apps.append(manifest)
        except (OSError, json.JSONDecodeError, AppRegistryError) as exc:
            errors.append(f"{path.name}: {exc}")

    return apps, errors


def resolve_registry_links(registry, legacy_links=None):
    legacy_by_service = {
        item.get("service"): item for item in (legacy_links or []) if item.get("service")
    }
    resolved = []
    for app in registry:
        legacy = legacy_by_service.get(app["service"], {})
        configured_url = os.getenv(app["url_env"], "") if app["url_env"] else ""
        url = _safe_url(configured_url or legacy.get("url") or app["url_default"])
        expected_image = app["image"]
        resolved.append(
            {
                **app,
                "url": url,
                "version": image_version(expected_image) if expected_image else "–",
            }
        )
    return resolved


def image_version(image_reference):
    reference = str(image_reference or "").strip()
    if not reference:
        return "–"
    if "@sha256:" in reference:
        return reference.split("@sha256:", 1)[1][:12]
    final_segment = reference.rsplit("/", 1)[-1]
    if ":" in final_segment:
        return final_segment.rsplit(":", 1)[1]
    return "latest"


def _parse_started_at(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _uptime_seconds(container):
    if not container or container.get("status") != "running":
        return None
    started_at = _parse_started_at(container.get("started_at"))
    if not started_at:
        return None
    return max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))


def build_app_center(apps, containers, registry_errors=None, docker_error=None):
    by_name = {item.get("name"): item for item in containers}
    by_service = {
        item.get("compose_service"): item
        for item in containers
        if item.get("compose_service")
    }
    results = []

    for app in apps:
        container = by_name.get(app["service"]) or by_service.get(app["service"])
        current_image = container.get("image") if container else None
        results.append(
            {
                **app,
                "installed": container is not None,
                "status": container.get("status", "not-installed") if container else "not-installed",
                "health": container.get("healthy") if container else None,
                "current_image": current_image,
                "version": image_version(current_image or app.get("image")),
                "cpu": container.get("cpu") if container else None,
                "memory_mb": container.get("memory_mb") if container else None,
                "uptime_seconds": _uptime_seconds(container),
                "container_id": container.get("container_id") if container else None,
                "compose_project": container.get("compose_project") if container else None,
            }
        )

    installed = sum(1 for item in results if item["installed"])
    running = sum(1 for item in results if item["status"] == "running")
    unhealthy = sum(1 for item in results if item["health"] == "unhealthy")
    return {
        "apps": results,
        "summary": {
            "registered": len(results),
            "installed": installed,
            "running": running,
            "missing": len(results) - installed,
            "unhealthy": unhealthy,
        },
        "registry_errors": list(registry_errors or []),
        "docker_available": not bool(docker_error),
        "read_only": True,
    }
