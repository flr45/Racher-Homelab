#!/usr/bin/env python3
"""Read-only host commissioning report. Never sends messages or restarts services."""
import hashlib
import json
import os
from pathlib import Path
import subprocess


def run(args, timeout=15):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, ''


def check():
    results = []
    def add(name, state, detail):
        results.append({'check': name, 'state': state, 'detail': detail})
    root = Path(os.getenv('SBR_PAGER_ROOT', '/opt/SBR-Pager-Gateway'))
    target = Path(os.getenv('SBR_PAGER_BOOT_TARGET', '/usr/local/sbin/sbr-pager-boot'))
    try:
        matched = hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256((root/'scripts/sbr-pager-boot.sh').read_bytes()).digest()
        add('Opstartsfil', 'ok' if matched else 'failed', 'Installeret fil matcher checkout' if matched else 'Installeret opstartsfil afviger')
    except OSError:
        add('Opstartsfil', 'failed', 'Opstartsfil mangler eller kunne ikke læses')
    for name in ('sbr-pager-boot.service', 'sbr-pager-watchdog.timer', 'sbr-modem-watchdog.timer'):
        enabled, _ = run(['systemctl', 'is-enabled', '--quiet', name])
        active, _ = run(['systemctl', 'is-active', '--quiet', name])
        add(name, 'ok' if enabled == active == 0 else 'warning', 'Aktiv og aktiveret ved opstart' if enabled == active == 0 else 'Kontrollér systemd-status')
    for name in ('sbr-sms-whatsapp', 'racher-sms-gateway', 'racher-sms-openwa'):
        code, output = run(['docker', 'inspect', name, '--format', '{{json .State}}'])
        try:
            state = json.loads(output)
            healthy = state.get('Running') and state.get('Health', {}).get('Status') == 'healthy'
        except (ValueError, AttributeError):
            healthy = False
        add(name, 'ok' if not code and healthy else 'failed', 'Healthy' if healthy else 'Ikke bekræftet healthy')
    code, output = run(['docker','exec','-e','SMS_WHATSAPP_RETRY_WORKER=false','-e','SMS_WHATSAPP_MONITOR_WORKER=false','-e','SBR_PAGER_AUTO_GEOCODE=false','sbr-sms-whatsapp','python','-c',
        'import json,dashboard_app as d;\nwith d.app.app_context():\n s=d.snapshot();print(json.dumps({"internet":s["internet"].get("state"),"modem":s["modem"].get("state"),"whatsapp":s["wa"].get("state"),"multipart":s["multipart"],"queue":s["queue"].get("active"),"last_sms_registered":bool(s["ops"].get("last_sms"))}))'], timeout=40)
    try:
        data = json.loads(output)
        for key, expected in [('internet','online'),('modem','online'),('whatsapp','ready')]:
            add(key, 'ok' if data.get(key)==expected else 'warning', str(data.get(key,'unknown')))
        add('SMS-modtagelse', 'ok' if data.get('last_sms_registered') else 'warning', 'Tidligere SMS registreret; en ny fysisk test kræves for at bekræfte aktuel modtagelse')
        multipart = data.get('multipart', {})
        add('Delte SMS', 'ok' if multipart.get('state') == 'current' and not multipart.get('warning') else 'warning', 'Manglende SMS-dele' if multipart.get('warning') else 'Ingen aktuelle advarsler' if multipart.get('state') == 'current' else 'Status kunne ikke bekræftes')
    except (ValueError, AttributeError):
        add('Applikationskontrol','warning','Status kunne ikke læses')
    return results


if __name__ == '__main__':
    report = check()
    print(json.dumps({'checks':report, 'note':'Ingen beskeder sendt. Health-status beviser ikke telefonlevering.'},ensure_ascii=False,indent=2))
    raise SystemExit(0 if all(row['state']=='ok' for row in report) else 1)
