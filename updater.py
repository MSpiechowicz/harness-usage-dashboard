#!/usr/bin/env python3
"""Check public releases; delegate explicit upgrades to OMP's native plugin manager."""
from contextlib import contextmanager
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from preferences import agent_dir, resolve_profile

ROOT = Path(__file__).resolve().parent
REPOSITORY = 'MSpiechowicz/harness-usage-dashboard'
RELEASE_API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
RELEASE_BASE = f'https://github.com/{REPOSITORY}/releases/tag/'
MARKETPLACE = 'harness-usage-dashboard'
PLUGIN_ID = 'harness-usage-dashboard@' + MARKETPLACE
CACHE_SECONDS = 24 * 60 * 60
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', re.ASCII)


class UpdateError(RuntimeError):
    pass


class NotManagedError(UpdateError):
    pass


SOURCE_CHECKOUT_MESSAGE = (
    'This source checkout is not managed by OMP. Update it with `git pull --ff-only` '
    'and `python3 install.py`, or migrate to the marketplace for native updates.'
)


def version_tuple(value):
    if not isinstance(value, str) or len(value) > 80 or VERSION.fullmatch(value) is None:
        raise UpdateError('Expected a stable MAJOR.MINOR.PATCH version.')
    return tuple(map(int, value.split('.')))


def package_version(content):
    try:
        data = json.loads(content)
        version = data['version']
        version_tuple(version)
        return version
    except (ValueError, TypeError, KeyError) as exc:
        raise UpdateError('package.json does not contain a valid stable version.') from exc


def current_version(root):
    try:
        return package_version((root / 'package.json').read_text(encoding='utf-8'))
    except (OSError, UnicodeError) as exc:
        raise UpdateError('Cannot read installed package.json; use a complete dashboard installation.') from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpdateError('GitHub redirected the release request; refusing a different endpoint.')


def latest_release():
    # No credential files, auth headers, proxy credentials or environment tokens are used.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(RELEASE_API, headers={
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'harness-usage-dashboard-updater',
    })
    try:
        with opener.open(request, timeout=5) as response:
            content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise UpdateError('GitHub release response exceeded the size limit.')
        data = json.loads(content)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return None
        if exc.code in (403, 429):
            raise UpdateError('GitHub public API rate limit or access restriction; try again later.') from exc
        raise UpdateError(f'GitHub release request failed (HTTP {exc.code}).') from exc
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise UpdateError('Cannot retrieve the public GitHub release; check connectivity and try again.') from exc
    if not isinstance(data, dict) or data.get('draft') is not False or data.get('prerelease') is not False:
        raise UpdateError('GitHub did not return a published stable release.')
    tag = data.get('tag_name')
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise UpdateError('Latest release does not use a stable vMAJOR.MINOR.PATCH tag.')
    version_tuple(tag[1:])
    return {'version': tag[1:], 'tag': tag, 'url': RELEASE_BASE + tag}


def cache_path(profile):
    return agent_dir(profile) / 'usage-dashboard-update.json'


def read_cache(profile):
    try:
        path = cache_path(profile)
        if path.is_symlink():
            return False, None
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return False, None
        age = time.time() - data['checkedAt']
        if data.get('repository') != REPOSITORY or not 0 <= age < CACHE_SECONDS:
            return False, None
        release = data['release']
        if release is not None:
            if not isinstance(release, dict):
                return False, None
            version_tuple(release['version'])
            if release['tag'] != 'v' + release['version'] or release['url'] != RELEASE_BASE + release['tag']:
                return False, None
        return True, release
    except (OSError, ValueError, TypeError, KeyError, UpdateError):
        return False, None


def write_cache(profile, release):
    path = cache_path(profile)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            return
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.usage-dashboard-update-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'repository': REPOSITORY, 'checkedAt': time.time(), 'release': release}, stream)
            stream.write('\n')
        temporary.replace(path)
    except OSError:
        # Cache availability must not turn a successful check/update into a failure.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def result(current, release):
    return {
        'currentVersion': current,
        'latestVersion': release['version'] if release else None,
        'updateAvailable': release is not None and version_tuple(release['version']) > version_tuple(current),
        'releaseUrl': release['url'] if release else None,
    }


def check(profile=None, cached=False, root=None, managed=False):
    root = root or ROOT
    current = current_version(root)
    valid, release = read_cache(profile) if cached else (False, None)
    if not valid:
        release = latest_release()
        write_cache(profile, release)
    response = result(current, release)
    if response['updateAvailable'] and managed:
        try:
            managed_install(profile, root)
        except NotManagedError:
            response['updateAvailable'] = False
            response['message'] = SOURCE_CHECKOUT_MESSAGE
    elif release is None:
        response['message'] = 'No published stable GitHub release is available.'
    return response


def native(profile, *args, timeout=8):
    """Use the current project and explicit profile, never an interactive shell."""
    command = ['omp', '--profile', resolve_profile(profile), 'plugin', *args]
    environment = dict(os.environ, NO_COLOR='1', GIT_TERMINAL_PROMPT='0')
    try:
        with subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
            try:
                stdout, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise UpdateError('OMP plugin operation timed out. Inspect `omp plugin list` before retrying; no fallback was attempted.') from exc
            if process.returncode:
                # Native output may contain credentials, remote URLs or terminal controls.
                raise UpdateError(f'OMP plugin {args[0]} failed. Run the same native command manually for details; no fallback was attempted.')
    except FileNotFoundError as exc:
        raise UpdateError('OMP is not available on PATH. Install a version supporting `omp plugin upgrade` and retry.') from exc
    return stdout


def installed_plugins(profile):
    try:
        data = json.loads(native(profile, 'list', '--json'))
        if not isinstance(data, dict) or not isinstance(data.get('marketplace'), list):
            raise ValueError('Unsupported plugin listing')
        return data['marketplace']
    except (ValueError, TypeError) as exc:
        raise UpdateError('Cannot read native OMP marketplace installations. Upgrade OMP to a version supporting `omp plugin list --json`.') from exc


def managed_install(profile, root=None, scope=None):
    matches = []
    for summary in installed_plugins(profile):
        if not isinstance(summary, dict) or summary.get('id') != PLUGIN_ID:
            continue
        if summary.get('scope') not in ('user', 'project') or summary.get('shadowedBy'):
            continue
        if scope is not None and summary['scope'] != scope:
            continue
        entries = summary.get('entries')
        # Ambiguous registry entries are not a safe update target.
        if not isinstance(entries, list) or len(entries) != 1:
            continue
        entry = entries[0]
        if not isinstance(entry, dict) or entry.get('enabled') is False or entry.get('scope') != summary['scope']:
            continue
        path = entry.get('installPath')
        if not isinstance(path, str) or not Path(path).is_absolute():
            continue
        if root is not None and Path(path).resolve() != root.resolve():
            continue
        if current_version(Path(path)) != entry.get('version'):
            raise UpdateError('Native plugin registry and installed package versions disagree. Inspect `omp plugin list` before updating.')
        matches.append((summary['scope'], str(Path(path).resolve()), entry['version']))
    if len(matches) != 1:
        raise NotManagedError(
            'This running dashboard is not an unambiguous active native marketplace installation. '
            'Legacy git/symlink checkouts are never overwritten. Follow Readme.MD installation: '
            'remove any previous integration, add the official marketplace, then '
            f'`omp plugin install {PLUGIN_ID}` and restart OMP.'
        )
    return matches[0]


@contextmanager
def update_lock(profile):
    path = cache_path(profile).with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError('Another dashboard update is running; wait for it to finish.') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def install_update(profile=None, root=None):
    root = root or ROOT
    with update_lock(profile):
        initial = managed_install(profile, root)
        current = current_version(root)
        release = latest_release()  # Never trust profile cache for installation.
        response = result(current, release)
        if not response['updateAvailable']:
            response.update(updated=False, message='No newer stable release is available.')
            write_cache(profile, release)
            return response
        native(profile, 'marketplace', 'update', MARKETPLACE, timeout=55)
        try:
            unchanged = managed_install(profile, root) == initial
        except UpdateError:
            unchanged = False
        if not unchanged:
            raise UpdateError('The native installation changed during the check; inspect it before retrying.')
        native(profile, 'upgrade', PLUGIN_ID, '--scope', initial[0], timeout=75)
        installed = managed_install(profile, scope=initial[0])
        if version_tuple(installed[2]) <= version_tuple(current):
            raise UpdateError('OMP did not install a newer stable version. The marketplace may not have published the release yet; try again later.')
        write_cache(profile, release)
        response = result(installed[2], release)
        response.update(updated=True, message=f'Updated to {installed[2]} using OMP plugin upgrade. Restart OMP to load the updated extension and dashboard.')
        return response


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'install'))
    parser.add_argument('--profile')
    parser.add_argument('--cached', action='store_true', help='reuse a successful public check for up to 24 hours')
    args = parser.parse_args(argv)
    if args.cached and args.action != 'check':
        parser.error('--cached is only supported for check')
    try:
        cache_path(args.profile)  # Validate profile before any mutation/network request.
        response = check(args.profile, args.cached, managed=True) if args.action == 'check' else install_update(args.profile)
    except (UpdateError, OSError, ValueError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, (UpdateError, ValueError)) else 'Local files could not be accessed safely; inspect permissions and checkout state.'
        message = ''.join(char if char.isprintable() else ' ' for char in message)
        print(f'Update failed: {message}', file=sys.stderr)
        return 1
    print(json.dumps(response))
    return 0


if __name__ == '__main__':
    sys.exit(main())
