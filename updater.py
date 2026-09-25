#!/usr/bin/env python3
"""Check public releases; update OMP plugins or an owned Claude source checkout."""

from contextlib import contextmanager
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import ssl
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
CANONICAL_ORIGINS = frozenset((
    f'https://github.com/{REPOSITORY}', f'https://github.com/{REPOSITORY}.git',
    f'git@github.com:{REPOSITORY}', f'git@github.com:{REPOSITORY}.git',
    f'ssh://git@github.com/{REPOSITORY}', f'ssh://git@github.com/{REPOSITORY}.git',
))
GIT_TIMEOUT = 20
FETCH_TIMEOUT = 75
REQUIRED_CLAUDE_FILES = (
    'claude_install.py', 'claude_launcher.py', 'claude_bridge.py', 'claude_usage_source.py',
    'dashboard.py', 'updater.py', 'preferences.py', 'host_adapters.py', 'session_usage.py',
    'usage_source.py',
)


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


def cache_path(profile, host='omp'):
    directory = agent_dir(None, host='claude') if host == 'claude' else agent_dir(profile)
    return directory / 'usage-dashboard-update.json'


def read_cache(profile, host='omp'):
    try:
        path = cache_path(profile, host)
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


def write_cache(profile, release, host='omp'):
    path = cache_path(profile, host)
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


def check_claude_update(root=None, cached=False):
    import claude_install

    root = root or ROOT
    claude_install.validate_owned_installation(root=root)
    current = current_version(root)
    valid, release = read_cache(None, host='claude') if cached else (False, None)
    if not valid:
        release = latest_release()
        write_cache(None, release, host='claude')
    response = result(current, release)
    if release is None:
        response['message'] = 'No published stable GitHub release is available.'
    return response


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


class PartialClaudeUpdate(UpdateError):
    def __init__(self, response):
        self.response = response
        super().__init__(response['message'])


def git(root, *args, timeout=GIT_TIMEOUT):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never',
                       SSH_ASKPASS='/bin/false', SSH_ASKPASS_REQUIRE='never')
    command = ['git', '-C', str(root), '-c', 'credential.helper=',
               '-c', 'core.hooksPath=/dev/null', '-c', 'http.followRedirects=false', *args]
    try:
        process = subprocess.run(command, env=environment, capture_output=True, text=True,
                                 timeout=timeout, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise UpdateError(f'Git {args[0]} timed out or is unavailable; inspect the checkout before retrying.') from exc
    if process.returncode:
        # Git stderr and command output can contain private remote URLs or credentials.
        raise UpdateError(f'Git {args[0]} failed; inspect the checkout before retrying.')
    return process.stdout.strip()


def claude_checkout_state(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or (root / '.git').is_symlink():
        raise UpdateError('Claude checkout is missing or linked; refusing source update.')
    if Path(git(root, 'rev-parse', '--show-toplevel')) != root.resolve():
        raise UpdateError('Claude integration must own exactly the Git checkout root.')
    if not (root / '.git').is_dir():
        raise UpdateError('Claude source update requires a standard checkout with its own .git directory.')
    configured = git(root, 'config', '--local', '--get-all', 'remote.origin.url').splitlines()
    effective = git(root, 'remote', 'get-url', '--all', 'origin').splitlines()
    if len(configured) != 1 or len(effective) != 1 or any(
            url not in CANONICAL_ORIGINS for url in (*configured, *effective)):
        raise UpdateError('Origin or its effective fetch URL is not the canonical GitHub repository.')
    branch = git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    if branch != 'main':
        raise UpdateError('Claude source update requires the main branch (not a detached or feature HEAD).')
    for operation in ('MERGE_HEAD', 'REBASE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD'):
        if (root / '.git' / operation).exists():
            raise UpdateError('Another Git operation is in progress; finish it before updating.')
    if any((root / '.git' / operation).exists() for operation in ('rebase-merge', 'rebase-apply', 'sequencer')):
        raise UpdateError('Another Git operation is in progress; finish it before updating.')
    if git(root, 'status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none'):
        raise UpdateError('Checkout has tracked, staged or untracked changes; clean it manually before updating.')
    try:
        if (root / 'package.json').is_symlink() or not (root / 'package.json').is_file():
            raise ValueError('linked or missing package')
        package = json.loads((root / 'package.json').read_text(encoding='utf-8'))
        if package['name'] != MARKETPLACE:
            raise ValueError('wrong package')
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise UpdateError('Checkout package.json is missing or has a different project identity.') from exc
    return git(root, 'rev-parse', '--verify', 'HEAD'), branch


@contextmanager
def claude_update_lock(root):
    path = Path(root) / '.git' / 'usage-dashboard-update.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError('Another Claude checkout update is running; wait for it to finish.') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _attested_tag(tag):
    """Resolve the GitHub tag ref independently of Git's transport configuration."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(
        f'https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{tag}',
        headers={
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'harness-usage-dashboard-updater',
            'Cache-Control': 'no-cache',
        })
    try:
        with opener.open(request, timeout=5) as response:
            content = response.read(64 * 1024 + 1)
        if len(content) > 64 * 1024:
            raise UpdateError('GitHub tag attestation response exceeded the size limit.')
        data = json.loads(content)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            raise UpdateError('Published release tag ref is missing from GitHub; refusing source update.') from exc
        if exc.code in (403, 429):
            raise UpdateError('GitHub tag attestation rate limit or access restriction; try again later.') from exc
        raise UpdateError(f'GitHub tag attestation failed (HTTP {exc.code}).') from exc
    except (OSError, ValueError, UnicodeError, urllib.error.URLError) as exc:
        raise UpdateError('Cannot verify the public GitHub release tag; check connectivity before retrying.') from exc
    if not isinstance(data, dict) or data.get('ref') != f'refs/tags/{tag}':
        raise UpdateError('GitHub returned a different release tag ref.')
    obj = data.get('object')
    if not isinstance(obj, dict) or obj.get('type') not in ('commit', 'tag') or (
            not isinstance(obj.get('sha'), str)
            or re.fullmatch(r'[0-9a-f]{40}', obj['sha'], flags=re.ASCII) is None):
        raise UpdateError('GitHub release tag attestation has an invalid object identity.')
    return {'sha': obj['sha'], 'type': obj['type']}


def _fetch_claude_release(root, tag):
    git(root, 'fetch', '--no-tags', '--no-recurse-submodules', 'origin',
        f'refs/tags/{tag}', timeout=FETCH_TIMEOUT)


def _release_commit(root, release):
    tag = release['tag']
    if tag != 'v' + release['version']:
        raise UpdateError('Published release tag and version disagree.')
    version_tuple(release['version'])
    attested = _attested_tag(tag)
    existing = git(root, 'for-each-ref', '--format=%(objectname)', f'refs/tags/{tag}')
    _fetch_claude_release(root, tag)
    fetched = git(root, 'rev-parse', '--verify', 'FETCH_HEAD')
    if fetched != attested['sha'] or git(root, 'cat-file', '-t', fetched) != attested['type']:
        raise UpdateError('Fetched tag does not match GitHub-attested release ref; refusing source update.')
    if existing and existing != fetched:
        raise UpdateError('Local release tag conflicts with the published release; refusing to replace it.')
    commit = git(root, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}')
    if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
        raise UpdateError('Fetched release did not resolve to a Git commit.')
    try:
        package = json.loads(git(root, 'show', f'{commit}:package.json'))
        catalog = json.loads(git(root, 'show', f'{commit}:.omp-plugin/marketplace.json'))
        plugins = [plugin for plugin in catalog['plugins'] if plugin['name'] == MARKETPLACE]
        valid = (package['name'] == MARKETPLACE and package_version(json.dumps(package)) == release['version']
                 and catalog['name'] == MARKETPLACE and len(plugins) == 1
                 and plugins[0]['version'] == release['version']
                 and plugins[0]['source'] == {'source': 'github', 'repo': REPOSITORY, 'ref': tag})
    except (ValueError, TypeError, KeyError, UpdateError) as exc:
        raise UpdateError('Release package or marketplace catalog is invalid.') from exc
    if not valid:
        raise UpdateError('Release tag, package version and marketplace catalog do not agree.')
    for path in (*REQUIRED_CLAUDE_FILES, 'package.json', '.omp-plugin/marketplace.json'):
        entry = git(root, 'ls-tree', commit, '--', path)
        if not entry.endswith(chr(9) + path) or entry.split(' ', 1)[0] not in ('100644', '100755'):
            raise UpdateError('Release is missing regular Claude integration or package files.')
    return commit


def refresh_claude_installer(root):
    """Use the newly fetched installer code, not this process's pre-merge import."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('PYTHON')}
    try:
        process = subprocess.run(
            [sys.executable, str(Path(root) / 'claude_install.py'), '--refresh'],
            cwd=root, env=environment, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=45)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise UpdateError('Claude installer refresh timed out or could not start; inspect the owned installation.') from exc
    if process.returncode:
        # Installer diagnostics may contain private paths; never copy stderr into updater JSON.
        raise UpdateError('Updated Claude installer could not refresh the owned integration.')


def install_claude_update(root=None):
    import claude_install

    root = root or ROOT
    initial = claude_install.validate_owned_installation(root=root)
    claude_checkout_state(root)
    with claude_update_lock(root):
        initial = claude_install.validate_owned_installation(root=root)
        head, branch = claude_checkout_state(root)
        current = current_version(root)
        release = latest_release()  # Never install from either host's cached release.
        response = result(current, release)
        if release is None:
            response.update(updated=False, message='No published stable GitHub release is available.')
            return response
        if not response['updateAvailable'] and release['version'] != current:
            response.update(updated=False, message='Installed checkout is newer than the latest stable release.')
            return response

        advanced = False
        refreshing = False
        try:
            if response['updateAvailable']:
                commit = _release_commit(root, release)
                if git(root, 'merge-base', head, commit) != head:
                    raise UpdateError('Release is not a fast-forward from this checkout; inspect history manually.')
                if claude_checkout_state(root) != (head, branch) or (
                        claude_install.validate_owned_installation(root=root) != initial):
                    raise UpdateError('Checkout or owned Claude integration changed during release inspection.')
                git(root, 'merge', '--ff-only', '--no-edit', commit, timeout=FETCH_TIMEOUT)
                advanced = True
                if git(root, 'rev-parse', 'HEAD') != commit or current_version(root) != release['version']:
                    raise UpdateError('Checkout did not reach the verified release commit/version.')
            else:
                commit = _release_commit(root, release)
                if commit != head:
                    raise UpdateError('Checkout HEAD is not the published release commit; inspect local history before retrying.')
                if claude_checkout_state(root) != (head, branch) or (
                        claude_install.validate_owned_installation(root=root) != initial):
                    raise UpdateError('Checkout or owned Claude integration changed during release inspection.')
            refreshing = True
            refresh_claude_installer(root)
            if claude_checkout_state(root) != (commit, branch) or current_version(root) != release['version']:
                raise UpdateError('Checkout changed during Claude integration refresh.')
        except (RuntimeError, OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
            actual = None
            try:
                actual = current_version(root)
            except UpdateError:
                pass
            try:
                reached = git(root, 'rev-parse', 'HEAD')
            except UpdateError:
                reached = 'unavailable'
            if advanced or reached != head or refreshing:
                state = result(actual, release) if actual else result(current, release)
                state['currentVersion'] = actual
                repair = 'python3 ' + shlex.quote(str(Path(root) / 'claude_install.py')) + ' --refresh'
                state.update(updated=False, message=(
                    'Checkout or Claude integration could not be fully verified '
                    f'(HEAD {reached}, package {actual or "unreadable"}). '
                    f'Inspect the checkout and run `{repair}` from the owned checkout; '
                    'do not reset automatically.'))
                raise PartialClaudeUpdate(state) from exc
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError('Claude update failed before checkout advancement; inspect the owned installation.') from exc
        write_cache(None, release, host='claude')
        final = result(current_version(root), release)
        final.update(updated=advanced, message=(
            f'Updated to {release["version"]}; restart Claude and open a new shell to load the refreshed integration.'
            if advanced else
            f'Already at {release["version"]}; owned integration refreshed. Restart Claude if it was previously stale.'))
        return final


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'install'))
    parser.add_argument('--host', choices=('omp', 'claude'), default='omp')
    parser.add_argument('--profile')
    parser.add_argument('--cached', action='store_true', help='reuse a successful public check for up to 24 hours')
    args = parser.parse_args(argv)
    if args.cached and args.action != 'check':
        parser.error('--cached is only supported for check')
    if args.host == 'claude' and args.profile is not None:
        parser.error('--profile is not supported for Claude')
    try:
        if args.host == 'claude':
            response = (check_claude_update(cached=args.cached) if args.action == 'check'
                        else install_claude_update())
        else:
            cache_path(args.profile)  # Validate profile before any mutation/network request.
            response = (check(args.profile, args.cached, managed=True) if args.action == 'check'
                        else install_update(args.profile))
    except PartialClaudeUpdate as exc:
        print(json.dumps(exc.response))
        return 1
    except (UpdateError, RuntimeError, OSError, ValueError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, (UpdateError, ValueError)) else 'Claude installation ownership or local files could not be accessed safely; inspect the checkout.'
        message = ''.join(char if char.isprintable() else ' ' for char in message)
        print(f'Update failed: {message}', file=sys.stderr)
        return 1
    print(json.dumps(response))
    return 0


if __name__ == '__main__':
    sys.exit(main())
