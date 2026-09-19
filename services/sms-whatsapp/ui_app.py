from __future__ import annotations

import admin_station_app as previous

app = previous.app


_previous_dashboard = app.view_functions["dashboard"]


def dashboard_with_collapsible_technical_logs():
    raw_response = _previous_dashboard()
    response = app.make_response(raw_response)
    if response.status_code != 200 or "text/html" not in response.content_type:
        return response

    html = response.get_data(as_text=True)
    enhancement = r"""
<style>
.tech-log-toggle{width:100%;display:flex;align-items:center;justify-content:space-between;gap:12px;border:0;background:transparent;color:var(--text);padding:0;cursor:pointer;text-align:left;font:inherit}
.tech-log-toggle .tech-title{font-size:17px;font-weight:700}.tech-log-toggle .tech-arrow{color:var(--muted);font-size:18px;transition:transform .18s ease}.tech-log-toggle[aria-expanded="true"] .tech-arrow{transform:rotate(180deg)}
.tech-log-body[hidden]{display:none!important}.tech-log-body{margin-top:14px}
</style>
<script>
document.addEventListener('DOMContentLoaded', function () {
  ['Teknisk SMS-log', 'Teknisk WhatsApp-log'].forEach(function (title) {
    const heading = Array.from(document.querySelectorAll('section.card.span12 > h2')).find(function (node) {
      return node.textContent.trim() === title;
    });
    if (!heading) return;
    const section = heading.parentElement;
    const body = document.createElement('div');
    body.className = 'tech-log-body';
    body.hidden = true;
    const siblings = Array.from(section.children).filter(function (node) { return node !== heading; });
    siblings.forEach(function (node) { body.appendChild(node); });

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tech-log-toggle';
    button.setAttribute('aria-expanded', 'false');
    button.innerHTML = '<span class="tech-title"></span><span class="tech-arrow">⌄</span>';
    button.querySelector('.tech-title').textContent = title;
    button.addEventListener('click', function () {
      const open = button.getAttribute('aria-expanded') === 'true';
      button.setAttribute('aria-expanded', open ? 'false' : 'true');
      body.hidden = open;
    });

    heading.replaceWith(button);
    section.appendChild(body);
  });
});
</script>
"""
    if "tech-log-toggle" not in html:
        html = html.replace("</body>", enhancement + "</body>", 1)
    response.set_data(html)
    return response


app.view_functions["dashboard"] = dashboard_with_collapsible_technical_logs
