import pytest
import dashboard_app as dashboard


@pytest.mark.parametrize('state,wait,group_state,missing,warn', [
    ('current', 0, 'waiting', [2], False),
    ('current', 60, 'waiting', [2], True),
    ('current', 0, 'disappeared', [2], True),
    ('stale', 100, 'disappeared', [2], False),
    ('current', 100, 'complete', [], False),
])
def test_multipart_warning_threshold(p, monkeypatch, state, wait, group_state, missing, warn):
    monkeypatch.setattr(dashboard, '_multipart_cache', (0, {}))
    monkeypatch.setattr(dashboard, 'gateway_request', lambda *args: {'state': state, 'groups': [dict(expected=2, seen=[1], missing=missing, state=group_state, wait_seconds=wait)]})
    assert dashboard.multipart_summary()['warning'] is warn


def test_gateway_failure_is_unknown(p, monkeypatch):
    monkeypatch.setattr(dashboard, '_multipart_cache', (0, {}))
    def fail(*args):
        raise OSError('secret must not escape')
    monkeypatch.setattr(dashboard, 'gateway_request', fail)
    summary = dashboard.multipart_summary()
    assert summary['state'] == 'unknown'
    assert 'secret' not in str(summary)
