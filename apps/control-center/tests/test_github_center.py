import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from github_extension import init_github_center  # noqa: E402
from services.github_service import (  # noqa: E402
    clear_github_cache,
    fetch_github_snapshot,
    github_status,
)


class Response:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def opener_factory():
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, dict(request.header_items()), timeout))
        url = request.full_url
        if url.endswith("/flr45/Racher-Homelab"):
            return Response(
                {
                    "default_branch": "main",
                    "private": True,
                    "open_issues_count": 3,
                    "updated_at": "2026-07-28T18:00:00Z",
                    "pushed_at": "2026-07-28T18:00:00Z",
                    "html_url": "https://github.com/flr45/Racher-Homelab",
                },
                {"X-RateLimit-Remaining": "4999"},
            )
        if "/commits?" in url:
            return Response(
                [
                    {
                        "sha": "abcdef1234567890",
                        "html_url": "https://example/commit",
                        "commit": {
                            "message": "Add GitHub Center\n\nDetails",
                            "author": {"name": "Frederik", "date": "2026-07-28T18:00:00Z"},
                        },
                    }
                ]
            )
        if "/pulls?" in url:
            return Response([{"number": 22, "title": "GitHub Center", "draft": False, "updated_at": "now", "html_url": "https://example/pr", "head": {"ref": "feature"}}])
        if "/actions/runs?" in url:
            return Response({"workflow_runs": [{"id": 1, "name": "CI", "status": "completed", "conclusion": "success", "head_branch": "main", "head_sha": "abcdef123", "created_at": "now", "html_url": "https://example/ci"}]})
        if "/releases?" in url:
            return Response([{"name": "v1", "tag_name": "v1.0.0", "published_at": "now", "html_url": "https://example/release"}])
        if "/tags?" in url:
            return Response([{"name": "v1.0.0"}])
        raise AssertionError(url)

    return opener, calls


def test_snapshot_is_read_only_and_sanitized():
    opener, calls = opener_factory()
    snapshot = fetch_github_snapshot(
        "flr45/Racher-Homelab", "secret-token", timeout=4, opener=opener
    )
    assert snapshot["default_branch"] == "main"
    assert snapshot["commits"][0]["message"] == "Add GitHub Center"
    assert snapshot["pull_requests"][0]["number"] == 22
    assert snapshot["failed_runs"] == []
    assert snapshot["latest_release"]["tag"] == "v1.0.0"
    assert snapshot["rate_limit_remaining"] == 4999
    assert all(method[1].get("Authorization") == "Bearer secret-token" for method in calls)
    assert "secret-token" not in json.dumps(snapshot)


def test_cache_avoids_duplicate_requests():
    clear_github_cache()
    opener, calls = opener_factory()
    config = {
        "GITHUB_REPOSITORY": "flr45/Racher-Homelab",
        "GITHUB_TOKEN": "secret-token",
        "GITHUB_CACHE_SECONDS": 120,
        "GITHUB_TIMEOUT_SECONDS": 4,
    }
    first = github_status(config, opener=opener, now=lambda: 100)
    second = github_status(config, opener=opener, now=lambda: 110)
    assert first == second
    assert len(calls) == 6


def test_extension_exposes_api_without_token(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "test.db",
            "GITHUB_REPOSITORY": "",
            "GITHUB_TOKEN": "never-return-this",
        }
    )
    init_github_center(app)
    response = app.test_client().get("/api/github")
    assert response.status_code == 200
    payload = response.get_json()["github"]
    assert payload["enabled"] is False
    assert "never-return-this" not in response.get_data(as_text=True)
