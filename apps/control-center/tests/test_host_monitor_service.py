import json

from services.host_monitor_service import read_monitor_state


def test_read_monitor_state_normalizes_fresh_snapshot(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "checked_at": 1_000,
                "failures": 0,
                "alerted": False,
                "last_status": {
                    "hostname": "racher-pi",
                    "status": "ok",
                    "temperature_c": 43,
                    "disk_percent": 8,
                    "memory_percent": 25,
                    "load1": 0.25,
                    "docker": {"running": 14, "total": 14},
                    "issues": [],
                },
            }
        ),
        encoding="utf-8",
    )

    result = read_monitor_state(state_file, stale_after_seconds=900, now=1_200)

    assert result["available"] is True
    assert result["stale"] is False
    assert result["age_seconds"] == 200
    assert result["monitor_status"] == "ok"
    assert result["snapshot"]["docker"]["running"] == 14


def test_read_monitor_state_marks_old_data_stale(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "checked_at": 100,
                "last_status": {"status": "ok", "issues": []},
            }
        ),
        encoding="utf-8",
    )

    result = read_monitor_state(state_file, stale_after_seconds=300, now=1_000)

    assert result["available"] is True
    assert result["stale"] is True
    assert result["age_seconds"] == 900


def test_read_monitor_state_keeps_backwards_compatible_local_state(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "status": "error",
                "checked_at": 900,
                "issues": ["Container vagtbytte-web har status exited"],
            }
        ),
        encoding="utf-8",
    )

    result = read_monitor_state(state_file, stale_after_seconds=900, now=1_000)

    assert result["available"] is True
    assert result["monitor_status"] == "error"
    assert result["issues"] == ["Container vagtbytte-web har status exited"]


def test_read_monitor_state_fails_closed_on_invalid_json(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("not json", encoding="utf-8")

    result = read_monitor_state(state_file, now=1_000)

    assert result["available"] is False
    assert result["stale"] is True
    assert result["monitor_status"] == "unknown"
    assert result["error"]
