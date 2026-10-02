#!/usr/bin/env python3
"""Run inside the gateway: inspect firmware forms without sending SMS.

Only fixed page paths and form control names/types are printed. Values,
message text, cookies, JavaScript contents and credentials are never printed.
"""
import json
import re
import urllib.request
from html.parser import HTMLParser
from cudy_client import CudyClient


class Structure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = []
        self.forms = 0
        self.status_words = set()

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == 'form':
            self.forms += 1
        if tag in ('input', 'textarea', 'select', 'button'):
            name = fields.get('name', '')
            if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,100}', name):
                self.fields.append({'tag': tag, 'name': name, 'type': fields.get('type', '') if fields.get('type', '') in ('hidden','text','password','submit','button','checkbox','radio','number','') else 'other'})

    def handle_data(self, value):
        # Exact public status vocabulary only; never free-form cell contents.
        word = value.strip()
        if word in ('Connected', 'Disconnected', 'Active', 'Inactive', 'Primary', 'Backup', 'WAN', 'WISP', 'Cellular', '4G', 'Online', 'Offline'):
            self.status_words.add(word)


def main():
    client = CudyClient()
    client.connect()
    paths = [
        'admin/network/gcom/sms/smsnew?nomodal=&iface=4g',
        'admin/network/gcom/status',
        'admin/network/wan/status',
        'admin/network/wireless/wds/status?wisp=',
    ]
    for path in paths:
        result = {'page': path}
        try:
            request = urllib.request.Request(client.root + path, headers={'X-Requested-With': 'XMLHttpRequest'})
            with client.opener.open(request, timeout=10) as response:
                parser = Structure()
                document = response.read(524289)
                if len(document) > 524288:
                    raise ValueError('Page too large')
                parser.feed(document.decode('utf-8', 'replace'))
                result.update(http=response.status, forms=parser.forms, fields=parser.fields, status_words=sorted(parser.status_words))
        except Exception as error:
            result['error_type'] = type(error).__name__
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'connection_error_type': type(error).__name__}))
        raise SystemExit(1)
