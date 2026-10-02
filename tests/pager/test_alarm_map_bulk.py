import json
import urllib.error
import urllib.parse

import pytest
import history_app as history
import events_app as events
import geocode_app as geocode
import operations
from test_delivery import ingest


def make_alarms(p):
    for n in range(3):
        assert ingest(p, source=f'bulk-{n}', eventKey=f'event-{n}').status_code == 201
    rows = events.AlarmEvent.query.order_by(events.AlarmEvent.id).all()
    assert len(rows) == 3
    for row in rows:
        p.db.session.add(history.AlarmEventLocation(event_id=row.id, latitude=55.4, longitude=11.35))
    p.db.session.commit()
    return [row.id for row in rows]


def test_bulk_deletes_selected_events_and_pending_delivery_only(authorized, client, monkeypatch):
    p = authorized
    ids = make_alarms(p)
    result = client.post('/alarmer/slet-valgte', data={'csrf_token':'test-csrf', 'confirm':'delete', 'event_ids':ids[:2], 'next':'/alarmstatistik?station=S'})
    assert result.status_code == 302 and result.location == '/alarmstatistik?station=S'
    assert [row.id for row in events.AlarmEvent.query.all()] == ids[2:]
    assert history.AlarmEventLocation.query.count() == 1
    assert p.base.InboundMessage.query.count() == 1
    assert p.base.WhatsAppDelivery.query.count() == 1
    assert p.deliveries.WhatsAppRetryState.query.count() == 1
    assert json.loads(operations.AuditEntry.query.one().changes)['event_ids'] == ids[:2]
    calls=[]
    monkeypatch.setattr(p.base, 'send_whatsapp', lambda *a: calls.append(a) or 'receipt')
    p.deliveries.retry_due_once()
    assert len(calls) == 1


@pytest.mark.parametrize('values,confirm,status', [([], 'delete',400),(['bad'],'delete',400),(['-1'],'delete',400),(['1'],'',400),(['9'*100],'delete',400),(['999'],'delete',409),(['1']*201,'delete',400)])
def test_invalid_bulk_request_never_partly_deletes(authorized,client,values,confirm,status):
    ids=make_alarms(authorized)
    result=client.post('/alarmer/slet-valgte',data={'csrf_token':'test-csrf','confirm':confirm,'event_ids':values})
    assert result.status_code==status and events.AlarmEvent.query.count()==3


def test_bulk_stale_selection_is_atomic_and_requires_csrf(authorized,client):
    ids=make_alarms(authorized)
    assert client.post('/alarmer/slet-valgte',data={'confirm':'delete','event_ids':ids}).status_code==400
    assert client.post('/alarmer/slet-valgte',data={'csrf_token':'test-csrf','confirm':'delete','event_ids':[ids[0],999]}).status_code==409
    assert events.AlarmEvent.query.count()==3


def test_map_library_is_local_and_empty_map_is_explained(client):
    response=client.get('/alarmkort')
    assert response.status_code==200 and b'unpkg.com' not in response.data
    assert b'/static/leaflet/leaflet.js' in response.data
    assert 'Ingen alarmer har en gemt kortplacering'.encode() in response.data
    assert client.get('/static/leaflet/leaflet.js').status_code==200
    assert client.get('/static/leaflet/images/marker-icon.png').status_code==200


def test_new_geocoder_converts_official_projected_coordinates(p,monkeypatch):
    monkeypatch.setattr(geocode,'AUTO_GEOCODE',True)
    monkeypatch.setattr(geocode,'GEOCODER_URL','https://adressevaelger.dk/vask/')
    urls=[]
    def reply(url):
        urls.append(url)
        if '/vask/' in url:
            return {'vaskestatus':{'kode':1000},'vaskeresultat':{'status':3,'adresse_id_lokalid':'197b47fd-050a-40cb-85a9-11418e009374'}}
        return {'status':'ok','adresse':{'husnummer':{'status':'3','adgangspunkt':{'geometri':{'type':'Point','crs':{'properties':{'name':'EPSG:25832'}},'coordinates':[533640.58,6152135.2]}}}}}
    monkeypatch.setattr(geocode,'_request_json',reply)
    result=geocode.geocode_address('Nr. Bjertvej 87A, 6000 Kolding')
    assert 55.4 < result['latitude'] < 55.6 and 9.4 < result['longitude'] < 9.7
    assert len(urls)==2 and '/adresser/197b47fd-' in urls[1]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)['adresse']==['Nr. Bjertvej 87A, 6000 Kolding']


@pytest.mark.parametrize('code',[-300,-400,800,700])
def test_new_geocoder_does_not_guess_intervals_or_missing_addresses(p,monkeypatch,code):
    monkeypatch.setattr(geocode,'AUTO_GEOCODE',True)
    monkeypatch.setattr(geocode,'GEOCODER_URL','https://adressevaelger.dk/vask/')
    monkeypatch.setattr(geocode,'_request_json',lambda url:{'vaskestatus':{'kode':code},'vaskeresultat':{'status':3,'adresse_id_lokalid':'197b47fd-050a-40cb-85a9-11418e009374'}})
    assert geocode.geocode_address('Eksempelvej 1, 4180 Sorø') is None


def test_geocoder_failure_does_not_log_token(p,monkeypatch,caplog):
    monkeypatch.setattr(geocode,'AUTO_GEOCODE',True)
    def fail(url):raise urllib.error.URLError('https://provider/?token=secret-token')
    monkeypatch.setattr(geocode,'_request_json',fail)
    assert geocode.geocode_address('Eksempelvej 1, 4180 Sorø') is None
    assert 'secret-token' not in caplog.text


def test_bulk_deletion_forbidden_for_viewer(authorized):
    from test_resilience import add_operator, personal_client
    ids=make_alarms(authorized)
    viewer=personal_client(authorized,add_operator(authorized,role='viewer'))
    assert viewer.post('/alarmer/slet-valgte',data={'csrf_token':'test-csrf','confirm':'delete','event_ids':ids}).status_code==403
    assert events.AlarmEvent.query.count()==3


def test_map_pages_send_origin_only_referrer_and_other_pages_keep_existing_policy(authorized,client):
    ids=make_alarms(authorized)
    for path in ('/alarmkort',f'/alarmer/{ids[0]}'):
        response=client.get(path)
        assert response.headers['Referrer-Policy']=='strict-origin-when-cross-origin'
        assert b"referrerPolicy:'strict-origin-when-cross-origin'" in response.data
        assert 'Kortet er klar.'.encode() not in response.data
    assert client.get('/alarmer').headers['Referrer-Policy']=='same-origin'


@pytest.mark.parametrize('query',['from=bad','from=2026-10-03&to=2026-10-02','to=2026-02-30'])
def test_map_rejects_invalid_date_ranges(client, query):
    assert client.get('/alarmkort?' + query).status_code == 400


def test_map_filters_local_dates_stations_and_missing_locations(authorized, client):
    from datetime import datetime, timezone
    ids = make_alarms(authorized)
    rows = events.AlarmEvent.query.order_by(events.AlarmEvent.id).all()
    for row in rows:
        row.station = 'S'
        row.started_at = datetime(2026, 10, 1, 22, 30, tzinfo=timezone.utc)
    rows[1].started_at = datetime(2026, 10, 2, 22, 30, tzinfo=timezone.utc)
    history.AlarmEventLocation.query.filter_by(event_id=ids[2]).delete()
    authorized.db.session.commit()
    response = client.get('/alarmkort?station=S&from=2026-10-02&to=2026-10-02')
    assert response.status_code == 200
    assert b'1 af 1 gemte ture' in response.data
    assert b'1 alarmer mangler kortplacering' in response.data
    assert b'alarm-cluster' in response.data
    response = client.get('/alarmkort?station=M')
    assert b'0 af 0 gemte ture' in response.data
