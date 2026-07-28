import json
from datetime import datetime, timedelta, timezone
from urllib import parse, request

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
PUSHOVER_MESSAGES_URL = "https://api.pushover.net/1/messages.json"


def _valid_http_url(value):
    try:
        parsed = parse.urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def configured_channels(config):
    channels = {}

    webhook_url = str(config.get("NOTIFICATION_WEBHOOK_URL") or "").strip()
    if webhook_url and _valid_http_url(webhook_url):
        channels["webhook"] = {"kind": "webhook", "url": webhook_url}

    pushover_token = str(config.get("PUSHOVER_APP_TOKEN") or "").strip()
    pushover_user = str(config.get("PUSHOVER_USER_KEY") or "").strip()
    if pushover_token and pushover_user:
        channels["pushover"] = {
            "kind": "pushover",
            "token": pushover_token,
            "user": pushover_user,
        }

    return channels


def severity_is_enabled(severity, minimum):
    return SEVERITY_ORDER.get(str(severity).lower(), -1) >= SEVERITY_ORDER.get(
        str(minimum).lower(), SEVERITY_ORDER["critical"]
    )


def enqueue_finding_notifications(
    finding,
    database_factory,
    channels,
    *,
    minimum_severity="critical",
    recorded_at=None,
):
    severity = str(finding.get("severity", "warning")).lower()
    if not channels or not severity_is_enabled(severity, minimum_severity):
        return 0

    timestamp = recorded_at or datetime.now(timezone.utc)
    queued = 0
    with database_factory() as connection:
        for channel_name in sorted(channels):
            connection.execute(
                """INSERT INTO notifications (
                    created_at,
                    event_key,
                    channel,
                    severity,
                    title,
                    message,
                    status,
                    attempts,
                    next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (
                    timestamp.isoformat(),
                    str(finding.get("key", "unknown"))[:200],
                    channel_name,
                    severity[:20],
                    str(finding.get("title", "Racher OS notification"))[:200],
                    str(finding.get("message", ""))[:500],
                    timestamp.isoformat(),
                ),
            )
            queued += 1
    return queued


def list_notifications(limit, database_factory):
    with database_factory() as connection:
        rows = connection.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def notification_center_status(
    database_factory,
    channels,
    *,
    minimum_severity="critical",
):
    with database_factory() as connection:
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM notifications GROUP BY status"
            ).fetchall()
        }
        latest = connection.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT 1"
        ).fetchone()

    retrying = counts.get("retrying", 0)
    processing = counts.get("processing", 0)
    return {
        "enabled": bool(channels),
        "channels": sorted(channels),
        "minimum_severity": minimum_severity,
        "pending": counts.get("pending", 0) + retrying + processing,
        "retrying": retrying,
        "processing": processing,
        "sent": counts.get("sent", 0),
        "failed": counts.get("failed", 0),
        "latest": dict(latest) if latest else None,
    }


def _message_text(notification):
    severity = str(notification["severity"]).upper()
    return f"[{severity}] {notification['title']}\n{notification['message']}"


def _send_webhook(channel, notification, timeout_seconds):
    payload = json.dumps(
        {
            "content": _message_text(notification),
            "allowed_mentions": {"parse": []},
        }
    ).encode("utf-8")
    webhook_request = request.Request(
        channel["url"],
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Racher-OS-Notification-Center/1.0",
        },
        method="POST",
    )
    with request.urlopen(webhook_request, timeout=timeout_seconds) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Webhook returned HTTP {response.status}")


def _send_pushover(channel, notification, timeout_seconds):
    payload = parse.urlencode(
        {
            "token": channel["token"],
            "user": channel["user"],
            "title": notification["title"],
            "message": notification["message"],
            "priority": 1 if notification["severity"] == "critical" else 0,
        }
    ).encode("utf-8")
    pushover_request = request.Request(
        PUSHOVER_MESSAGES_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Racher-OS-Notification-Center/1.0",
        },
        method="POST",
    )
    with request.urlopen(pushover_request, timeout=timeout_seconds) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Pushover returned HTTP {response.status}")


def send_notification(channel, notification, timeout_seconds):
    if channel["kind"] == "webhook":
        _send_webhook(channel, notification, timeout_seconds)
        return
    if channel["kind"] == "pushover":
        _send_pushover(channel, notification, timeout_seconds)
        return
    raise ValueError(f"Unsupported notification channel: {channel['kind']}")


def _safe_error_message(exc, channel):
    message = f"{type(exc).__name__}: {exc}"
    for value in channel.values() if channel else ():
        if isinstance(value, str) and value:
            message = message.replace(value, "[redacted]")
    return message[:500]


def _claim_due_notifications(
    database_factory,
    timestamp,
    *,
    limit,
    processing_lease_seconds,
):
    lease_until = timestamp + timedelta(seconds=processing_lease_seconds)
    with database_factory() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT * FROM notifications
               WHERE (
                    status IN ('pending', 'retrying')
                    AND next_attempt_at <= ?
               ) OR (
                    status = 'processing'
                    AND next_attempt_at <= ?
               )
               ORDER BY id
               LIMIT ?""",
            (timestamp.isoformat(), timestamp.isoformat(), limit),
        ).fetchall()
        if rows:
            placeholders = ",".join("?" for _ in rows)
            connection.execute(
                f"""UPDATE notifications
                    SET status = 'processing', next_attempt_at = ?
                    WHERE id IN ({placeholders})""",
                (lease_until.isoformat(), *(row["id"] for row in rows)),
            )
    return [dict(row) for row in rows]


def purge_old_notifications(
    database_factory,
    *,
    now=None,
    retention_days=30,
):
    timestamp = now or datetime.now(timezone.utc)
    cutoff = timestamp - timedelta(days=max(1, int(retention_days)))
    with database_factory() as connection:
        cursor = connection.execute(
            """DELETE FROM notifications
               WHERE status IN ('sent', 'failed')
                 AND COALESCE(sent_at, created_at) < ?""",
            (cutoff.isoformat(),),
        )
    return max(cursor.rowcount, 0)


def dispatch_pending_notifications(
    database_factory,
    channels,
    *,
    now=None,
    limit=20,
    max_attempts=5,
    retry_base_seconds=60,
    timeout_seconds=10,
    retention_days=30,
    sender=send_notification,
):
    timestamp = now or datetime.now(timezone.utc)
    limit = max(1, int(limit))
    max_attempts = max(1, int(max_attempts))
    retry_base_seconds = max(1, int(retry_base_seconds))
    timeout_seconds = max(1, int(timeout_seconds))
    processing_lease_seconds = max(30, timeout_seconds * 3)
    rows = _claim_due_notifications(
        database_factory,
        timestamp,
        limit=limit,
        processing_lease_seconds=processing_lease_seconds,
    )

    result = {
        "processed": 0,
        "sent": 0,
        "retrying": 0,
        "failed": 0,
        "purged": 0,
    }
    for notification in rows:
        result["processed"] += 1
        channel = channels.get(notification["channel"])
        try:
            if not channel:
                raise RuntimeError("Notification channel is not configured")
            sender(channel, notification, timeout_seconds)
        except Exception as exc:
            attempts = notification["attempts"] + 1
            terminal = attempts >= max_attempts
            delay = min(retry_base_seconds * (2 ** max(attempts - 1, 0)), 3600)
            next_attempt_at = None if terminal else timestamp + timedelta(seconds=delay)
            status = "failed" if terminal else "retrying"
            with database_factory() as connection:
                connection.execute(
                    """UPDATE notifications
                       SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ?
                       WHERE id = ?""",
                    (
                        status,
                        attempts,
                        next_attempt_at.isoformat() if next_attempt_at else None,
                        _safe_error_message(exc, channel),
                        notification["id"],
                    ),
                )
            result[status] += 1
            continue

        with database_factory() as connection:
            connection.execute(
                """UPDATE notifications
                   SET status = 'sent', sent_at = ?, attempts = attempts + 1,
                       next_attempt_at = NULL, last_error = NULL
                   WHERE id = ?""",
                (timestamp.isoformat(), notification["id"]),
            )
        result["sent"] += 1

    result["purged"] = purge_old_notifications(
        database_factory,
        now=timestamp,
        retention_days=retention_days,
    )
    return result
