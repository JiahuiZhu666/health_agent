#!/usr/bin/env python3
"""Container startup: private state, explicit access settings, optional data prep.

Importing this module performs no I/O, downloads or model calls. The production
server and all statistical scripts remain in the application source directory.
"""
import argparse
import os
from pathlib import Path
import pwd
import sys
import threading
from urllib.parse import urlsplit

CODE_ROOT = Path(__file__).resolve().parents[1]
STATE_SUBDIRECTORIES = ('data', 'runs', 'conversations', '.cache')


def enabled(value):
    value = str(value).strip().lower()
    if value in ('true', '1', 'yes', 'on'):
        return True
    if value in ('false', '0', 'no', 'off', ''):
        return False
    raise ValueError('NHIS_AUTO_DOWNLOAD_DATA must be true or false.')


def configure_environment(environ):
    """Validate deployment inputs without printing or copying their values."""
    try:
        port = int(environ.get('PORT', '10000'))
    except (TypeError, ValueError) as error:
        raise ValueError('PORT must be an integer.') from error
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be between 1 and 65535.')
    environ['PORT'] = str(port)
    environ.setdefault('NHIS_HOST', '0.0.0.0')
    environ.setdefault('NHIS_STATE_DIR', '/var/lib/nhis')
    if len(environ.get('WEB_AUTH_SECRET', '').strip()) < 32:
        raise ValueError('Set a server-only WEB_AUTH_SECRET of at least 32 characters before startup.')
    download = enabled(environ.get('NHIS_AUTO_DOWNLOAD_DATA', 'false'))

    # Platform-provided identity is trusted configuration, never a request header.
    # For a custom domain, NHIS_PUBLIC_URL explicitly selects that HTTPS origin.
    public_url = environ.get('NHIS_PUBLIC_URL') or environ.get('RENDER_EXTERNAL_URL')
    if public_url:
        parsed = urlsplit(public_url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('NHIS_PUBLIC_URL must be an HTTPS origin without credentials or a path.')
        try:
            parsed.port
        except ValueError as error:
            raise ValueError('NHIS_PUBLIC_URL has an invalid port.') from error
        environ.setdefault('NHIS_ALLOWED_HOSTS', parsed.netloc)
        environ.setdefault('NHIS_ALLOWED_ORIGINS', 'https://' + parsed.netloc)
    if not environ.get('NHIS_ALLOWED_HOSTS') or not environ.get('NHIS_ALLOWED_ORIGINS'):
        raise ValueError('Set NHIS_PUBLIC_URL or explicit NHIS_ALLOWED_HOSTS and NHIS_ALLOWED_ORIGINS.')
    return port, download


def prepare_state(path):
    state = Path(path).expanduser().resolve()
    if state == CODE_ROOT or state in CODE_ROOT.parents or CODE_ROOT in state.parents:
        raise ValueError('NHIS_STATE_DIR must be separate from the application source directory.')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    children = []
    for name in STATE_SUBDIRECTORIES:
        child = state / name
        if child.is_symlink():
            raise ValueError('Persistent state directories must not be symbolic links.')
        child.mkdir(exist_ok=True, mode=0o700)
        children.append(child)
    # A newly attached Render disk may be root-owned. Existing application files
    # retain the same fixed UID across releases; no recursive root traversal occurs.
    if os.geteuid() == 0:
        account = pwd.getpwnam('nhis')
        for target in (state, *children):
            os.chown(str(target), account.pw_uid, account.pw_gid)
            target.chmod(0o700)
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    if not os.access(str(state), os.W_OK | os.X_OK):
        raise ValueError('The application user cannot write to NHIS_STATE_DIR.')
    return state


def download_data(state):
    """Fetch only the fixed official sources through the existing validator."""
    from nhis_agent.data import download
    print('Official NHIS data preparation started; chat remains available.', flush=True)
    try:
        report = download(state)
        success = report.get('ready') is True and report.get('income_ready') is True
        print('Official NHIS data preparation ' + ('completed and passed validation.' if success else
              'did not pass all checks; survey analysis remains unavailable.'), flush=True)
        return success
    except Exception:
        # Full raw responses and exception payloads do not enter deployment logs.
        print('Official NHIS data preparation failed; survey analysis remains unavailable.', flush=True)
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download-data', action='store_true', help='Prepare official data on the mounted runtime disk, then exit')
    args = parser.parse_args(argv)
    sys.path.insert(0, str(CODE_ROOT))
    try:
        port, auto_download = configure_environment(os.environ)
        state = prepare_state(os.environ['NHIS_STATE_DIR'])
    except (KeyError, OSError, ValueError) as error:
        parser.exit(1, 'Deployment configuration error: {}\n'.format(error))
    os.umask(0o077)
    if args.download_data:
        return 0 if download_data(state) else 1
    if auto_download:
        threading.Thread(target=download_data, args=(state,), name='official-data-preparation', daemon=True).start()
    # Keep the listener in the foreground. The server reads the validated HOST,
    # state and origin settings from its environment; only the numeric port is CLI.
    import server
    sys.argv = [str(CODE_ROOT / 'server.py'), '--port', str(port)]
    server.main()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
