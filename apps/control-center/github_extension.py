from html import escape

from flask import Blueprint, current_app, jsonify, request

from services.github_service import github_status

github_blueprint = Blueprint("github_center", __name__)


def status():
    return github_status(current_app.config)


@github_blueprint.get("/api/github")
def api_github():
    force = request.args.get("refresh", "").lower() == "true"
    return jsonify({"github": github_status(current_app.config, force=force)})


def _link(url, label):
    if not url:
        return escape(str(label))
    return f'<a class="btn" href="{escape(str(url), quote=True)}" rel="noreferrer">{escape(str(label))}</a>'


def _card(snapshot):
    if not snapshot.get("enabled"):
        body = '<div class="muted">Konfigurér GITHUB_REPOSITORY for at aktivere modulet.</div>'
        badge = '<span class="pill readonly">Ikke konfigureret</span>'
    elif snapshot.get("error") and not snapshot.get("commits"):
        body = f'<div class="bad">{escape(snapshot["error"])}</div>'
        badge = '<span class="pill readonly">Utilgængelig</span>'
    else:
        prs = len(snapshot.get("pull_requests") or [])
        failed = len(snapshot.get("failed_runs") or [])
        commits = snapshot.get("commits") or []
        latest = commits[0] if commits else None
        stale = " · cachet data" if snapshot.get("stale") else ""
        body = (
            '<div class="notification-stats">'
            f'<div class="notification-stat"><span class="label">Åbne PR’er</span><strong class="{ "warn" if prs else "ok" }">{prs}</strong></div>'
            f'<div class="notification-stat"><span class="label">Fejlede CI</span><strong class="{ "bad" if failed else "ok" }">{failed}</strong></div>'
            f'<div class="notification-stat"><span class="label">Branch</span><strong>{escape(str(snapshot.get("default_branch") or "–"))}</strong></div>'
            '</div>'
        )
        if latest:
            body += (
                '<div class="event">'
                f'<strong>{escape(latest.get("short_sha") or "")}</strong>'
                f'<small>{escape(latest.get("message") or "")} · {escape(latest.get("author") or "")}{stale}</small>'
                '</div>'
            )
        release = snapshot.get("latest_release")
        details = []
        if release:
            details.append(f'Seneste release: {escape(str(release.get("tag") or release.get("name") or "–"))}')
        remaining = snapshot.get("rate_limit_remaining")
        if remaining is not None:
            details.append(f'API-kald tilbage: {remaining}')
        if snapshot.get("error"):
            details.append(f'Advarsel: {escape(snapshot["error"])}')
        if details:
            body += f'<div class="channel-list"><small>{" · ".join(details)}</small></div>'
        body += f'<p>{_link(snapshot.get("url"), "Åbn repository")} <a class="btn" href="/api/github">Vis JSON</a></p>'
        badge = '<span class="pill admin">Read-only</span>'

    return (
        '<article class="card" id="github">'
        '<div class="section"><div><h2>GitHub Center</h2>'
        '<small>Commits, pull requests, CI, releases og tags</small></div>'
        f'{badge}</div>{body}</article>'
    )


def init_github_center(app):
    app.register_blueprint(github_blueprint)

    @app.after_request
    def expose_github(response):
        if request.path == "/api/status" and response.is_json:
            payload = response.get_json(silent=True) or {}
            payload["github"] = status()
            response.set_data(current_app.json.dumps(payload))
            response.content_length = len(response.get_data())
        elif request.path == "/" and response.mimetype == "text/html":
            html = response.get_data(as_text=True)
            marker = '<article class="card" id="docker">'
            card = _card(status())
            response.set_data(html.replace(marker, card + marker, 1))
            response.content_length = len(response.get_data())
        return response
