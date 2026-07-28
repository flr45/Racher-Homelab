import re

import docker


CONTAINER_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


def _client():
    return docker.from_env()


def _safe_labels(labels):
    labels = labels or {}
    return {
        key: value
        for key, value in labels.items()
        if not any(token in key.lower() for token in ("secret", "token", "password", "credential"))
    }


def docker_center_inventory():
    try:
        client = _client()
        containers = []
        for container in client.containers.list(all=True):
            attrs = container.attrs or {}
            state = attrs.get("State", {})
            containers.append(
                {
                    "name": container.name,
                    "id": getattr(container, "short_id", None),
                    "status": state.get("Status", container.status),
                    "health": state.get("Health", {}).get("Status"),
                    "image": (getattr(container.image, "tags", None) or [getattr(container.image, "short_id", None)])[0],
                    "networks": sorted((attrs.get("NetworkSettings", {}).get("Networks") or {}).keys()),
                    "mount_count": len(attrs.get("Mounts") or []),
                }
            )
        networks = [
            {
                "name": network.name,
                "id": getattr(network, "short_id", None),
                "driver": (network.attrs or {}).get("Driver"),
                "scope": (network.attrs or {}).get("Scope"),
            }
            for network in client.networks.list()
        ]
        volumes = [
            {
                "name": volume.name,
                "driver": (volume.attrs or {}).get("Driver"),
                "scope": (volume.attrs or {}).get("Scope"),
            }
            for volume in client.volumes.list()
        ]
        images = [
            {
                "id": getattr(image, "short_id", None),
                "tags": sorted(getattr(image, "tags", None) or []),
                "size": (image.attrs or {}).get("Size", 0),
                "dangling": not bool(getattr(image, "tags", None)),
            }
            for image in client.images.list(all=True)
        ]
        return {
            "containers": sorted(containers, key=lambda item: item["name"]),
            "networks": sorted(networks, key=lambda item: item["name"]),
            "volumes": sorted(volumes, key=lambda item: item["name"]),
            "images": images,
        }, None
    except Exception:
        return None, "docker_unavailable"


def inspect_container(container_name):
    if not CONTAINER_NAME_PATTERN.fullmatch(str(container_name or "")):
        return None, "not_found"
    try:
        container = _client().containers.get(container_name)
        container.reload()
        attrs = container.attrs or {}
        config = attrs.get("Config", {})
        host = attrs.get("HostConfig", {})
        return {
            "name": container.name,
            "id": getattr(container, "id", None),
            "status": attrs.get("State", {}).get("Status", container.status),
            "health": attrs.get("State", {}).get("Health", {}).get("Status"),
            "image": (getattr(container.image, "tags", None) or [getattr(container.image, "short_id", None)])[0],
            "created": attrs.get("Created"),
            "restart_policy": host.get("RestartPolicy"),
            "read_only_rootfs": bool(host.get("ReadonlyRootfs", False)),
            "privileged": bool(host.get("Privileged", False)),
            "labels": _safe_labels(config.get("Labels")),
            "mounts": [
                {
                    "type": mount.get("Type"),
                    "source": mount.get("Name") or mount.get("Source"),
                    "destination": mount.get("Destination"),
                    "read_only": not mount.get("RW", True),
                }
                for mount in attrs.get("Mounts") or []
            ],
            "networks": sorted((attrs.get("NetworkSettings", {}).get("Networks") or {}).keys()),
            "ports": attrs.get("NetworkSettings", {}).get("Ports") or {},
        }, None
    except docker.errors.NotFound:
        return None, "not_found"
    except Exception:
        return None, "docker_unavailable"


def prune_dangling_images():
    try:
        result = _client().images.prune(filters={"dangling": True}) or {}
        return {
            "deleted": len(result.get("ImagesDeleted") or []),
            "space_reclaimed": int(result.get("SpaceReclaimed") or 0),
        }, None
    except Exception:
        return None, "prune_failed"
