import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path, data, mode=0o600):
    descriptor, filename = tempfile.mkstemp(dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def workspace_for(package):
    if package == 'sv-mythic':
        return Path('/var/lib/svent/mythic')
    base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    return base / 'svent/havoc'


def safe_target(workspace, relative):
    target = workspace / relative
    for ancestor in (target, *target.parents):
        if ancestor == workspace.parent:
            break
        if ancestor.is_symlink():
            raise RuntimeError('Workspace symlink is not allowed: ' + str(ancestor))
    if not target.resolve().is_relative_to(workspace.resolve()):
        raise RuntimeError('Workspace path escaped its root')
    return target


def initialize(package, prefix, workspace):
    manifest = json.loads((prefix / 'upstream.json').read_text())
    if package == 'sv-mythic' and os.geteuid() != 0:
        raise RuntimeError('Run Mythic management commands with sudo')
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    if workspace.is_symlink():
        raise RuntimeError('Workspace root must not be a symlink')
    workspace.chmod(0o700)
    lock_path = safe_target(workspace, '.svent.lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = safe_target(workspace, '.svent-managed.json')
        state = json.loads(state_path.read_text()) if state_path.is_file() else {}
        if state.get('version') == manifest['version']:
            return
        files = dict(state.get('files', {}))
        template = prefix / 'template'
        for directory in template.rglob('*'):
            if directory.is_dir() and not directory.is_symlink():
                safe_target(workspace, directory.relative_to(template)).mkdir(parents=True, exist_ok=True)
        if package == 'sv-havoc':
            safe_target(workspace, 'data').mkdir(exist_ok=True)
        for relative, link in manifest.get('archive_symlinks', {}).items():
            parent = safe_target(workspace, Path(relative).parent)
            parent.mkdir(parents=True, exist_ok=True)
            target = parent / Path(relative).name
            if target.is_symlink() and os.readlink(target) == link:
                continue
            if target.exists() or target.is_symlink():
                print('[info] Preserved existing path: ' + str(target), file=sys.stderr)
                continue
            target.symlink_to(link)
        sources = [(path.relative_to(template), path) for path in template.rglob('*') if path.is_file() and not path.is_symlink()]
        if package == 'sv-mythic':
            sources.append((Path('mythic-cli'), prefix / 'mythic-cli'))
        else:
            sources.extend([(Path('havoc'), prefix / 'bin/havoc'),
                            (Path('client/Havoc'), prefix / 'bin/Havoc')])
        for relative, source in sources:
            target = safe_target(workspace, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if relative.as_posix() in ('profiles/havoc.yaotl', 'client/config.toml'):
                    continue
                if not target.is_file() or files.get(relative.as_posix()) != checksum(target):
                    print('[info] Preserved existing file: ' + str(target), file=sys.stderr)
                    continue
            data = source.read_bytes()
            if package == 'sv-havoc' and relative.as_posix() == 'profiles/havoc.yaotl':
                data = data.replace(b'password1234', secrets.token_urlsafe(24).encode())
                data = data.replace(b'service-password', secrets.token_urlsafe(24).encode())
            mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
            if relative.as_posix() in ('profiles/havoc.yaotl', 'client/config.toml'):
                mode = 0o600
            atomic_write(target, data, mode)
            files[relative.as_posix()] = checksum(target)
        if package == 'sv-mythic':
            configuration = safe_target(workspace, '.env')
            if not configuration.exists():
                settings = {'COMPOSE_PROJECT_NAME': 'svent_mythic',
                            'NGINX_BIND_LOCALHOST_ONLY': 'true',
                            'POSTGRES_BIND_LOCALHOST_ONLY': 'true',
                            'MYTHIC_ADMIN_PASSWORD': secrets.token_urlsafe(32),
                            'POSTGRES_PASSWORD': secrets.token_urlsafe(32),
                            'RABBITMQ_PASSWORD': secrets.token_urlsafe(32),
                            'JWT_SECRET': secrets.token_urlsafe(48),
                            'HASURA_SECRET': secrets.token_urlsafe(32)}
                atomic_write(configuration, ''.join(f'{key}="{value}"\n' for key, value in settings.items()).encode())
        atomic_write(state_path, (json.dumps({'version': manifest['version'], 'files': files}, indent=2) + '\n').encode())


def launch(package, command, arguments):
    prefix = Path('/opt') / package
    if command != 'initialize' and package == 'sv-mythic' and (not arguments or arguments[0] in ('--help', '-h', 'help', 'version')):
        os.chdir(prefix / 'template')
        os.execv(str(prefix / 'mythic-cli'), ['mythic-cli', *(arguments or ['--help'])])
    workspace = workspace_for(package)
    initialize(package, prefix, workspace)
    if command == 'initialize':
        print('[+] Workspace prepared: ' + str(workspace))
        return
    for index, argument in enumerate(arguments):
        if index and arguments[index - 1] in ('--profile', '-p', '--config'):
            arguments[index] = str(Path(argument).absolute())
        elif argument.startswith(('--profile=', '--config=')):
            key, value = argument.split('=', 1)
            arguments[index] = key + '=' + str(Path(value).absolute())
    os.chdir(workspace)
    if package == 'sv-havoc':
        executable = workspace / ('client/Havoc' if command == 'havoc-client' else 'havoc')
    else:
        executable = workspace / 'mythic-cli'
    env = os.environ.copy()
    for name in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP'):
        env.pop(name, None)
    env['PYTHONNOUSERSITE'] = '1'
    os.execve(str(executable), [command, *arguments], env)


if __name__ == '__main__':
    try:
        launch(sys.argv[1], sys.argv[2], sys.argv[3:])
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit('[-] ' + str(error))
