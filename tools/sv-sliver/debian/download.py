import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import urllib.request


PREFIX = Path('/opt/sv-sliver')
MANIFEST_PATH = Path('/usr/share/svent/downloads/sliver.json')


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify(path, item):
    if not path.is_file() or path.is_symlink():
        raise RuntimeError('Release file is missing: ' + str(path))
    if path.stat().st_size != item['size'] or checksum(path) != item['sha256']:
        raise RuntimeError('Release checksum or size mismatch: ' + str(path))
    with path.open('rb') as stream:
        header = stream.read(20)
    if header[:6] != b'\x7fELF\x02\x01' or struct.unpack('<H', header[18:20])[0] != 62:
        raise RuntimeError('Release is not a Linux amd64 ELF executable')
    if not os.access(path, os.X_OK):
        raise RuntimeError('Release executable permissions are missing')


def configure(manifest):
    if os.geteuid() != 0:
        raise RuntimeError('Release installation requires root')
    if PREFIX.is_symlink():
        raise RuntimeError('Release prefix must not be a symlink')
    PREFIX.mkdir(parents=True, exist_ok=True)
    with (PREFIX / '.download.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        release = PREFIX / 'releases' / manifest['version']
        if (PREFIX / 'releases').is_symlink() or release.is_symlink():
            raise RuntimeError('Release directory must not be a symlink')
        release.mkdir(parents=True, exist_ok=True)
        for item in manifest['files']:
            target = release / item['name']
            if target.exists():
                verify(target, item)
                continue
            if target.is_symlink() or not item['url'].startswith('https://'):
                raise RuntimeError('Unsafe release target or URL')
            descriptor, filename = tempfile.mkstemp(prefix='download-', dir=release)
            temporary = Path(filename)
            try:
                request = urllib.request.Request(item['url'], headers={'User-Agent': 'Svent-packaging'})
                print('[+] Downloading ' + item['name'], flush=True)
                with os.fdopen(descriptor, 'wb') as output, urllib.request.urlopen(request, timeout=180) as response:
                    if not response.url.startswith('https://'):
                        raise RuntimeError('Release redirected outside HTTPS')
                    shutil.copyfileobj(response, output)
                temporary.chmod(0o755)
                verify(temporary, item)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        current = PREFIX / 'current'
        if current.exists() and not current.is_symlink():
            raise RuntimeError('Existing current release path was preserved')
        temporary_link = PREFIX / ('current-' + str(os.getpid()))
        try:
            temporary_link.symlink_to(Path('releases') / manifest['version'])
            temporary_link.replace(current)
        finally:
            temporary_link.unlink(missing_ok=True)
        print('[+] Sliver release verified and installed')


def check(manifest):
    expected = (PREFIX / 'releases' / manifest['version']).resolve()
    if not (PREFIX / 'current').is_symlink() or (PREFIX / 'current').resolve() != expected:
        raise RuntimeError('Active release does not match the package manifest')
    for item in manifest['files']:
        verify(expected / item['name'], item)
    print('[+] Sliver release checksums verified successfully')


if __name__ == '__main__':
    try:
        manifest = json.loads(MANIFEST_PATH.read_text())
        if sys.argv[1] == 'configure':
            configure(manifest)
        elif sys.argv[1] == 'check':
            check(manifest)
        else:
            raise RuntimeError('Unknown release command')
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit('[-] ' + str(error))
