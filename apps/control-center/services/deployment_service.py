from datetime import datetime, timedelta, timezone


def utc_now():
    return datetime.now(timezone.utc)


def _clean(value, limit=500):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def normalize_deployment(container):
    return {
        "container_name": _clean(container.get("name"), 255),
        "compose_project": _clean(container.get("compose_project"), 255),
        "compose_service": _clean(container.get("compose_service"), 255),
        "image_reference": _clean(container.get("image"), 500),
        "image_id": _clean(container.get("image_id"), 255),
        "image_digest": _clean(container.get("image_digest"), 500),
        "container_id": _clean(container.get("container_id"), 255),
        "container_created_at": _clean(container.get("created_at"), 100),
        "status": _clean(container.get("status"), 50),
        "health": _clean(container.get("healthy"), 50),
    }


def _insert_history(
    connection,
    deployment,
    change_type,
    timestamp,
    previous=None,
):
    previous = previous or {}
    connection.execute(
        """INSERT INTO deployment_history (
            recorded_at,
            container_name,
            compose_project,
            compose_service,
            change_type,
            previous_image_id,
            image_id,
            previous_container_id,
            container_id,
            image_reference,
            status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            timestamp,
            deployment["container_name"],
            deployment["compose_project"],
            deployment["compose_service"],
            change_type,
            previous.get("image_id"),
            deployment["image_id"],
            previous.get("container_id"),
            deployment["container_id"],
            deployment["image_reference"],
            deployment["status"],
        ),
    )


def _change_type(previous, deployment):
    if not previous["present"]:
        return "restored"
    if previous["image_id"] != deployment["image_id"]:
        return "image_changed"
    if previous["container_id"] != deployment["container_id"]:
        return "container_recreated"
    if previous["image_reference"] != deployment["image_reference"]:
        return "image_reference_changed"
    return None


def sync_deployment_inventory(containers, database_factory, *, recorded_at=None):
    timestamp = (recorded_at or utc_now()).isoformat()
    deployments = [normalize_deployment(container) for container in containers]
    deployments = [item for item in deployments if item["container_name"]]
    changes = []

    with database_factory() as connection:
        existing = {
            row["container_name"]: dict(row)
            for row in connection.execute("SELECT * FROM deployments").fetchall()
        }
        seen = {item["container_name"] for item in deployments}

        for name, previous in existing.items():
            if name in seen or not previous["present"]:
                continue
            connection.execute(
                """UPDATE deployments
                   SET present = 0, missing_since = ?, last_change_at = ?
                   WHERE container_name = ?""",
                (timestamp, timestamp, name),
            )
            missing = {
                "container_name": name,
                "compose_project": previous["compose_project"],
                "compose_service": previous["compose_service"],
                "image_reference": previous["image_reference"],
                "image_id": previous["image_id"],
                "container_id": previous["container_id"],
                "status": "missing",
            }
            _insert_history(connection, missing, "missing", timestamp, previous)
            changes.append({**missing, "change_type": "missing"})

        for deployment in deployments:
            name = deployment["container_name"]
            previous = existing.get(name)
            if previous is None:
                connection.execute(
                    """INSERT INTO deployments (
                        container_name,
                        compose_project,
                        compose_service,
                        image_reference,
                        image_id,
                        image_digest,
                        container_id,
                        container_created_at,
                        first_seen_at,
                        last_seen_at,
                        last_change_at,
                        status,
                        health,
                        present,
                        missing_since
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)""",
                    (
                        name,
                        deployment["compose_project"],
                        deployment["compose_service"],
                        deployment["image_reference"],
                        deployment["image_id"],
                        deployment["image_digest"],
                        deployment["container_id"],
                        deployment["container_created_at"],
                        timestamp,
                        timestamp,
                        timestamp,
                        deployment["status"],
                        deployment["health"],
                    ),
                )
                _insert_history(connection, deployment, "discovered", timestamp)
                changes.append({**deployment, "change_type": "discovered"})
                continue

            change_type = _change_type(previous, deployment)
            last_change_at = timestamp if change_type else previous["last_change_at"]
            connection.execute(
                """UPDATE deployments SET
                    compose_project = ?,
                    compose_service = ?,
                    image_reference = ?,
                    image_id = ?,
                    image_digest = ?,
                    container_id = ?,
                    container_created_at = ?,
                    last_seen_at = ?,
                    last_change_at = ?,
                    status = ?,
                    health = ?,
                    present = 1,
                    missing_since = NULL
                   WHERE container_name = ?""",
                (
                    deployment["compose_project"],
                    deployment["compose_service"],
                    deployment["image_reference"],
                    deployment["image_id"],
                    deployment["image_digest"],
                    deployment["container_id"],
                    deployment["container_created_at"],
                    timestamp,
                    last_change_at,
                    deployment["status"],
                    deployment["health"],
                    name,
                ),
            )
            if change_type:
                _insert_history(
                    connection,
                    deployment,
                    change_type,
                    timestamp,
                    previous,
                )
                changes.append({**deployment, "change_type": change_type})

    return changes


def list_deployments(database_factory, *, include_missing=True):
    where = "" if include_missing else "WHERE present = 1"
    with database_factory() as connection:
        rows = connection.execute(
            f"""SELECT * FROM deployments {where}
                ORDER BY present DESC,
                         COALESCE(compose_project, ''),
                         COALESCE(compose_service, ''),
                         container_name"""
        ).fetchall()
    return [dict(row) for row in rows]


def list_deployment_history(limit, database_factory):
    with database_factory() as connection:
        rows = connection.execute(
            "SELECT * FROM deployment_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def deployment_center_status(database_factory, *, checked_at=None):
    checked_at = checked_at or utc_now()
    changed_since = (checked_at - timedelta(hours=24)).isoformat()
    with database_factory() as connection:
        counts = connection.execute(
            """SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN present = 1 THEN 1 ELSE 0 END) AS present,
                SUM(CASE WHEN present = 0 THEN 1 ELSE 0 END) AS missing,
                SUM(CASE WHEN present = 1 AND status = 'running' THEN 1 ELSE 0 END) AS running,
                SUM(CASE WHEN present = 1 AND health = 'unhealthy' THEN 1 ELSE 0 END) AS unhealthy
               FROM deployments"""
        ).fetchone()
        projects = [
            row["compose_project"]
            for row in connection.execute(
                """SELECT DISTINCT compose_project FROM deployments
                   WHERE present = 1 AND compose_project IS NOT NULL
                   ORDER BY compose_project"""
            ).fetchall()
        ]
        changed_last_24h = connection.execute(
            """SELECT COUNT(*) FROM deployment_history
               WHERE recorded_at >= ? AND change_type != 'discovered'""",
            (changed_since,),
        ).fetchone()[0]
        latest = connection.execute(
            "SELECT * FROM deployment_history ORDER BY id DESC LIMIT 1"
        ).fetchone()

    return {
        "mode": "inventory",
        "total": counts["total"] or 0,
        "present": counts["present"] or 0,
        "missing": counts["missing"] or 0,
        "running": counts["running"] or 0,
        "unhealthy": counts["unhealthy"] or 0,
        "compose_projects": projects,
        "changed_last_24h": changed_last_24h,
        "latest": dict(latest) if latest else None,
    }
