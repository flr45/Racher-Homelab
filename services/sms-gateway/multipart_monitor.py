"""Bounded, text-free diagnostics from the modem owner's actual PDU scans."""
import hashlib
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone


def path():
    return Path(os.getenv('SMS_MULTIPART_STATUS_FILE','/data/multipart-status.json'))


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc)


def read_status():
    try:
        target=path()
        if target.stat().st_size>512*1024:
            raise ValueError('oversized monitor file')
        data=json.loads(target.read_text())
        if not isinstance(data,dict) or not isinstance(data.get('groups'),list):
            raise ValueError('invalid monitor file')
        age=max(0,(datetime.now(timezone.utc)-timestamp(data['checked_at'])).total_seconds())
        data['state']='current' if age<=120 else 'stale'
        return data
    except (OSError,ValueError,KeyError,TypeError):
        return {'state':'unknown','checked_at':None,'groups':[]}


def observe(parts, now=None):
    try:
        now=now or datetime.now(timezone.utc)
        data=read_status()
        groups={row['key']:row for row in data['groups'] if isinstance(row,dict) and 'key' in row}
        current={}
        for part in parts:
            if part.concat_reference is None or not part.concat_total or part.concat_total<2:
                continue
            if not part.concat_part or not 1<=part.concat_part<=part.concat_total<=255:
                continue
            fingerprint=hashlib.sha256(f'{part.sender}|{part.concat_reference}|{part.concat_total}'.encode()).hexdigest()[:20]
            item=current.setdefault(fingerprint,{'expected':part.concat_total,'parts':set(),'stamps':[]})
            item['parts'].add(part.concat_part)
            item['stamps'].append(part.timestamp)
        for fingerprint,item in current.items():
            stamp=min(item['stamps'])
            key=hashlib.sha256(f'{fingerprint}|{stamp}'.encode()).hexdigest()[:20]
            # SC timestamps may differ between parts. Reuse the incomplete
            # group currently on the SIM, just as the assembler groups by ref.
            pending=next((row for row in groups.values() if row.get('fingerprint')==fingerprint and (row.get('source_stamp')==stamp or row.get('state')=='missing')),None)
            if pending:
                key=pending['key']
            row=groups.get(key,{'key':key,'first_seen':now.isoformat(),'completed_at':None})
            seen=sorted(item['parts'])
            missing=sorted(set(range(1,item['expected']+1))-set(seen))
            row.update(fingerprint=fingerprint,source_stamp=stamp,expected=item['expected'],seen=seen,missing=missing,last_seen=now.isoformat(),state='missing' if missing else 'complete')
            if not missing and not row.get('completed_at'):
                row['completed_at']=now.isoformat()
            row['wait_seconds']=max(0,round(((timestamp(row['completed_at']) if row['completed_at'] else now)-timestamp(row['first_seen'])).total_seconds(),1))
            groups[key]=row
        observed_keys={row['key'] for row in groups.values() if row.get('last_seen')==now.isoformat()}
        for key,row in groups.items():
            if key not in observed_keys and row.get('state')=='missing':
                row['state']='disappeared'
        rows=sorted((row for row in groups.values() if (now-timestamp(row['last_seen'])).total_seconds()<86400),key=lambda r:r['last_seen'],reverse=True)[:100]
        target=path();target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_suffix('.tmp')
        temporary.write_text(json.dumps({'checked_at':now.isoformat(),'groups':rows}))
        os.chmod(temporary,0o600);temporary.replace(target)
    except Exception:
        # Observability must never stop PDU reception or SIM acknowledgements.
        logging.getLogger('sms-multipart-monitor').exception('Kunne ikke opdatere delt-SMS-kontrol')
