import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'upstream.json').read_text())
BUILD = ROOT / '.build' / MANIFEST['ref']
SOURCE = BUILD / 'source'
CACHE = ROOT / '.build' / 'downloads'
PACKAGE = MANIFEST['package']


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def fetch(item, name):
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / name
    if target.is_file() and checksum(target) == item['sha256']:
        return target
    if not item['url'].startswith('https://'):
        raise RuntimeError('Downloads require HTTPS')
    request = urllib.request.Request(item['url'], headers={'User-Agent': 'Svent-packaging'})
    descriptor, temporary_name = tempfile.mkstemp(dir=CACHE)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as stream, urllib.request.urlopen(request, timeout=120) as response:
            if not response.url.startswith('https://'):
                raise RuntimeError('Download redirected outside HTTPS')
            shutil.copyfileobj(response, stream)
        if checksum(temporary_path) != item['sha256']:
            raise RuntimeError('Download checksum mismatch: ' + name)
        if item.get('size') and temporary_path.stat().st_size != item['size']:
            raise RuntimeError('Download size mismatch: ' + name)
        temporary_path.replace(target)
        return target
    finally:
        temporary_path.unlink(missing_ok=True)


def extract(archive_path, destination, allowed_symlinks=None):
    if destination.is_dir():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='extract-', dir=destination.parent))
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        prefixes = {member.name.split('/')[0] for member in members}
        if len(prefixes) != 1:
            raise RuntimeError('Archive must have one root directory')
        for member in members:
            pieces = member.name.split('/')[1:]
            if not pieces or not any(pieces):
                continue
            if '..' in pieces or member.name.startswith('/') or member.isdev() or member.islnk():
                raise RuntimeError('Unsafe archive member: ' + member.name)
            target = staging.joinpath(*pieces)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o755)
            elif member.issym():
                relative = '/'.join(pieces)
                if (allowed_symlinks or {}).get(relative) == member.linkname:
                    if any(candidate.name.startswith(member.name.rstrip('/') + '/') for candidate in members):
                        raise RuntimeError('Archive contains files below an external symlink')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(member.linkname)
                    continue
                resolved = (target.parent / member.linkname).resolve()
                if not resolved.is_relative_to(staging.resolve()):
                    raise RuntimeError('Unsafe archive symlink: ' + member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            else:
                raise RuntimeError('Unsupported archive member: ' + member.name)
    staging.rename(destination)


def run(arguments, directory, environment=None, capture=False):
    print('[+] ' + ' '.join(map(str, arguments)), flush=True)
    return subprocess.run(list(map(str, arguments)), cwd=directory, env=environment,
                          check=True, text=True, capture_output=capture, timeout=3600)


def patch_once(path, old, new):
    source = path.read_text()
    if new in source:
        return
    if old not in source:
        raise RuntimeError('Source patch does not match: ' + str(path))
    path.write_text(source.replace(old, new))


def environment(go_binary=None):
    result = os.environ.copy()
    for key in ('GOFLAGS', 'GOWORK', 'GOPATH', 'GOMODCACHE', 'GOCACHE', 'GOROOT',
                'GOOS', 'GOARCH', 'PYTHONHOME', 'PYTHONPATH'):
        result.pop(key, None)
    result.update({'GOPATH': str(BUILD / 'go'), 'GOMODCACHE': str(BUILD / 'go/modules'),
                   'GOCACHE': str(BUILD / 'go/cache'), 'GOTOOLCHAIN': 'local',
                   'GOWORK': 'off', 'GOPROXY': 'https://proxy.golang.org',
                   'GOSUMDB': 'sum.golang.org', 'GOOS': 'linux', 'GOARCH': 'amd64',
                   'GOMAXPROCS': str(max(1, int(result.get('SVENT_BUILD_JOBS', '2'))))})
    if go_binary:
        result['PATH'] = str(go_binary.parent) + os.pathsep + result.get('PATH', '')
    return result


def module_sources(go, directory, env):
    toolchain_root = Path(run([go, 'env', 'GOROOT'], directory, env, True).stdout.strip())
    notices = BUILD / 'toolchain-notices'
    notices.mkdir(exist_ok=True)
    found = False
    for name in ('LICENSE', 'PATENTS', 'AUTHORS'):
        source = toolchain_root / name
        if source.is_file():
            shutil.copy2(source, notices / name)
            found = found or name == 'LICENSE'
    if not found:
        fallback = Path('/usr/share/doc/golang-go/copyright')
        if not fallback.is_file():
            raise RuntimeError('Go toolchain license notice is missing')
        shutil.copy2(fallback, notices / 'copyright')
    result = run([go, 'mod', 'download', '-json', 'all'], directory, env, True)
    decoder = json.JSONDecoder()
    text = result.stdout.lstrip()
    records = []
    folder = BUILD / 'module-sources'
    folder.mkdir(exist_ok=True)
    while text:
        record, offset = decoder.raw_decode(text)
        text = text[offset:].lstrip()
        if record.get('Error'):
            raise RuntimeError(str(record['Error']))
        if not record.get('Zip'):
            continue
        archive = Path(record['Zip'])
        identifier = hashlib.sha256((record['Path'] + '@' + record['Version']).encode()).hexdigest()
        target = folder / (identifier + '.zip')
        shutil.copy2(archive, target)
        records.append({'module': record['Path'], 'version': record['Version'],
                        'sum': record.get('Sum'), 'source': target.name, 'sha256': checksum(target)})
    (folder / 'manifest.json').write_text(json.dumps(records, indent=2) + '\n')


def prepare():
    BUILD.mkdir(parents=True, exist_ok=True)
    archive = fetch(MANIFEST['source'], 'source-' + MANIFEST['ref'] + '.tar.gz')
    extract(archive, SOURCE, MANIFEST.get('archive_symlinks'))
    if not (SOURCE / 'LICENSE').is_file():
        raise RuntimeError('Upstream license is missing')
    if PACKAGE == 'sv-havoc':
        for component in MANIFEST['components']:
            downloaded = fetch(component, component['name'] + '-' + component['ref'] + '.tar.gz')
            target = SOURCE / 'client/external' / component['name']
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
            extract(downloaded, target)
        cmake = SOURCE / 'client/CMakeLists.txt'
        data = cmake.read_text()
        data = data.replace('set( CMAKE_CPP_COMPILER /usr/bin/x86_64-w64-mingw32-g++ )', '')
        data = data.replace('set( CMAKE_C_COMPILER /usr/bin/x86_64-w64-mingw32-gcc )', '')
        data = data.replace('set( CMAKE_RUNTIME_OUTPUT_DIRECTORY .. )',
                            'set( CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}" )')
        cmake.write_text(data)
    elif PACKAGE == 'sv-mythic':
        patch_once(SOURCE / 'Mythic_CLI/src/cmd/rootCmd.go',
                   'func init() {\n',
                   'func init() {\n\tfor _, argument := range os.Args[1:] {\n\t\tif argument == "--help" || argument == "-h" { return }\n\t}\n\tif len(os.Args) > 1 && (os.Args[1] == "help" || os.Args[1] == "version") { return }\n')


def build():
    prepare()
    jobs = str(max(1, int(os.environ.get('SVENT_BUILD_JOBS', '2'))))
    if PACKAGE == 'sv-havoc':
        toolchain = MANIFEST['toolchain']
        archive = fetch(toolchain, toolchain['filename'])
        extract(archive, BUILD / 'toolchain')
        go = BUILD / 'toolchain/bin/go'
        env = environment(go)
        env['CGO_ENABLED'] = '1'
        directory = SOURCE / 'teamserver'
        module_sources(go, directory, env)
        run([go, 'build', '-mod=readonly', '-trimpath', '-ldflags=-s -w',
             '-o', SOURCE / 'havoc', 'main.go'], directory, env)
        run(['cmake', '-S', SOURCE / 'client', '-B', BUILD / 'client-build',
             '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_C_COMPILER=/usr/bin/gcc',
             '-DCMAKE_CXX_COMPILER=/usr/bin/g++'], ROOT, env)
        run(['cmake', '--build', BUILD / 'client-build', '--parallel', jobs], ROOT, env)
    else:
        go = Path(shutil.which('go') or '/usr/bin/go')
        env = environment()
        env['CGO_ENABLED'] = '0'
        directory = SOURCE / 'Mythic_CLI/src'
        module_sources(go, directory, env)
        run([go, 'build', '-mod=readonly', '-trimpath', '-ldflags=-s -w',
             '-o', SOURCE / 'mythic-cli', '.'], directory, env)


def test():
    if PACKAGE == 'sv-havoc':
        run([SOURCE / 'havoc', '--help'], SOURCE)
        env = os.environ.copy()
        env['QT_QPA_PLATFORM'] = 'offscreen'
        result = subprocess.run([str(SOURCE / 'client/Havoc'), '--help'], cwd=SOURCE,
                                env=env, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or 'config' not in result.stdout + result.stderr:
            raise RuntimeError('Havoc client help failed: ' + result.stdout + result.stderr)
    else:
        run([SOURCE / 'mythic-cli', '--help'], SOURCE)
        run([SOURCE / 'mythic-cli', 'version'], SOURCE)


def copy_tree(source, destination, excluded=()):
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*excluded))


def install(staging):
    staging = staging.resolve()
    if staging != (ROOT / 'debian' / PACKAGE).resolve():
        raise RuntimeError('Unexpected package staging directory')
    prefix = staging / 'opt' / PACKAGE
    prefix.mkdir(parents=True, exist_ok=True)
    template = prefix / 'template'
    if PACKAGE == 'sv-havoc':
        (prefix / 'bin').mkdir(exist_ok=True)
        shutil.copy2(SOURCE / 'havoc', prefix / 'bin/havoc')
        shutil.copy2(SOURCE / 'client/Havoc', prefix / 'bin/Havoc')
        for folder in ('client', 'data', 'payloads', 'profiles'):
            excluded = ('Build', 'external', 'Havoc', 'src', 'include', 'CMakeLists.txt') if folder == 'client' else ()
            copy_tree(SOURCE / folder, template / folder, excluded)
        (template / 'client/config.toml').write_text('[font]\nsize = 9\nfamily = "Monospace"\n\n[scripts]\nfiles = []\n')
        profile = template / 'profiles/havoc.yaotl'
        text = profile.read_text().replace('Host = "0.0.0.0"', 'Host = "127.0.0.1"')
        text = text.replace('data/x86_64-w64-mingw32-cross/bin/x86_64-w64-mingw32-gcc', '/usr/bin/x86_64-w64-mingw32-gcc')
        text = text.replace('data/i686-w64-mingw32-cross/bin/i686-w64-mingw32-gcc', '/usr/bin/i686-w64-mingw32-gcc')
        text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#')) + '\n'
        profile.write_text(text)
    else:
        copy_tree(SOURCE, template, ('.git', 'mythic-cli', 'node_modules', '.github'))
        shutil.copy2(SOURCE / 'mythic-cli', prefix / 'mythic-cli')
    shutil.copy2(ROOT / 'upstream.json', prefix / 'upstream.json')
    document = staging / 'usr/share/doc' / PACKAGE
    document.mkdir(parents=True, exist_ok=True)
    archives = document / 'source-archives'
    archives.mkdir(exist_ok=True)
    source_archive = fetch(MANIFEST['source'], 'source-' + MANIFEST['ref'] + '.tar.gz')
    shutil.copy2(source_archive, archives / 'upstream.tar.gz')
    for component in MANIFEST.get('components', []):
        archive = fetch(component, component['name'] + '-' + component['ref'] + '.tar.gz')
        shutil.copy2(archive, archives / (component['name'] + '.tar.gz'))
    copy_tree(BUILD / 'module-sources', document / 'module-sources')
    copy_tree(BUILD / 'toolchain-notices', document / 'toolchain-notices')
    shutil.copy2(SOURCE / 'LICENSE', document / 'upstream-license')
    shutil.copy2(ROOT / 'debian/build.py', document / 'build.py')
    shutil.copy2(ROOT / 'upstream.json', document / 'upstream.json')
    for path in (ROOT / 'files').iterdir():
        if path.name == 'runtime.py':
            target = staging / 'usr/libexec/svent' / (PACKAGE + '-runtime.py')
        elif path.suffix == '.desktop':
            target = staging / 'usr/share/applications' / path.name
        elif path.suffix == '.svg':
            target = staging / 'usr/share/icons/hicolor/scalable/apps' / path.name
        else:
            target = staging / 'usr/bin' / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        target.chmod(0o644 if path.suffix in ('.desktop', '.svg', '.py') else 0o755)
    copy_tree(ROOT / 'catalog.d', staging / 'usr/share/svent/catalog.d')


if __name__ == '__main__':
    command = sys.argv[1]
    if command == 'build':
        build()
    elif command == 'test':
        test()
    elif command == 'install':
        install(Path(sys.argv[2]))
    else:
        raise SystemExit('Unknown build command')
