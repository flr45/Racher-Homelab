import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def setup_restore(tmp_path, fail=''):
    bindir=tmp_path/'bin';bindir.mkdir()
    logfile=tmp_path/'commands.jsonl'
    stub='''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['RESTORE_TEST_LOG'],'a') as f:f.write(json.dumps([name,*args])+'\\n')
if name=='id':print('0')
if name=='docker' and os.environ.get('RESTORE_TEST_FAIL')=='config' and 'config' in args:sys.exit(2)
if name=='docker' and os.environ.get('RESTORE_TEST_FAIL')=='restore' and '/app/restore_db.py' in args:sys.exit(2)
'''
    for name in ('docker','systemctl','id'):
        path=bindir/name;path.write_text(stub);path.chmod(0o755)
    (tmp_path/'.env').write_text('test=true\n')
    backup=tmp_path/'backup.sqlite';backup.write_bytes(b'test backup; command stub only')
    env=dict(os.environ,PATH=str(bindir)+':'+os.environ['PATH'],SBR_PAGER_ROOT=str(tmp_path),RESTORE_TEST_LOG=str(logfile),RESTORE_TEST_FAIL=fail)
    return backup,env,logfile


def test_restore_requires_explicit_word_before_stopping_anything(tmp_path):
    backup,env,logfile=setup_restore(tmp_path)
    result=subprocess.run(['bash',str(ROOT/'scripts/restore-sbr-pager.sh'),str(backup)],env=env,capture_output=True)
    assert result.returncode!=0 and not logfile.exists()

@pytest.mark.parametrize('fail',['','restore'])
def test_restore_stops_pager_and_restores_watchdog_even_on_failure(tmp_path,fail):
    backup,env,logfile=setup_restore(tmp_path,fail)
    result=subprocess.run(['bash',str(ROOT/'scripts/restore-sbr-pager.sh'),str(backup),'GENDAN'],env=env,capture_output=True)
    commands=[json.loads(line) for line in logfile.read_text().splitlines()]
    stop=next(i for i,c in enumerate(commands) if 'stop' in c and 'sms-whatsapp' in c)
    restore=next(i for i,c in enumerate(commands) if '/app/restore_db.py' in c)
    restart=next(i for i,c in enumerate(commands) if 'up' in c)
    assert stop<restore<restart
    assert ['systemctl','start','sbr-pager-watchdog.timer'] in commands
    assert all('openwa' not in c and 'sms-gateway' not in c for c in commands)
    assert result.returncode==(2 if fail else 0)


def test_compose_validation_failure_never_stops_running_pager(tmp_path):
    backup,env,logfile=setup_restore(tmp_path,'config')
    result=subprocess.run(['bash',str(ROOT/'scripts/restore-sbr-pager.sh'),str(backup),'GENDAN'],env=env,capture_output=True)
    assert result.returncode!=0
    commands=[json.loads(line) for line in logfile.read_text().splitlines()]
    assert not any('stop' in c for c in commands)
