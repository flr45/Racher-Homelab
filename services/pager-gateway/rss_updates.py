from __future__ import annotations

import hashlib
import html
import ipaddress
import os
import re
import socket
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

from flask import g, jsonify, request
from pywebpush import WebPushException


POLICE_FEEDS: tuple[tuple[str, str], ...] = (
    ("Politi Update · Alle", "https://via.ritzau.dk/rss/short-messages/latest"),
    ("Bornholms Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90799"),
    ("Fyns Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90797"),
    ("Københavns Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90685"),
    ("Københavns Vestegns Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90718"),
    ("Midt- og Vestjyllands Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90687"),
    ("Midt- og Vestsjællands Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=13562881"),
    ("National enhed for Særlig Kriminalitet", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=13562884"),
    ("Nordjyllands Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=13562880"),
    ("Nordsjællands Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90719"),
    ("Syd- og Sønderjyllands Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90686"),
    ("Sydøstjyllands Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90720"),
    ("Sydsjællands og Lolland-Falsters Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90594"),
    ("Østjyllands Politi", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90721"),
    ("Rigspolitiet", "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90752"),
)

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ITEMS_PER_FEED = 100
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
PUNCT_SPACE_RE = re.compile(r"\s+([,.;:!?])")


def clean_text(value: Any, limit: int = 4000) -> str:
    text = html.unescape(TAG_RE.sub(" ", str(value or "")))
    text = SPACE_RE.sub(" ", text).strip()
    text = PUNCT_SPACE_RE.sub(r"\1", text)
    return text[:limit]


def normalize_published(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return ""


def _local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1].lower()


def _child_text(node: ET.Element, names: set[str]) -> str:
    for child in list(node):
        if _local_name(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def _entry_link(node: ET.Element) -> str:
    for child in list(node):
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        rel = str(child.attrib.get("rel") or "alternate").strip().lower()
        if href and rel in {"", "alternate"}:
            return href
        text = "".join(child.itertext()).strip()
        if text:
            return text
    return ""


def parse_feed_xml(payload: bytes) -> list[dict[str, str]]:
    if not payload or len(payload) > MAX_FEED_BYTES:
        raise ValueError("RSS-feedet er tomt eller for stort")
    if b"<!DOCTYPE" in payload.upper():
        raise ValueError("RSS-feed med DTD accepteres ikke")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"Ugyldigt RSS/XML: {exc}") from exc

    entries: list[dict[str, str]] = []
    for node in root.iter():
        kind = _local_name(node.tag)
        if kind not in {"item", "entry"}:
            continue
        title = clean_text(_child_text(node, {"title"}), 500)
        summary = clean_text(_child_text(node, {"description", "summary", "content"}), 4000)
        link = _entry_link(node).strip()[:2000]
        guid = _child_text(node, {"guid", "id"}).strip()[:2000]
        published = normalize_published(_child_text(node, {"pubdate", "published", "updated", "date"}))
        # Prefer the canonical link for cross-feed deduplication. Some publishers
        # issue feed-specific GUIDs for the same public update.
        identity_basis = link or guid or "|".join((title, published, summary[:500]))
        if not identity_basis or not (title or summary):
            continue
        entries.append({
            "dedupe_key": hashlib.sha256(identity_basis.encode("utf-8", "replace")).hexdigest(),
            "title": title or "Politi Update",
            "summary": summary,
            "link": link if link.startswith(("https://", "http://")) else "",
            "published_at": published,
        })
        if len(entries) >= MAX_ITEMS_PER_FEED:
            break
    return entries


def validate_feed_url(value: Any, *, resolve: bool = True) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("RSS-feed skal bruge https://")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("RSS-feed URL må ikke indeholde login eller fragment")
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname or hostname in {"localhost", "localhost.localdomain"}:
        raise ValueError("Ugyldigt RSS-hostnavn")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("Ugyldig RSS-port") from exc
    if port != 443:
        raise ValueError("RSS-feed skal bruge standard HTTPS-port 443")
    if resolve:
        try:
            addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("RSS-hostnavnet kunne ikke slås op") from exc
        for item in addresses:
            address = ipaddress.ip_address(item[4][0])
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            ):
                raise ValueError("RSS-feed må ikke pege på et privat eller lokalt netværk")
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        safe = validate_feed_url(newurl, resolve=True)
        return super().redirect_request(req, fp, code, msg, headers, safe)


class RSSStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        self._lock = threading.RLock()
        self._initialize()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rss_feeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL DEFAULT 'custom',
                    active INTEGER NOT NULL DEFAULT 1,
                    seeded INTEGER NOT NULL DEFAULT 0,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '',
                    last_fetch_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_by INTEGER,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rss_feeds_active ON rss_feeds(active, id);

                CREATE TABLE IF NOT EXISTS user_rss_subscriptions (
                    user_id INTEGER NOT NULL,
                    feed_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, feed_id),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(feed_id) REFERENCES rss_feeds(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_user_rss_feed ON user_rss_subscriptions(feed_id, user_id);

                CREATE TABLE IF NOT EXISTS user_rss_preferences (
                    user_id INTEGER PRIMARY KEY,
                    push_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS rss_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    link TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rss_items_time ON rss_items(published_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS rss_item_feeds (
                    item_id INTEGER NOT NULL,
                    feed_id INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY(item_id, feed_id),
                    FOREIGN KEY(item_id) REFERENCES rss_items(id) ON DELETE CASCADE,
                    FOREIGN KEY(feed_id) REFERENCES rss_feeds(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_rss_item_feed ON rss_item_feeds(feed_id, item_id DESC);

                CREATE TABLE IF NOT EXISTS rss_push_delivery (
                    item_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(item_id, user_id),
                    FOREIGN KEY(item_id) REFERENCES rss_items(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )
            now = self._now()
            for name, url in POLICE_FEEDS:
                conn.execute(
                    """INSERT INTO rss_feeds(name, url, kind, active, seeded, created_at, updated_at)
                       VALUES (?, ?, 'politi', 1, 1, ?, ?)
                       ON CONFLICT(url) DO UPDATE SET
                           name=CASE WHEN rss_feeds.seeded=1 THEN excluded.name ELSE rss_feeds.name END,
                           kind=CASE WHEN rss_feeds.seeded=1 THEN 'politi' ELSE rss_feeds.kind END,
                           updated_at=excluded.updated_at""",
                    (name, url, now, now),
                )
            conn.commit()

    def list_feeds(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        where = "" if include_inactive else "WHERE f.active=1"
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                f"""SELECT f.*,
                           COUNT(DISTINCT s.user_id) AS subscriber_count,
                           SUM(CASE WHEN COALESCE(p.push_enabled,0)=1 THEN 1 ELSE 0 END) AS push_user_count
                    FROM rss_feeds f
                    LEFT JOIN user_rss_subscriptions s ON s.feed_id=f.id
                    LEFT JOIN user_rss_preferences p ON p.user_id=s.user_id
                    {where}
                    GROUP BY f.id
                    ORDER BY CASE WHEN f.kind='politi' THEN 0 ELSE 1 END, f.name COLLATE NOCASE"""
            ).fetchall()
        return [dict(row) for row in rows]

    def add_feed(self, name: Any, url: Any, created_by: int | None) -> dict[str, Any]:
        clean_name = SPACE_RE.sub(" ", str(name or "").strip())[:120]
        if len(clean_name) < 3:
            raise ValueError("RSS-navnet skal være mindst 3 tegn")
        safe_url = validate_feed_url(url, resolve=True)
        now = self._now()
        with self._lock, self.connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO rss_feeds(name, url, kind, active, seeded, created_at, updated_at, created_by)
                       VALUES (?, ?, 'custom', 1, 0, ?, ?, ?)""",
                    (clean_name, safe_url, now, now, created_by),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("RSS-feedet findes allerede") from exc
            row = conn.execute("SELECT * FROM rss_feeds WHERE id=?", (int(cur.lastrowid),)).fetchone()
            conn.commit()
        return dict(row)

    def update_feed(self, feed_id: int, *, name: Any | None = None, active: Any | None = None) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            current = conn.execute("SELECT * FROM rss_feeds WHERE id=?", (int(feed_id),)).fetchone()
            if not current:
                return None
            clean_name = str(current["name"])
            if name is not None:
                clean_name = SPACE_RE.sub(" ", str(name or "").strip())[:120]
                if len(clean_name) < 3:
                    raise ValueError("RSS-navnet skal være mindst 3 tegn")
            enabled = bool(current["active"]) if active is None else bool(active)
            conn.execute(
                "UPDATE rss_feeds SET name=?, active=?, updated_at=? WHERE id=?",
                (clean_name, 1 if enabled else 0, self._now(), int(feed_id)),
            )
            row = conn.execute("SELECT * FROM rss_feeds WHERE id=?", (int(feed_id),)).fetchone()
            conn.commit()
        return dict(row)

    def normalize_feed_ids(self, feed_ids: Iterable[Any]) -> list[int]:
        values: list[int] = []
        for raw in feed_ids:
            try:
                feed_id = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("RSS-feed-id skal være et tal") from exc
            if feed_id not in values:
                values.append(feed_id)
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM rss_feeds WHERE active=1 AND id IN ({placeholders})", values
            ).fetchall()
        valid = {int(row["id"]) for row in rows}
        if any(feed_id not in valid for feed_id in values):
            raise ValueError("Et eller flere RSS-feeds findes ikke eller er deaktiveret")
        return values

    def set_user_feeds(self, user_id: int, feed_ids: Iterable[Any]) -> list[int]:
        ids = self.normalize_feed_ids(feed_ids)
        now = self._now()
        with self._lock, self.connect() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (int(user_id),)).fetchone():
                raise ValueError("Brugeren findes ikke")
            conn.execute("DELETE FROM user_rss_subscriptions WHERE user_id=?", (int(user_id),))
            conn.executemany(
                "INSERT INTO user_rss_subscriptions(user_id, feed_id, created_at) VALUES (?, ?, ?)",
                [(int(user_id), feed_id, now) for feed_id in ids],
            )
            conn.commit()
        return ids

    def user_feeds(self, user_id: int) -> list[int]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT feed_id FROM user_rss_subscriptions WHERE user_id=? ORDER BY feed_id", (int(user_id),)
            ).fetchall()
        return [int(row["feed_id"]) for row in rows]

    def set_user_push(self, user_id: int, enabled: bool) -> bool:
        with self._lock, self.connect() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (int(user_id),)).fetchone():
                raise ValueError("Brugeren findes ikke")
            conn.execute(
                """INSERT INTO user_rss_preferences(user_id, push_enabled, updated_at)
                   VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
                       push_enabled=excluded.push_enabled, updated_at=excluded.updated_at""",
                (int(user_id), 1 if enabled else 0, self._now()),
            )
            conn.commit()
        return bool(enabled)

    def user_push_enabled(self, user_id: int) -> bool:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT push_enabled FROM user_rss_preferences WHERE user_id=?", (int(user_id),)
            ).fetchone()
        return bool(row and row["push_enabled"])

    def attach_user_feeds(self, users: list[dict[str, Any]]) -> list[dict[str, Any]]:
        names = {int(row["id"]): str(row["name"]) for row in self.list_feeds(include_inactive=True)}
        for user in users:
            ids = self.user_feeds(int(user["id"]))
            user["rss_feeds"] = ids
            user["rss_feed_names"] = [names[item] for item in ids if item in names]
            user["rss_push_enabled"] = self.user_push_enabled(int(user["id"]))
        return users

    def subscribed_feeds_for_poll(self) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT f.* FROM rss_feeds f
                   WHERE f.active=1 AND EXISTS(
                       SELECT 1 FROM user_rss_subscriptions s
                       JOIN users u ON u.id=s.user_id AND u.active=1
                       WHERE s.feed_id=f.id
                   )
                   ORDER BY f.id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def update_fetch_state(self, feed_id: int, *, success: bool, etag: str = "", last_modified: str = "", error: str = "", initialized: bool | None = None) -> None:
        now = self._now()
        with self._lock, self.connect() as conn:
            fields = ["last_fetch_at=?", "last_error=?", "updated_at=?"]
            values: list[Any] = [now, "" if success else str(error or "")[:500], now]
            if success:
                fields.append("last_success_at=?")
                values.append(now)
                if etag:
                    fields.append("etag=?")
                    values.append(str(etag)[:500])
                if last_modified:
                    fields.append("last_modified=?")
                    values.append(str(last_modified)[:500])
            if initialized is not None:
                fields.append("initialized=?")
                values.append(1 if initialized else 0)
            values.append(int(feed_id))
            conn.execute(f"UPDATE rss_feeds SET {', '.join(fields)} WHERE id=?", values)
            conn.commit()

    def ingest_entries(self, feed_id: int, entries: list[dict[str, str]]) -> list[int]:
        now = self._now()
        new_mappings: list[int] = []
        with self._lock, self.connect() as conn:
            for entry in entries:
                conn.execute(
                    """INSERT INTO rss_items(
                           dedupe_key, title, summary, link, published_at, first_seen_at, last_seen_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(dedupe_key) DO UPDATE SET
                           title=excluded.title,
                           summary=CASE WHEN LENGTH(excluded.summary)>LENGTH(rss_items.summary) THEN excluded.summary ELSE rss_items.summary END,
                           link=CASE WHEN excluded.link!='' THEN excluded.link ELSE rss_items.link END,
                           published_at=CASE WHEN excluded.published_at!='' THEN excluded.published_at ELSE rss_items.published_at END,
                           last_seen_at=excluded.last_seen_at""",
                    (
                        entry["dedupe_key"], entry["title"], entry.get("summary", ""), entry.get("link", ""),
                        entry.get("published_at", ""), now, now,
                    ),
                )
                row = conn.execute("SELECT id FROM rss_items WHERE dedupe_key=?", (entry["dedupe_key"],)).fetchone()
                item_id = int(row["id"])
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO rss_item_feeds(item_id, feed_id, first_seen_at) VALUES (?, ?, ?)",
                    (item_id, int(feed_id), now),
                )
                if inserted.rowcount == 1:
                    new_mappings.append(item_id)
            conn.commit()
        return new_mappings

    def list_items_for_user(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT i.*,
                          (SELECT GROUP_CONCAT(f2.name, ' · ')
                           FROM rss_item_feeds m2
                           JOIN rss_feeds f2 ON f2.id=m2.feed_id
                           JOIN user_rss_subscriptions s2 ON s2.feed_id=f2.id AND s2.user_id=?
                           WHERE m2.item_id=i.id AND f2.active=1) AS feed_names
                   FROM rss_items i
                   WHERE EXISTS(
                       SELECT 1 FROM rss_item_feeds m
                       JOIN rss_feeds f ON f.id=m.feed_id AND f.active=1
                       JOIN user_rss_subscriptions s ON s.feed_id=f.id AND s.user_id=?
                       WHERE m.item_id=i.id
                   )
                   ORDER BY CASE WHEN i.published_at!='' THEN i.published_at ELSE i.first_seen_at END DESC, i.id DESC
                   LIMIT ?""",
                (int(user_id), int(user_id), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def push_users_for_feed(self, feed_id: int) -> list[int]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT s.user_id FROM user_rss_subscriptions s
                   JOIN users u ON u.id=s.user_id AND u.active=1
                   JOIN user_rss_preferences p ON p.user_id=s.user_id AND p.push_enabled=1
                   WHERE s.feed_id=? ORDER BY s.user_id""",
                (int(feed_id),),
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def claim_push(self, item_id: int, user_id: int) -> bool:
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO rss_push_delivery(item_id, user_id, status, attempts, updated_at)
                   VALUES (?, ?, 'pending', 0, ?)""",
                (int(item_id), int(user_id), self._now()),
            )
            conn.commit()
            return cur.rowcount == 1

    def finish_push(self, item_id: int, user_id: int, *, success: bool, error: str = "") -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """UPDATE rss_push_delivery SET status=?, attempts=attempts+1,
                          last_error=?, updated_at=? WHERE item_id=? AND user_id=?""",
                ("sent" if success else "failed", str(error or "")[:500], self._now(), int(item_id), int(user_id)),
            )
            conn.commit()

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM rss_items WHERE id=?", (int(item_id),)).fetchone()
        return dict(row) if row else None


class RSSUpdater:
    def __init__(self, core, store: RSSStore, poll_seconds: int = 60) -> None:
        self.core = core
        self.store = store
        self.poll_seconds = max(30, min(int(poll_seconds), 900))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._status = "stopped"
        self._last_cycle = ""
        self._opener = urllib.request.build_opener(SafeRedirectHandler())

    @property
    def status(self) -> dict[str, Any]:
        return {"state": self._status, "poll_seconds": self.poll_seconds, "last_cycle": self._last_cycle}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rss-update-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def wake(self) -> None:
        self._wake.set()

    def _fetch(self, feed: dict[str, Any]) -> tuple[int, bytes, str, str]:
        safe_url = validate_feed_url(feed["url"], resolve=True)
        headers = {
            "User-Agent": "Racher-Pager-Gateway/1.0 RSS reader",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.2",
        }
        if feed.get("etag"):
            headers["If-None-Match"] = str(feed["etag"])
        if feed.get("last_modified"):
            headers["If-Modified-Since"] = str(feed["last_modified"])
        request_obj = urllib.request.Request(safe_url, headers=headers, method="GET")
        try:
            with self._opener.open(request_obj, timeout=12) as response:
                payload = response.read(MAX_FEED_BYTES + 1)
                if len(payload) > MAX_FEED_BYTES:
                    raise ValueError("RSS-feedet overstiger 2 MB")
                return (
                    int(getattr(response, "status", 200)), payload,
                    str(response.headers.get("ETag") or ""), str(response.headers.get("Last-Modified") or ""),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, b"", str(exc.headers.get("ETag") or ""), str(exc.headers.get("Last-Modified") or "")
            raise

    def _notify(self, feed: dict[str, Any], item_ids: list[int]) -> None:
        if not item_ids:
            return
        user_ids = self.store.push_users_for_feed(int(feed["id"]))
        if not user_ids:
            return
        for item_id in item_ids:
            item = self.store.get_item(item_id)
            if not item:
                continue
            for user_id in user_ids:
                if not self.store.claim_push(item_id, user_id):
                    continue
                subscriptions = self.core.storage.list_user_push_subscriptions(user_id)
                if not subscriptions:
                    self.store.finish_push(item_id, user_id, success=False, error="ingen aktiv push-enhed")
                    continue
                sent = 0
                errors: list[str] = []
                body = item.get("title") or item.get("summary") or "Ny Politi Update"
                for subscription in subscriptions:
                    try:
                        self.core.web_push.send(subscription, {
                            "title": f"Politi Update · {feed['name']}",
                            "body": clean_text(body, 240),
                            "url": "/#politi",
                        })
                        sent += 1
                    except WebPushException as exc:
                        if self.core.web_push.is_gone(exc):
                            self.core.storage.delete_push_subscription(subscription["endpoint"])
                        else:
                            errors.append(str(exc))
                    except Exception as exc:  # pragma: no cover - defensive network boundary
                        errors.append(str(exc))
                self.store.finish_push(
                    item_id, user_id, success=sent > 0,
                    error=" | ".join(errors[:3]) if not sent else "",
                )

    def poll_feed(self, feed: dict[str, Any]) -> None:
        was_initialized = bool(feed.get("initialized"))
        try:
            status, payload, etag, modified = self._fetch(feed)
            if status == 304:
                self.store.update_fetch_state(
                    int(feed["id"]), success=True, etag=etag, last_modified=modified,
                    initialized=True if not was_initialized else None,
                )
                return
            entries = parse_feed_xml(payload)
            new_items = self.store.ingest_entries(int(feed["id"]), entries)
            self.store.update_fetch_state(
                int(feed["id"]), success=True, etag=etag, last_modified=modified, initialized=True
            )
            # First successful fetch seeds the timeline without generating a burst
            # of old notifications. Only later arrivals may trigger user RSS push.
            if was_initialized:
                self._notify(feed, new_items)
        except Exception as exc:
            self.store.update_fetch_state(int(feed["id"]), success=False, error=str(exc))
            self.core.app.logger.warning("RSS update failed for %s: %s", feed.get("name"), exc)

    def poll_once(self) -> None:
        self._status = "running"
        for feed in self.store.subscribed_feeds_for_poll():
            if self._stop.is_set():
                break
            self.poll_feed(feed)
        self._last_cycle = datetime.now(timezone.utc).isoformat()

    def _run(self) -> None:
        self._status = "running"
        # Give gunicorn a moment to finish startup before the first outbound fetch.
        if self._stop.wait(2):
            return
        while not self._stop.is_set():
            # Clear before polling so a wake request arriving during a fetch is
            # preserved and causes the next cycle to run immediately.
            self._wake.clear()
            self.poll_once()
            self._wake.wait(self.poll_seconds)
        self._status = "stopped"


def install_rss_updates(core) -> RSSUpdater:
    store = RSSStore(core.DB_PATH)
    try:
        poll_seconds = int(os.getenv("PAGER_RSS_POLL_SECONDS", "60"))
    except ValueError:
        poll_seconds = 60
    updater = RSSUpdater(core, store, poll_seconds)

    original_users_get = core.app.view_functions["api_users_get"]

    def rss_users_get(*args, **kwargs):
        response = original_users_get(*args, **kwargs)
        if isinstance(response, tuple) or getattr(response, "status_code", 200) >= 400:
            return response
        payload = response.get_json(silent=True)
        if isinstance(payload, list):
            return jsonify(store.attach_user_feeds(payload))
        return response

    core.app.view_functions["api_users_get"] = rss_users_get

    original_me = core.app.view_functions["api_me"]

    def rss_me(*args, **kwargs):
        response = original_me(*args, **kwargs)
        if isinstance(response, tuple) or getattr(response, "status_code", 200) >= 400:
            return response
        payload = response.get_json(silent=True)
        if isinstance(payload, dict) and g.user:
            payload["rss_feeds"] = store.user_feeds(g.user["id"])
            payload["rss_push_enabled"] = store.user_push_enabled(g.user["id"])
            return jsonify(payload)
        return response

    core.app.view_functions["api_me"] = rss_me

    @core.app.get("/api/rss/items")
    @core.auth_required()
    def rss_items():
        try:
            limit = max(1, min(int(request.args.get("limit", "50")), 200))
        except ValueError:
            limit = 50
        return jsonify(store.list_items_for_user(g.user["id"], limit))

    @core.app.get("/api/rss/feeds")
    @core.auth_required()
    def rss_feeds():
        if g.user["role"] == "admin":
            return jsonify(store.list_feeds(include_inactive=True))
        selected = set(store.user_feeds(g.user["id"]))
        return jsonify([row for row in store.list_feeds() if int(row["id"]) in selected])

    @core.app.post("/api/rss/feeds")
    @core.auth_required(admin=True)
    def rss_feed_create():
        payload = request.get_json(silent=True) or {}
        try:
            row = store.add_feed(payload.get("name"), payload.get("url"), g.user["id"])
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        core.storage.add_audit(g.user["id"], "rss-feed-create", f"feed_id={row['id']}; name={row['name']}")
        updater.wake()
        return jsonify({"ok": True, "feed": row})

    @core.app.patch("/api/rss/feeds/<int:feed_id>")
    @core.auth_required(admin=True)
    def rss_feed_update(feed_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            row = store.update_feed(
                feed_id,
                name=payload.get("name") if "name" in payload else None,
                active=core.as_bool(payload.get("active")) if "active" in payload else None,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not row:
            return jsonify({"ok": False, "error": "RSS-feedet findes ikke"}), 404
        core.storage.add_audit(g.user["id"], "rss-feed-update", f"feed_id={feed_id}; active={row['active']}")
        updater.wake()
        return jsonify({"ok": True, "feed": row})

    @core.app.patch("/api/users/<int:user_id>/rss")
    @core.auth_required(admin=True)
    def rss_user_routing(user_id: int):
        payload = request.get_json(silent=True) or {}
        feeds = payload.get("feeds", [])
        if not isinstance(feeds, list):
            return jsonify({"ok": False, "error": "RSS-feeds skal sendes som en liste"}), 400
        try:
            selected = store.set_user_feeds(user_id, feeds)
            push_enabled = store.set_user_push(user_id, core.as_bool(payload.get("push_enabled"), False))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        core.storage.add_audit(
            g.user["id"], "user-rss-routing",
            f"user_id={user_id}; feeds={','.join(str(item) for item in selected) or '-'}; push={int(push_enabled)}",
        )
        updater.wake()
        return jsonify({"ok": True, "feeds": selected, "push_enabled": push_enabled})

    @core.app.post("/api/rss/refresh")
    @core.auth_required(admin=True)
    def rss_refresh():
        updater.wake()
        core.storage.add_audit(g.user["id"], "rss-refresh", "RSS-opdatering anmodet")
        return jsonify({"ok": True, "status": updater.status})

    original_status = core.app.view_functions["api_status"]

    def rss_status(*args, **kwargs):
        response = original_status(*args, **kwargs)
        if isinstance(response, tuple) or getattr(response, "status_code", 200) >= 400:
            return response
        payload = response.get_json(silent=True)
        if isinstance(payload, dict):
            payload["rss"] = updater.status
            payload["rss_feeds"] = len(store.subscribed_feeds_for_poll())
            return jsonify(payload)
        return response

    core.app.view_functions["api_status"] = rss_status
    return updater
