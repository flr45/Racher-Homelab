import importlib.util
import json
from pathlib import Path


def test_host_report_disables_workers_and_warns_on_unknown_sms_status(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('host_check', Path(__file__).parents[2] / 'scripts/check-sbr-pager.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/sbr-pager-boot.sh').write_text('same')
    target = tmp_path / 'installed'
    target.write_text('same')
    monkeypatch.setenv('SBR_PAGER_ROOT', str(tmp_path))
    monkeypatch.setenv('SBR_PAGER_BOOT_TARGET', str(target))
    commands = []
    def run(args, timeout=15):
        commands.append(args)
        if args[:2] == ['docker', 'inspect']:
            return 0, json.dumps({'Running': True, 'Health': {'Status': 'healthy'}})
        if args[:2] == ['docker', 'exec']:
            return 0, json.dumps({'internet': 'online', 'modem': 'online', 'whatsapp': 'ready', 'multipart': {'state': 'unknown', 'warning': False}, 'last_sms_registered': True})
        return 0, ''
    monkeypatch.setattr(module, 'run', run)
    results = module.check()
    assert next(row for row in results if row['check'] == 'Delte SMS')['state'] == 'warning'
    execution = next(args for args in commands if args[:2] == ['docker', 'exec'])
    for flag in ['SMS_WHATSAPP_RETRY_WORKER=false', 'SMS_WHATSAPP_MONITOR_WORKER=false', 'SBR_PAGER_AUTO_GEOCODE=false']:
        assert flag in execution[:execution.index('sbr-sms-whatsapp')]


def test_host_probe_import_does_not_recover_a_live_sending_job(authorized):
    import os
    import subprocess
    import sys

    p = authorized
    inbound = p.base.InboundMessage(source_id='live-status-probe', sender='+4512345678',
        body='(S)M+V · Test af læsende rapport', received_at=p.base.utcnow(), accepted=True)
    p.db.session.add(inbound); p.db.session.flush()
    row = p.base.WhatsAppDelivery(inbound_id=inbound.id, recipient_name='Test',
        recipient_phone='+4522222222', status='sending', attempted_at=p.base.utcnow())
    p.db.session.add(row); p.db.session.commit()
    env = dict(os.environ, DATABASE_URL=str(p.db.engine.url),
        SMS_WHATSAPP_RETRY_WORKER='false', SMS_WHATSAPP_MONITOR_WORKER='false',
        SBR_PAGER_AUTO_GEOCODE='false')
    result = subprocess.run([sys.executable, '-c', 'import dashboard_app'],
        cwd=Path(__file__).parents[2] / 'services/sms-whatsapp', env=env,
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    p.db.session.expire_all()
    assert p.base.WhatsAppDelivery.query.one().status == 'sending'
    assert p.deliveries.WhatsAppRetryState.query.count() == 0
