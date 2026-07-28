import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from cloudflare_extension import init_cloudflare_center
from services.cloudflare_service import clear_cloudflare_cache, cloudflare_status


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def opener_factory(payloads, requests):
    iterator = iter(payloads)

    def opener(request, timeout):
        requests.append((request, timeout))
        return Response(next(iterator))

    return opener


def test_snapshot_redacts_token_and_txt_content():
    clear_cloudflare_cache()
    requests = []
    payloads = [
        {"success": True, "result": [{"id": "t1", "name": "home", "status": "healthy", "connections": [{}]}]},
        {"success": True, "result": [{"id": "a1", "name": "Home", "domain": "home.example.test", "type": "self_hosted"}]},
        {"success": True, "result": {"id": "z1", "name": "example.test", "status": "active", "paused": False, "plan": {"name": "Free"}}},
        {"success": True, "result": [{"id": "d1", "type": "TXT", "name": "secret.example.test", "content": "private-value", "ttl": 1}]},
    ]
    result = cloudflare_status(
        {
            "CLOUDFLARE_ACCOUNT_ID": "account",
            "CLOUDFLARE_ZONE_ID": "zone",
            "CLOUDFLARE_API_TOKEN": "super-secret-token",
            "CLOUDFLARE_CACHE_SECONDS": 120,
            "CLOUDFLARE_TIMEOUT_SECONDS": 8,
        },
        opener=opener_factory(payloads, requests),
    )
    assert result["zone"]["status"] == "active"
    assert result["dns_records"][0]["content"] == "[REDACTED]"
    assert "super-secret-token" not in repr(result)
    assert all(request.headers["Authorization"] == "Bearer super-secret-token" for request, _ in requests)
    assert all(request.method == "GET" for request, _ in requests)


def test_disabled_without_complete_configuration():
    result = cloudflare_status({"CLOUDFLARE_ACCOUNT_ID": "", "CLOUDFLARE_ZONE_ID": "", "CLOUDFLARE_API_TOKEN": ""})
    assert result["enabled"] is False
    assert result["configured"] == {"account_id": False, "zone_id": False, "token": False}


def test_api_dashboard_and_status(tmp_path):
    app = create_app({"TESTING": True, "DATA_ROOT": tmp_path, "DATABASE_PATH": tmp_path / "test.db"})
    init_cloudflare_center(app)
    client = app.test_client()

    api = client.get("/api/cloudflare")
    assert api.status_code == 200
    assert api.get_json()["cloudflare"]["enabled"] is False

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.get_json()["cloudflare"]["enabled"] is False

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Cloudflare Center" in dashboard.get_data(as_text=True)
