from flask import Flask

import operations_status_extension


def make_context():
    app = Flask(__name__)
    app.config.update(TESTING=True, HOST_MONITOR_STALE_SECONDS=900)
    return app.app_context()


def test_host_card_is_healthy_for_fresh_ok_snapshot():
    monitor = {
        "available": True,
        "stale": False,
        "age_seconds": 60,
        "checked_at": 1_000,
        "monitor_status": "ok",
        "alerted": False,
        "snapshot": {
            "temperature_c": 43,
            "disk_percent": 8,
            "memory_percent": 25,
            "load1": 0.25,
            "uptime_seconds": 86_400,
            "docker": {"running": 14, "total": 14},
        },
        "issues": [],
        "error": None,
    }

    card = operations_status_extension._host_card("pi", "PI · racher-pi", monitor)

    assert card["state"] == "healthy"
    values = {metric["label"]: metric["value"] for metric in card["metrics"]}
    assert values["Temperatur"] == "43 °C"
    assert values["Docker"] == "14/14"
    assert values["Oppetid"] == "1d 0t"


def test_host_card_marks_offline_snapshot_critical():
    monitor = {
        "available": True,
        "stale": False,
        "age_seconds": 10,
        "checked_at": 1_000,
        "monitor_status": "offline",
        "alerted": True,
        "snapshot": {"status": "offline", "issues": ["SSH utilgængelig"]},
        "issues": ["SSH utilgængelig"],
        "error": None,
    }

    card = operations_status_extension._host_card("mini", "MINI · racherserver", monitor)

    assert card["state"] == "critical"
    assert "SSH utilgængelig" in card["details"]


def test_host_cards_use_configured_read_only_state_paths(monkeypatch):
    calls = []

    def fake_read(path, stale_after_seconds):
        calls.append((path, stale_after_seconds))
        return {
            "available": False,
            "stale": True,
            "age_seconds": None,
            "checked_at": None,
            "monitor_status": "unknown",
            "alerted": False,
            "snapshot": {},
            "issues": [],
            "error": "missing",
        }

    monkeypatch.setattr(operations_status_extension, "read_monitor_state", fake_read)
    app = Flask(__name__)
    app.config.update(
        LOCAL_MONITOR_STATE_FILE="/readonly/pi.json",
        REMOTE_MONITOR_STATE_FILE="/readonly/mini.json",
        HOST_MONITOR_STALE_SECONDS=600,
    )

    with app.app_context():
        cards = operations_status_extension._host_cards()

    assert [card["id"] for card in cards] == ["host-pi", "host-mini"]
    assert calls == [("/readonly/pi.json", 600), ("/readonly/mini.json", 600)]
