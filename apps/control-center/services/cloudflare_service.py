import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_CACHE = {}
_CACHE_LOCK = threading.Lock()


class CloudflareServiceError(RuntimeError):
    pass


def _clean_error(error):
    text = str(error or "Cloudflare request failed")
    for secret_word in ("Authorization", "Bearer", "token", "secret"):
        text = text.replace(secret_word, "credential")
    return text[:300]


def _request_json(url, token, timeout, opener=urllib.request.urlopen):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Racher-OS-Control-Center",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise CloudflareServiceError(_clean_error(exc)) from exc
    if not payload.get("success", False):
        errors = payload.get("errors") or []
        message = errors[0].get("message") if errors else "Cloudflare API returned an error"
        raise CloudflareServiceError(_clean_error(message))
    return payload.get("result") or []


def _api(path, query=None):
    url = f"https://api.cloudflare.com/client/v4/{path.lstrip('/')}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def _tunnel(item):
    return {
        "id": item.get("id"),
        "name": str(item.get("name") or "")[:150],
        "status": item.get("status"),
        "created_at": item.get("created_at"),
        "deleted_at": item.get("deleted_at"),
        "connections": len(item.get("connections") or []),
    }


def _access_app(item):
    return {
        "id": item.get("id"),
        "name": str(item.get("name") or "")[:150],
        "domain": str(item.get("domain") or "")[:255],
        "type": item.get("type"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _dns_record(item):
    record_type = item.get("type")
    return {
        "id": item.get("id"),
        "type": record_type,
        "name": str(item.get("name") or "")[:255],
        "content": "[REDACTED]" if record_type == "TXT" else str(item.get("content") or "")[:255],
        "proxied": bool(item.get("proxied")),
        "ttl": item.get("ttl"),
    }


def fetch_cloudflare_snapshot(account_id, zone_id, token, *, timeout=8, opener=urllib.request.urlopen):
    if not account_id or not zone_id or not token:
        raise CloudflareServiceError("Cloudflare credentials are incomplete")
    tunnels = _request_json(
        _api(f"accounts/{account_id}/cfd_tunnel", {"is_deleted": "false", "per_page": 100}),
        token,
        timeout,
        opener,
    )
    access_apps = _request_json(
        _api(f"accounts/{account_id}/access/apps", {"per_page": 100}), token, timeout, opener
    )
    zone = _request_json(_api(f"zones/{zone_id}"), token, timeout, opener)
    records = _request_json(
        _api(f"zones/{zone_id}/dns_records", {"per_page": 100}), token, timeout, opener
    )
    normalized_tunnels = [_tunnel(item) for item in tunnels]
    return {
        "enabled": True,
        "account_id": account_id,
        "zone": {
            "id": zone.get("id"),
            "name": zone.get("name"),
            "status": zone.get("status"),
            "paused": bool(zone.get("paused")),
            "plan": (zone.get("plan") or {}).get("name"),
        },
        "tunnels": normalized_tunnels,
        "unhealthy_tunnels": [
            item for item in normalized_tunnels if item.get("status") not in {"healthy", "inactive"}
        ],
        "access_apps": [_access_app(item) for item in access_apps],
        "dns_records": [_dns_record(item) for item in records],
        "fetched_at": time.time(),
        "stale": False,
        "error": None,
    }


def cloudflare_status(config, *, force=False, opener=urllib.request.urlopen, now=time.time):
    account_id = str(config.get("CLOUDFLARE_ACCOUNT_ID", "")).strip()
    zone_id = str(config.get("CLOUDFLARE_ZONE_ID", "")).strip()
    token = str(config.get("CLOUDFLARE_API_TOKEN", "")).strip()
    ttl = max(15, int(config.get("CLOUDFLARE_CACHE_SECONDS", 120)))
    timeout = max(1, int(config.get("CLOUDFLARE_TIMEOUT_SECONDS", 8)))
    if not account_id or not zone_id or not token:
        return {
            "enabled": False,
            "configured": {"account_id": bool(account_id), "zone_id": bool(zone_id), "token": bool(token)},
            "stale": False,
            "error": None,
        }
    cache_key = f"{account_id}:{zone_id}"
    current = now()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and not force and current - cached["cached_at"] < ttl:
            return dict(cached["snapshot"])
    try:
        snapshot = fetch_cloudflare_snapshot(account_id, zone_id, token, timeout=timeout, opener=opener)
    except CloudflareServiceError as exc:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached:
            stale = dict(cached["snapshot"])
            stale["stale"] = True
            stale["error"] = _clean_error(exc)
            return stale
        return {
            "enabled": True,
            "account_id": account_id,
            "zone": None,
            "tunnels": [],
            "unhealthy_tunnels": [],
            "access_apps": [],
            "dns_records": [],
            "stale": False,
            "error": _clean_error(exc),
        }
    with _CACHE_LOCK:
        _CACHE[cache_key] = {"cached_at": current, "snapshot": dict(snapshot)}
    return snapshot


def clear_cloudflare_cache():
    with _CACHE_LOCK:
        _CACHE.clear()
