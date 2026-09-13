"""Profile-local dashboard preferences, separate from OMP credentials."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import tempfile


DEFAULTS = {
    'providers': ['openai-codex'],
    'hidden': [],
    'windows': {},
    'side': 'right',
    'compact': True,
    'interval': 60,
    'enabled': True,
}
_TRANSIENT = {'profile', 'refresh'}


def resolve_profile(profile: str | None = None) -> str:
    if profile is None:
        profile = os.environ.get('OMP_PROFILE', os.environ.get('PI_PROFILE', 'default'))
    if not isinstance(profile, str):
        raise ValueError('OMP profile must be a string.')
    profile = profile.strip() or 'default'
    if profile in ('.', '..') or any(char in profile for char in ('/', '\\', '\x00')):
        raise ValueError('OMP profile must be a single directory name.')
    return profile


def agent_dir(profile: str | None = None) -> Path:
    profile = resolve_profile(profile)
    if profile != 'default':
        return Path.home() / '.omp' / 'profiles' / profile / 'agent'
    configured = os.environ.get('PI_CODING_AGENT_DIR')
    return Path(configured).expanduser() if configured else Path.home() / '.omp' / 'agent'


def preferences_path(profile: str | None = None) -> Path:
    return agent_dir(profile) / 'usage-dashboard.json'


def _valid_string(value):
    return isinstance(value, str) and bool(value.strip()) and '\x00' not in value


def _validate_strings(value, field):
    if not isinstance(value, list) or not all(_valid_string(item) for item in value):
        raise ValueError(f'Dashboard {field} must be a list of nonempty strings.')


def _validated(data):
    if not isinstance(data, dict):
        raise ValueError('Dashboard preferences must be a JSON object.')
    if data.keys() - DEFAULTS.keys() - _TRANSIENT:
        raise ValueError('Unknown dashboard preference field.')
    result = deepcopy(DEFAULTS)
    result.update({key: deepcopy(value) for key, value in data.items() if key not in _TRANSIENT})
    for field in ('providers', 'hidden'):
        _validate_strings(result[field], field)
    windows = result['windows']
    if not isinstance(windows, dict):
        raise ValueError('Dashboard windows must map providers to substring lists.')
    for provider, patterns in windows.items():
        if not _valid_string(provider):
            raise ValueError('Dashboard window provider must be a nonempty string.')
        _validate_strings(patterns, 'window filters')
    if result['side'] not in ('left', 'right'):
        raise ValueError('Dashboard side must be left or right.')
    for field in ('compact', 'enabled'):
        if type(result[field]) is not bool:
            raise ValueError(f'Dashboard {field} must be a boolean.')
    if type(result['interval']) is not int or result['interval'] < 15:
        raise ValueError('Dashboard interval must be an integer of at least 15 seconds.')
    return result


@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _load(path):
    try:
        content = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return deepcopy(DEFAULTS)
    except UnicodeError as error:
        raise ValueError(f'Invalid dashboard preferences in {path}.') from error
    try:
        data = json.loads(content)
    except ValueError as error:
        raise ValueError(f'Invalid dashboard preferences JSON in {path}.') from error
    return _validated(data)


def load_preferences(profile: str | None = None) -> dict:
    path = preferences_path(profile)
    with _locked(path):
        return _load(path)


def update_preferences(profile: str | None, changes: dict) -> dict:
    """Patch current preferences under a stable lock; never replace stale snapshots."""
    if not isinstance(changes, dict):
        raise ValueError('Dashboard preference changes must be an object.')
    path = preferences_path(profile)
    with _locked(path):
        current = _load(path)
        current.update(changes)
        current = _validated(current)
        descriptor, temporary = tempfile.mkstemp(prefix='.usage-dashboard-', suffix='.json', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(current, stream, ensure_ascii=True, allow_nan=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return current
