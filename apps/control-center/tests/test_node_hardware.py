from types import SimpleNamespace

from flask import Flask

import node_hardware_extension
from node_hardware_extension import init_node_hardware_center
from rbac_extension import init_rbac
from services.node_hardware_service import node_inventory

VIEWER_HEADERS = {"Cf-Access-Authenticated-User-Email": "viewer@example.com"}


class FakeDockerClient:
    def ping(self):
        return True

    def info(self):
        return {
            "OperatingSystem": "Raspberry Pi OS",
            "Architecture": "aarch64",
            "NCPU": 4,
            "MemTotal": 8_000_000_000,
            "Containers": 7,
        }

    def version(self):
        return {"Version": "27.1.0"}


def fake_reader(path):
    values = {
        "/proc/device-tree/model": "Raspberry Pi 5 Model B Rev 1.0\x00",
        "/sys/class/thermal/thermal_zone0/temp": "62000",
    }
    if str(path) not in values:
        raise OSError
    return values[str(path)]


def fake_runner(*args, **kwargs):
    return SimpleNamespace(returncode=0, stdout="throttled=0x0\n", stderr="")


def make_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, RBAC_VIEWER_EMAILS={"viewer@example.com"})
    init_rbac(app)
    init_node_hardware_center(app)
    return app


def test_inventory_reads_pi_hardware_without_writes(monkeypatch):
    monkeypatch.setattr("services.node_hardware_service._storage", lambda path="/": {
        "path": str(path),
        "total_bytes": 100,
        "used_bytes": 20,
        "free_bytes": 80,
        "used_percent": 20.0,
    })
    inventory = node_inventory(
        reader=fake_reader,
        runner=fake_runner,
        docker_factory=lambda: FakeDockerClient(),
    )
    assert inventory["model"] == "Raspberry Pi 5 Model B Rev 1.0"
    assert inventory["temperature_c"] == 62.0
    assert inventory["throttling"]["flags"] == []
    assert inventory["docker"]["available"] is True
    assert inventory["healthy"] is True


def test_inventory_flags_temperature_and_throttling(monkeypatch):
    def hot_reader(path):
        if str(path) == "/sys/class/thermal/thermal_zone0/temp":
            return "81000"
        raise OSError

    def throttled(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="throttled=0x50005", stderr="")

    monkeypatch.setattr("services.node_hardware_service._storage", lambda path="/": {
        "path": str(path),
        "total_bytes": 100,
        "used_bytes": 90,
        "free_bytes": 10,
        "used_percent": 90.0,
    })
    inventory = node_inventory(reader=hot_reader, runner=throttled, docker_factory=lambda: FakeDockerClient())
    assert "high_temperature" in inventory["warnings"]
    assert "power_or_throttling_event" in inventory["warnings"]
    assert "storage_pressure" in inventory["warnings"]
    assert inventory["healthy"] is False


def test_api_requires_read_permission_and_disables_cache(monkeypatch):
    app = make_app()
    client = app.test_client()
    assert client.get("/api/node-hardware").status_code == 403
    monkeypatch.setattr(
        node_hardware_extension,
        "node_inventory",
        lambda: {"hostname": "pi", "healthy": True, "warnings": []},
    )
    response = client.get("/api/node-hardware", headers=VIEWER_HEADERS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["read_only"] is True
