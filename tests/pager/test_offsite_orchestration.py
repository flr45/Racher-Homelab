import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('offsite',[False,True])
def test_boot_default_bind_and_optional_mount_are_preserved(tmp_path,offsite):
    app=tmp_path/'app';app.mkdir()
    (app/'.env').write_text('SMS_MODEM_DRIVER=cudy\n'+('SMS_WHATSAPP_OFFSITE_MOUNT=true\n' if offsite else ''))
    commands=tmp_path/'bin';commands.mkdir();log=tmp_path/'calls'
    docker=commands/'docker';docker.write_text('#!'+sys.executable+'\nimport os,json,sys\nwith open(os.environ["BOOT_TEST_LOG"],"a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\n');docker.chmod(0o755)
    result=subprocess.run(['bash',str(ROOT/'scripts/sbr-pager-boot.sh')],env=dict(os.environ,SBR_PAGER_ROOT=str(app),BOOT_TEST_LOG=str(log),PATH=str(commands)+':'+os.environ['PATH']),capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    calls=[json.loads(x) for x in log.read_text().splitlines()]
    assert [x[-1] for x in calls]==['openwa','sms-whatsapp','sms-gateway']
    assert any(x.endswith('sms-gateway/cudy.yml') for x in calls[-1])
    assert all(any(x.endswith('sms-whatsapp/offsite-backup.yml') for x in call)==offsite for call in calls[:2])


def test_watchdog_recreation_retains_mount_only_for_pager_components(monkeypatch):
    spec=importlib.util.spec_from_file_location('offsite_watchdog',ROOT/'scripts/sbr-pager-watchdog.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setenv('SMS_WHATSAPP_OFFSITE_MOUNT','true')
    calls=[]
    monkeypatch.setattr(module,'run',lambda command,**kw:(calls.append(command) or SimpleNamespace(returncode=0,stdout='',stderr='')))
    for component in ['pager','openwa','gateway']:module.compose_up(component,force_recreate=True)
    assert all('compose/sms-whatsapp/offsite-backup.yml' in call for call in calls[:2])
    assert 'compose/sms-whatsapp/offsite-backup.yml' not in calls[-1]
    assert all(call.index('up')>call.index('-f') and '--force-recreate' in call for call in calls)
