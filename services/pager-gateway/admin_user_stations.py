from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from flask import jsonify, request


class AdminUserStationStore:
    """Admin-only organisational stations for grouping user accounts.

    These tables are deliberately separate from routing.stations/user_stations.
    They never participate in alarm delivery or user-visible station subscriptions.
    """

    def __init__(self, storage):
        self.storage = storage
        self._init_schema()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _normalise_name(value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    def _init_schema(self) -> None:
        with self.storage.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS admin_user_stations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    created_at TEXT NOT NULL,
                    created_by INTEGER,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS admin_user_station_memberships (
                    user_id INTEGER PRIMARY KEY,
                    station_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (station_id) REFERENCES admin_user_stations(id) ON DELETE CASCADE,
                    FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_admin_user_station_memberships_station
                ON admin_user_station_memberships(station_id, user_id);
                """
            )

    def create_station(self, name: Any, created_by: int | None = None) -> dict[str, Any]:
        name = self._normalise_name(name)
        if len(name) < 2:
            raise ValueError("Stationsnavnet skal være mindst 2 tegn.")
        if len(name) > 80:
            raise ValueError("Stationsnavnet må højst være 80 tegn.")

        now = self._now()
        try:
            with self.storage.connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO admin_user_stations(name, created_at, created_by) VALUES (?, ?, ?)",
                    (name, now, created_by),
                )
                station_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Der findes allerede en administrativ station med det navn.") from exc

        return {"id": station_id, "name": name, "created_at": now, "user_count": 0}

    def list_overview(self) -> dict[str, Any]:
        with self.storage.connect() as conn:
            station_rows = conn.execute(
                """SELECT s.id, s.name, s.created_at, COUNT(m.user_id) AS user_count
                   FROM admin_user_stations s
                   LEFT JOIN admin_user_station_memberships m ON m.station_id = s.id
                   GROUP BY s.id
                   ORDER BY s.name COLLATE NOCASE, s.id"""
            ).fetchall()
            assignment_rows = conn.execute(
                """SELECT m.user_id, m.station_id, s.name AS station_name
                   FROM admin_user_station_memberships m
                   JOIN admin_user_stations s ON s.id = m.station_id
                   JOIN users u ON u.id = m.user_id
                   ORDER BY u.display_name COLLATE NOCASE, u.id"""
            ).fetchall()
        return {
            "stations": [dict(row) for row in station_rows],
            "assignments": [dict(row) for row in assignment_rows],
        }

    def set_user_station(
        self,
        user_id: int,
        station_id: int | None,
        updated_by: int | None = None,
    ) -> dict[str, Any]:
        user = self.storage.get_user(int(user_id))
        if not user:
            raise LookupError("Brugeren blev ikke fundet.")

        if station_id is None:
            with self.storage.connect() as conn:
                conn.execute(
                    "DELETE FROM admin_user_station_memberships WHERE user_id = ?",
                    (int(user_id),),
                )
            return {"user_id": int(user_id), "station_id": None, "station_name": None}

        try:
            station_id = int(station_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Den valgte station er ugyldig.") from exc
        if station_id < 1:
            raise ValueError("Den valgte station er ugyldig.")

        with self.storage.connect() as conn:
            station = conn.execute(
                "SELECT id, name FROM admin_user_stations WHERE id = ?",
                (station_id,),
            ).fetchone()
            if not station:
                raise LookupError("Den valgte station findes ikke.")
            conn.execute(
                """INSERT INTO admin_user_station_memberships(user_id, station_id, updated_at, updated_by)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       station_id = excluded.station_id,
                       updated_at = excluded.updated_at,
                       updated_by = excluded.updated_by""",
                (int(user_id), station_id, self._now(), updated_by),
            )

        return {
            "user_id": int(user_id),
            "station_id": station_id,
            "station_name": station["name"],
        }


def install_admin_user_stations(core):
    """Register admin-only organisational station endpoints on the Pager app."""

    store = AdminUserStationStore(core.storage)

    @core.app.get("/api/admin/user-stations")
    @core.auth_required(admin=True)
    def api_admin_user_stations_get():
        return jsonify(store.list_overview())

    @core.app.post("/api/admin/user-stations")
    @core.auth_required(admin=True)
    def api_admin_user_stations_post():
        data = request.get_json(silent=True) or {}
        try:
            station = store.create_station(data.get("name"), int(core.g.user["id"]))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        core.storage.add_audit(
            int(core.g.user["id"]),
            "admin-station-create",
            f"station_id={station['id']}; name={station['name']}",
        )
        return jsonify({"ok": True, "station": station})

    @core.app.patch("/api/admin/users/<int:user_id>/station")
    @core.auth_required(admin=True)
    def api_admin_user_station_patch(user_id: int):
        data = request.get_json(silent=True) or {}
        raw_station_id = data.get("station_id")
        station_id = None if raw_station_id in {None, ""} else raw_station_id
        try:
            assignment = store.set_user_station(
                user_id,
                station_id,
                int(core.g.user["id"]),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except LookupError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

        core.storage.add_audit(
            int(core.g.user["id"]),
            "admin-station-assign",
            f"user_id={user_id}; station_id={assignment['station_id'] or 'none'}",
        )
        return jsonify({"ok": True, "assignment": assignment})

    return store
