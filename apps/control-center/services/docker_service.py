import docker
from flask import current_app


class ContainerNotFoundError(Exception):
    """Raised when Docker cannot find the requested container."""


def docker_client():
    return docker.from_env()


def container_usage(container):
    try:
        stats = container.stats(stream=False)
        cpu_stats = stats.get("cpu_stats", {})
        pre_cpu = stats.get("precpu_stats", {})
        cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - pre_cpu.get(
            "cpu_usage", {}
        ).get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - pre_cpu.get(
            "system_cpu_usage", 0
        )
        cpu_count = len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])) or 1
        cpu_percent = (
            cpu_delta / system_delta * cpu_count * 100
            if system_delta > 0 and cpu_delta >= 0
            else 0
        )
        memory = stats.get("memory_stats", {}).get("usage", 0)
        cache = stats.get("memory_stats", {}).get("stats", {}).get("cache", 0)
        return {
            "cpu": round(cpu_percent, 1),
            "memory_mb": round(max(memory - cache, 0) / 1024 / 1024, 1),
        }
    except Exception:
        return {"cpu": None, "memory_mb": None}


def _container_image_metadata(container):
    image = container.image
    tags = getattr(image, "tags", None) or []
    image_id = getattr(image, "id", None) or getattr(image, "short_id", None)
    image_attrs = getattr(image, "attrs", None) or {}
    repo_digests = image_attrs.get("RepoDigests") or []
    return {
        "image": tags[0] if tags else getattr(image, "short_id", image_id),
        "image_id": image_id,
        "image_digest": repo_digests[0] if repo_digests else None,
    }


def docker_status(include_usage=True):
    try:
        containers = []
        protected_containers = current_app.config["PROTECTED_CONTAINERS"]
        for container in docker_client().containers.list(all=True):
            attrs = container.attrs or {}
            state = attrs.get("State", {})
            labels = getattr(container, "labels", None) or attrs.get("Config", {}).get(
                "Labels", {}
            )
            usage = (
                container_usage(container)
                if include_usage and container.status == "running"
                else {"cpu": None, "memory_mb": None}
            )
            containers.append(
                {
                    "name": container.name,
                    "container_id": getattr(container, "id", None),
                    "status": container.status,
                    "healthy": state.get("Health", {}).get("Status"),
                    "started_at": state.get("StartedAt"),
                    "created_at": attrs.get("Created"),
                    "compose_project": labels.get("com.docker.compose.project"),
                    "compose_service": labels.get("com.docker.compose.service"),
                    "protected": container.name in protected_containers,
                    **_container_image_metadata(container),
                    **usage,
                }
            )
        containers.sort(key=lambda item: item["name"])
        return containers, None
    except Exception as exc:
        return [], str(exc)


def app_status(containers):
    states = {container["name"]: container for container in containers}
    return [
        {
            **item,
            "container": states.get(item["service"]),
            "status": states.get(item["service"], {}).get("status", "not-found"),
        }
        for item in current_app.config["APP_LINKS"]
    ]


def domain_status(containers):
    states = {container["name"]: container["status"] for container in containers}
    return [
        {
            **domain,
            "status": states.get(domain["service"], "not-found"),
            "url": f"https://{domain['host']}",
        }
        for domain in current_app.config["DOMAIN_LINKS"]
    ]


def container_logs(container_name, tail):
    try:
        container = docker_client().containers.get(container_name)
    except docker.errors.NotFound as exc:
        raise ContainerNotFoundError(container_name) from exc
    logs = container.logs(tail=tail, timestamps=True).decode(
        "utf-8", errors="replace"
    )
    return container.name, logs


def perform_container_action(container_name, action):
    try:
        container = docker_client().containers.get(container_name)
    except docker.errors.NotFound as exc:
        raise ContainerNotFoundError(container_name) from exc
    if action in {"stop", "restart"}:
        getattr(container, action)(timeout=20)
    else:
        container.start()
