import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from security_extension import InMemoryRateLimiter, init_security


def test_rate_limiter_expires_old_requests():
    now = [100.0]
    limiter = InMemoryRateLimiter(limit=2, window_seconds=10, clock=lambda: now[0])

    assert limiter.check("client") == (True, 0)
    assert limiter.check("client") == (True, 0)
    allowed, retry_after = limiter.check("client")
    assert not allowed
    assert retry_after == 10

    now[0] = 111.0
    assert limiter.check("client") == (True, 0)


def test_security_headers_are_applied(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "test.db",
        }
    )
    init_security(app)
    response = app.test_client().get(
        "/health",
        headers={"X-Forwarded-Proto": "https"},
    )

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Strict-Transport-Security"].startswith("max-age=31536000")


def test_write_requests_are_rate_limited(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "test.db",
            "RATE_LIMIT_REQUESTS": 1,
            "RATE_LIMIT_WINDOW_SECONDS": 60,
        }
    )

    @app.post("/test-write")
    def test_write():
        return {"ok": True}

    init_security(app)
    client = app.test_client()

    first = client.post("/test-write")
    second = client.post("/test-write")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.get_json()["error"] == "For mange forespørgsler. Prøv igen senere."
    assert int(second.headers["Retry-After"]) >= 1
