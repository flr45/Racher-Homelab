import argparse
import logging
import signal
import sys
import time
from threading import Event

from app import create_app, database
from services.monitoring_service import collect_snapshot
from services.notification_service import (
    configured_channels,
    dispatch_pending_notifications,
)
from services.worker_service import (
    get_worker_status,
    record_worker_heartbeat,
    register_worker_start,
)

LOGGER = logging.getLogger("racher.monitoring-worker")


def remaining_interval(started_at, interval_seconds, *, finished_at=None):
    finished_at = time.monotonic() if finished_at is None else finished_at
    return max(0.0, interval_seconds - (finished_at - started_at))


def run_cycle(flask_app, database_factory=None):
    database_factory = database if database_factory is None else database_factory

    with flask_app.app_context():
        try:
            containers, docker_error, metrics, backup, findings = collect_snapshot(
                database_factory
            )
            notification_result = dispatch_pending_notifications(
                database_factory,
                configured_channels(flask_app.config),
                limit=flask_app.config.get("NOTIFICATION_BATCH_SIZE", 20),
                max_attempts=flask_app.config.get("NOTIFICATION_MAX_ATTEMPTS", 5),
                retry_base_seconds=flask_app.config.get(
                    "NOTIFICATION_RETRY_BASE_SECONDS", 60
                ),
                timeout_seconds=flask_app.config.get(
                    "NOTIFICATION_HTTP_TIMEOUT_SECONDS", 10
                ),
                retention_days=flask_app.config.get("NOTIFICATION_RETENTION_DAYS", 30),
            )
            record_worker_heartbeat(database_factory, success=True)
            LOGGER.info(
                "Monitoring cycle completed: containers=%s findings=%s cpu=%s ram=%s backup=%s docker_error=%s notifications=%s",
                len(containers),
                len(findings),
                metrics["cpu"],
                metrics["ram"],
                backup["name"] if backup else "missing",
                docker_error or "none",
                notification_result,
            )
            return True
        except Exception as exc:
            LOGGER.exception("Monitoring cycle failed")
            try:
                record_worker_heartbeat(
                    database_factory,
                    success=False,
                    error=str(exc),
                )
            except Exception:
                LOGGER.exception("Could not persist failed worker heartbeat")
            return False


def run_forever(flask_app, *, interval_seconds, stop_event=None, database_factory=None):
    database_factory = database if database_factory is None else database_factory
    stop_event = stop_event or Event()

    with flask_app.app_context():
        register_worker_start(database_factory)

    LOGGER.info("Monitoring worker started with interval=%ss", interval_seconds)
    while not stop_event.is_set():
        started_at = time.monotonic()
        run_cycle(flask_app, database_factory)
        stop_event.wait(remaining_interval(started_at, interval_seconds))
    LOGGER.info("Monitoring worker stopped")


def worker_is_healthy(flask_app, database_factory=None):
    database_factory = database if database_factory is None else database_factory
    with flask_app.app_context():
        status = get_worker_status(
            database_factory,
            max_age_seconds=flask_app.config["WORKER_HEALTH_MAX_AGE_SECONDS"],
        )
    if not status["healthy"]:
        LOGGER.error("Worker healthcheck failed: %s", status)
    return status["healthy"]


def install_signal_handlers(stop_event):
    def request_stop(signum, _frame):
        LOGGER.info("Received signal %s", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Racher OS monitoring worker")
    parser.add_argument("--once", action="store_true", help="Run one monitoring cycle")
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Exit non-zero when the worker heartbeat is stale",
    )
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args(argv)
    flask_app = create_app()

    if args.healthcheck:
        return 0 if worker_is_healthy(flask_app) else 1
    if args.once:
        return 0 if run_cycle(flask_app) else 1

    stop_event = Event()
    install_signal_handlers(stop_event)
    run_forever(
        flask_app,
        interval_seconds=flask_app.config["MONITOR_INTERVAL_SECONDS"],
        stop_event=stop_event,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
