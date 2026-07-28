import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_CACHE = {}
_CACHE_LOCK = threading.Lock()


class GitHubServiceError(RuntimeError):
    pass


def _clean_error(error):
    text = str(error or "GitHub request failed")
    return text.replace("Authorization", "credential").replace("Bearer", "credential")[:300]


def _request_json(url, token, timeout, opener=urllib.request.urlopen):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Racher-OS-Control-Center",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), dict(response.headers)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise GitHubServiceError(_clean_error(exc)) from exc


def _api_url(repository, path, query=None):
    url = f"https://api.github.com/repos/{repository}/{path.lstrip('/')}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def _commit(item):
    commit = item.get("commit") or {}
    author = commit.get("author") or {}
    return {
        "sha": str(item.get("sha") or "")[:40],
        "short_sha": str(item.get("sha") or "")[:7],
        "message": str(commit.get("message") or "").splitlines()[0][:200],
        "author": str(author.get("name") or "unknown")[:100],
        "date": author.get("date"),
        "url": item.get("html_url"),
    }


def _pull(item):
    return {
        "number": item.get("number"),
        "title": str(item.get("title") or "")[:200],
        "draft": bool(item.get("draft")),
        "updated_at": item.get("updated_at"),
        "url": item.get("html_url"),
        "head": (item.get("head") or {}).get("ref"),
    }


def _run(item):
    return {
        "id": item.get("id"),
        "name": str(item.get("name") or "")[:150],
        "status": item.get("status"),
        "conclusion": item.get("conclusion"),
        "branch": item.get("head_branch"),
        "sha": str(item.get("head_sha") or "")[:7],
        "created_at": item.get("created_at"),
        "url": item.get("html_url"),
    }


def fetch_github_snapshot(repository, token="", *, timeout=8, opener=urllib.request.urlopen):
    if not repository or "/" not in repository:
        raise GitHubServiceError("GITHUB_REPOSITORY skal være angivet som owner/repository")

    repo, repo_headers = _request_json(
        f"https://api.github.com/repos/{repository}", token, timeout, opener
    )
    default_branch = repo.get("default_branch") or "main"
    commits, _ = _request_json(
        _api_url(repository, "commits", {"sha": default_branch, "per_page": 8}),
        token,
        timeout,
        opener,
    )
    pulls, _ = _request_json(
        _api_url(repository, "pulls", {"state": "open", "per_page": 20}),
        token,
        timeout,
        opener,
    )
    runs, _ = _request_json(
        _api_url(repository, "actions/runs", {"branch": default_branch, "per_page": 10}),
        token,
        timeout,
        opener,
    )
    releases, _ = _request_json(
        _api_url(repository, "releases", {"per_page": 5}), token, timeout, opener
    )
    tags, _ = _request_json(
        _api_url(repository, "tags", {"per_page": 10}), token, timeout, opener
    )

    workflow_runs = [_run(item) for item in (runs.get("workflow_runs") or [])]
    failed_runs = [
        item for item in workflow_runs if item["conclusion"] in {"failure", "cancelled", "timed_out"}
    ]
    latest_release = releases[0] if releases else None
    rate_remaining = repo_headers.get("X-RateLimit-Remaining")

    return {
        "enabled": True,
        "repository": repository,
        "private": bool(repo.get("private")),
        "default_branch": default_branch,
        "open_issues": int(repo.get("open_issues_count") or 0),
        "updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
        "url": repo.get("html_url"),
        "commits": [_commit(item) for item in commits],
        "pull_requests": [_pull(item) for item in pulls],
        "workflow_runs": workflow_runs,
        "failed_runs": failed_runs,
        "latest_release": {
            "name": latest_release.get("name") or latest_release.get("tag_name"),
            "tag": latest_release.get("tag_name"),
            "published_at": latest_release.get("published_at"),
            "url": latest_release.get("html_url"),
        }
        if latest_release
        else None,
        "tags": [str(item.get("name") or "")[:100] for item in tags],
        "rate_limit_remaining": int(rate_remaining) if str(rate_remaining).isdigit() else None,
        "fetched_at": time.time(),
        "stale": False,
        "error": None,
    }


def github_status(config, *, force=False, opener=urllib.request.urlopen, now=time.time):
    repository = str(config.get("GITHUB_REPOSITORY", "")).strip()
    token = str(config.get("GITHUB_TOKEN", "")).strip()
    ttl = max(15, int(config.get("GITHUB_CACHE_SECONDS", 120)))
    timeout = max(1, int(config.get("GITHUB_TIMEOUT_SECONDS", 8)))
    if not repository:
        return {"enabled": False, "repository": None, "stale": False, "error": None}

    cache_key = repository
    current = now()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and not force and current - cached["cached_at"] < ttl:
            return dict(cached["snapshot"])

    try:
        snapshot = fetch_github_snapshot(
            repository, token, timeout=timeout, opener=opener
        )
    except GitHubServiceError as exc:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached:
            stale = dict(cached["snapshot"])
            stale["stale"] = True
            stale["error"] = _clean_error(exc)
            return stale
        return {
            "enabled": True,
            "repository": repository,
            "stale": False,
            "error": _clean_error(exc),
            "commits": [],
            "pull_requests": [],
            "workflow_runs": [],
            "failed_runs": [],
            "tags": [],
            "latest_release": None,
        }

    with _CACHE_LOCK:
        _CACHE[cache_key] = {"cached_at": current, "snapshot": dict(snapshot)}
    return snapshot


def clear_github_cache():
    with _CACHE_LOCK:
        _CACHE.clear()
