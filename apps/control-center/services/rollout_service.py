import re
import time
from datetime import datetime, timezone

from services.deployment_service import list_deployments

IMAGE_PATTERN = re.compile(r"^[a-zA-Z0-9._/-]+(?::[a-zA-Z0-9._-]+|@sha256:[a-fA-F0-9]{64})$")


class RolloutError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def validate_image_reference(image_reference):
    image_reference = str(image_reference or "").strip()
    if not image_reference or len(image_reference) > 500:
        raise RolloutError("Image-reference mangler eller er for lang.")
    if not IMAGE_PATTERN.fullmatch(image_reference):
        raise RolloutError("Image-reference har ugyldigt format.")
    if image_reference.endswith(":latest"):
        raise RolloutError("Tagget latest er ikke tilladt til kontrollerede deployments.")
    return image_reference


def deployment_by_name(container_name, database_factory):
    for deployment in list_deployments(database_factory, include_missing=False):
        if deployment["container_name"] == container_name:
            return deployment
    raise RolloutError("Deployment blev ikke fundet i inventory.")


def create_rollout(container_name, target_image, actor, database_factory, *, created_at=None):
    target_image = validate_image_reference(target_image)
    current = deployment_by_name(container_name, database_factory)
    if current["image_reference"] == target_image:
        raise RolloutError("Target-image er allerede aktivt.")
    timestamp = (created_at or utc_now()).isoformat()
    with database_factory() as connection:
        active = connection.execute(
            "SELECT id FROM rollout_jobs WHERE container_name = ? AND status NOT IN ('succeeded','rolled_back','failed')",
            (container_name,),
        ).fetchone()
        if active:
            raise RolloutError("Der findes allerede en aktiv rollout for containeren.")
        cursor = connection.execute(
            """INSERT INTO rollout_jobs (
                created_at, updated_at, actor, container_name, previous_image,
                target_image, status, phase, automatic_rollback, message
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 'preflight', 1, ?)""",
            (
                timestamp,
                timestamp,
                str(actor or "unknown")[:255],
                container_name,
                current["image_reference"],
                target_image,
                "Afventer eksekvering",
            ),
        )
        rollout_id = cursor.lastrowid
    return get_rollout(rollout_id, database_factory)


def get_rollout(rollout_id, database_factory):
    with database_factory() as connection:
        row = connection.execute(
            "SELECT * FROM rollout_jobs WHERE id = ?", (rollout_id,)
        ).fetchone()
    if not row:
        raise RolloutError("Rollout blev ikke fundet.")
    return dict(row)


def list_rollouts(limit, database_factory):
    with database_factory() as connection:
        rows = connection.execute(
            "SELECT * FROM rollout_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def update_rollout(rollout_id, database_factory, *, status=None, phase=None, message=None):
    updates = ["updated_at = ?"]
    values = [utc_now().isoformat()]
    for field, value in (("status", status), ("phase", phase), ("message", message)):
        if value is not None:
            updates.append(f"{field} = ?")
            values.append(str(value)[:1000])
    values.append(rollout_id)
    with database_factory() as connection:
        connection.execute(
            f"UPDATE rollout_jobs SET {', '.join(updates)} WHERE id = ?", values
        )


def wait_for_healthy(inspect_container, container_name, timeout_seconds, poll_seconds=1):
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        last = inspect_container(container_name)
        if last.get("status") == "running" and last.get("health") in {None, "healthy"}:
            return last
        time.sleep(poll_seconds)
    raise RolloutError(f"Healthcheck timeout: {last}")


def execute_rollout(
    rollout_id,
    database_factory,
    *,
    pull_image,
    replace_container,
    inspect_container,
    cleanup_backups=lambda _container_name: None,
    timeout_seconds=120,
):
    rollout = get_rollout(rollout_id, database_factory)
    if rollout["status"] != "pending":
        raise RolloutError("Kun pending rollouts kan eksekveres.")

    try:
        update_rollout(rollout_id, database_factory, status="running", phase="pull", message="Henter target-image")
        pull_image(rollout["target_image"])
        update_rollout(rollout_id, database_factory, phase="replace", message="Udskifter container")
        replace_container(rollout["container_name"], rollout["target_image"])
        update_rollout(rollout_id, database_factory, phase="healthcheck", message="Afventer healthcheck")
        wait_for_healthy(inspect_container, rollout["container_name"], timeout_seconds)
        cleanup_backups(rollout["container_name"])
        update_rollout(rollout_id, database_factory, status="succeeded", phase="complete", message="Deployment gennemført")
        return get_rollout(rollout_id, database_factory)
    except Exception as deployment_error:
        update_rollout(rollout_id, database_factory, status="rolling_back", phase="rollback", message=str(deployment_error))
        try:
            pull_image(rollout["previous_image"])
            replace_container(rollout["container_name"], rollout["previous_image"])
            wait_for_healthy(inspect_container, rollout["container_name"], timeout_seconds)
            cleanup_backups(rollout["container_name"])
            update_rollout(rollout_id, database_factory, status="rolled_back", phase="complete", message=f"Automatisk rollback: {deployment_error}")
        except Exception as rollback_error:
            update_rollout(rollout_id, database_factory, status="failed", phase="rollback_failed", message=f"Deployment: {deployment_error}; rollback: {rollback_error}")
        return get_rollout(rollout_id, database_factory)
