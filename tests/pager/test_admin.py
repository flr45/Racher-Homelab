import pytest
from bs4 import BeautifulSoup


@pytest.mark.parametrize("path", ["/", "/brugere", "/alarmer", "/alarmkort", "/indstillinger", "/forbindelser", "/stationsfilter"])
def test_admin_pages_have_navigation_and_require_login(p, client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert 'pager-sidebar' in response.text
    assert BeautifulSoup(response.text, "html.parser").select_one('link[href="/static/pager.css"]')
    assert p.app.test_client().get(path).status_code == 302


def test_login_accepts_unicode_password_without_server_error(p):
    c = p.app.test_client()
    c.get("/login")
    with c.session_transaction() as s:token = s["csrf_token"]
    assert c.post("/login", data={"csrf_token": token, "username": "admin", "password": "test-adgangskode-æ"}).status_code == 302


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "121"])
def test_prealert_delay_rejects_invalid_numbers(p, client, value):
    client.post("/indstillinger/prealarm-delay", data={"csrf_token": "test-csrf", "seconds": value})
    assert p.stations.PagerRuntimeSetting.query.count() == 0


@pytest.mark.parametrize("payload", [[1, 2], {"sender": None, "body": "Test"}, {"sender": "+4512345678", "body": None}])
def test_bad_ingest_returns_400(p, payload):
    assert p.app.test_client().post("/api/incoming", json=payload, headers={"Authorization": "Bearer test-ingest"}).status_code == 400


def test_csrf_and_api_auth_are_required(p, client):
    assert client.post("/forbindelser/cudy-test").status_code == 400
    assert client.post("/leveringsko/genforsog").status_code == 400
    assert p.app.test_client().post("/api/incoming", json={}).status_code == 401


def test_names_cannot_escape_javascript_confirmation(p, client):
    name = "O'Brian');alert(1);//"
    p.db.session.add(p.base.Recipient(name=name, phone="+4512345678", active=True)); p.db.session.commit()
    page = BeautifulSoup(client.get("/brugere").text, "html.parser")
    assert all(name not in form.get('onsubmit', '') for form in page.select('form[onsubmit]'))
