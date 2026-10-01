"""Authenticated backup packaging; CLI only extracts to a new directory."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import zipfile
from cryptography.fernet import Fernet

MAX_BYTES = 32 * 1024 * 1024
FILES = ('pager.sqlite', 'pager.json', 'manifest.json')


def cipher(key_path):
    path = Path(key_path)
    if path.stat().st_size > 256:
        raise ValueError('Ugyldig nøglefil')
    return Fernet(path.read_bytes().strip())


def package(database, configuration, name):
    paths = (Path(database), Path(configuration))
    if sum(p.stat().st_size for p in paths) > MAX_BYTES:
        raise ValueError('Backup overstiger den understøttede grænse på 32 MiB')
    contents = [p.read_bytes() for p in paths]
    manifest = {'format': 1, 'backup': name, 'sha256': {filename: hashlib.sha256(content).hexdigest() for filename, content in zip(FILES, contents)}}
    contents.append(json.dumps(manifest, sort_keys=True).encode())
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for filename, content in zip(FILES, contents):
            # Repeated attempts create the same plaintext bundle.
            info = zipfile.ZipInfo(filename, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return stream.getvalue()


def unpack(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.infolist()
        if len(members) != len(FILES) or set(archive.namelist()) != set(FILES) or sum(m.file_size for m in members) > MAX_BYTES + 4096:
            raise ValueError('Ugyldigt backuparkiv')
        values = {name: archive.read(name) for name in FILES}
    manifest = json.loads(values['manifest.json'])
    if manifest.get('format') != 1 or any(manifest['sha256'][name] != hashlib.sha256(values[name]).hexdigest() for name in FILES[:2]):
        raise ValueError('Backupens kontrolsummer passer ikke')
    return values


def decrypt_to_directory(source, key, destination):
    source = Path(source)
    if source.stat().st_size > (MAX_BYTES + 8192) * 2:
        raise ValueError('Krypteret backup er for stor')
    values = unpack(cipher(key).decrypt(source.read_bytes()))
    target = Path(destination)
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, content in values.items():
        with (target / name).open('xb') as output:
            os.chmod(output.name, 0o600)
            output.write(content)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SBR Pager: nøgle eller isoleret dekryptering')
    commands = parser.add_subparsers(dest='action', required=True)
    key_command = commands.add_parser('key')
    key_command.add_argument('--output', required=True)
    decrypt = commands.add_parser('decrypt')
    decrypt.add_argument('--input', required=True)
    decrypt.add_argument('--key', required=True)
    decrypt.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.action == 'key':
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(Fernet.generate_key() + b'\n')
        print('Nøglefil oprettet. Gem også en sikker kopi uden for serveren.')
    else:
        print('Udpakket til ny mappe:', decrypt_to_directory(args.input, args.key, args.output))
