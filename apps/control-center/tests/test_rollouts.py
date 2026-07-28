import sys
from pathlib import Path

import pytest

CONTROL_CENTER_ROOT = Path(__file__).resolve().parents[1]
if str(CONTROL_CENTER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_CENTER_ROOT))

from services.database_service import open_database  # noqa: E402
from services.deployment_service import sync_deployment_inventory  # noqa: E402
from services.rollout_service import (  # noqa: E402
    RolloutError,
    create_rollout,
    execute_rollout,
    validate_image_reference,
)


def database_factory(tmp_path):
    root = tmp_path / "data"
    return lambda: open_database(root, root / "racher-os.db")


def seed(factory):
    sync_deployment_inventory(
        [
            {
                "name": "api",
                "image": "ghcr.io/example/api:1.0",
                "image_id": "sha256:v1",
                "container_id": "container-v1",
                "status": "running",
                "healthy": "healthy",
            }
        ],
        factory,
    )


def test_image_validation_rejects_latest_and_shell_tokens():
    assert validate_image_reference("ghcr.io/example/api:2.0") == "ghcr.io/example/api:2.0"
    with pytest.raises(RolloutError):
        validate_image_reference("ghcr.io/example/api:latest")
    with pytest.raises(RolloutError):
        validate_image_reference("image:2.0;rm")


def test_rollout_succeeds_and_records_previous_image(tmp_path):
    factory = database_factory(tmp_path)
    seed(factory)
    rollout = create_rollout("api", "ghcr.io/example/api:2.0", "admin@example.dk", factory)
    calls = []

    result = execute_rollout(
        rollout["id"],
        factory,
        pull_image=lambda image: calls.append(("pull", image)),
        replace_container=lambda name, image: calls.append(("replace", name, image)),
        inspect_container=lambda name: {"status": "running", "health": "healthy"},
        cleanup_backups=lambda name: calls.append(("cleanup", name)),
        timeout_seconds=10,
    )

    assert result["status"] == "succeeded"
    assert result["previous_image"] == "ghcr.io/example/api:1.0"
    assert ("replace", "api", "ghcr.io/example/api:2.0") in calls
    assert calls[-1] == ("cleanup", "api")


def test_failed_healthcheck_rolls_back(tmp_path, monkeypatch):
    factory = database_factory(tmp_path)
    seed(factory)
    rollout = create_rollout("api", "ghcr.io/example/api:2.0", "admin", factory)
    replacements = []
    states = iter(
        [
            {"status": "exited", "health": "unhealthy"},
            {"status": "running", "health": "healthy"},
        ]
    )
    monkeypatch.setattr("services.rollout_service.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("services.rollout_service.time.monotonic", iter([0, 0, 11, 11, 11]).__next__)

    result = execute_rollout(
        rollout["id"],
        factory,
        pull_image=lambda image: None,
        replace_container=lambda name, image: replacements.append((name, image)),
        inspect_container=lambda name: next(states),
        timeout_seconds=10,
    )

    assert result["status"] == "rolled_back"
    assert replacements == [
        ("api", "ghcr.io/example/api:2.0"),
        ("api", "ghcr.io/example/api:1.0"),
    ]


def test_only_one_active_rollout_per_container(tmp_path):
    factory = database_factory(tmp_path)
    seed(factory)
    create_rollout("api", "ghcr.io/example/api:2.0", "admin", factory)
    with pytest.raises(RolloutError):
        create_rollout("api", "ghcr.io/example/api:3.0", "admin", factory)


def test_rollout_schema_and_indexes(tmp_path):
    factory = database_factory(tmp_path)
    with factory() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "rollout_jobs" in tables
    assert "idx_rollout_jobs_container_status" in indexes
    assert "idx_rollout_jobs_created_at" in indexes
