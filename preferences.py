"""Profile-local dashboard preferences, separate from OMP credentials."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from host_adapters import get_host


THEME_NAMES = ('green', 'blue', 'brown', 'yellow', 'cyan', 'magenta', 'orange', 'red', 'claude')
TOKEN_NAMES = ('text', 'muted', 'secondary', 'accent', 'chart', 'good', 'warn', 'error')
COLOR_NAMES = frozenset(('default', 'black', 'red', 'green', 'yellow', 'blue',
                         'magenta', 'cyan', 'white', 'gray', 'brown', 'orange'))
_HEX_COLOR = re.compile(r'^#[0-9a-f]{6}$')


DEFAULTS = {
    'providers': [],
    'hidden': [],
    'windows': {},
    'side': 'right',
    'compact': True,
    'commands_visible': True,
    'previous_visible': True,
    'history_other_visible': True,
    'history_total_visible': True,
    'interval': 60,
    'enabled': True,
    'theme': 'green',
    'tokens': {},
}
_TRANSIENT = {'profile', 'refresh'}


def normalize_color(value):
    if not isinstance(value, str):
        raise ValueError('Dashboard token colors must be terminal names or #RRGGBB values.')
    value = value.strip().lower()
    if value in COLOR_NAMES or _HEX_COLOR.fullmatch(value):
        return value
    raise ValueError('Dashboard token colors must be terminal names or #RRGGBB values.')


def resolve_profile(profile: str | None = None) -> str:
    return get_host('omp').normalize_profile(profile)


def agent_dir(profile: str | None = None, *, host: str = 'omp') -> Path:
    return get_host(host).data_root(profile)


def preferences_path(profile: str | None = None, *, host: str = 'omp') -> Path:
    return agent_dir(profile, host=host) / 'usage-dashboard.json'


def _valid_string(value):
    return isinstance(value, str) and bool(value.strip()) and '\x00' not in value


def _validate_strings(value, field):
    if not isinstance(value, list) or not all(_valid_string(item) for item in value):
        raise ValueError(f'Dashboard {field} must be a list of nonempty strings.')


def migrate_history_visibility(data):
    """Expand the deprecated combined history visibility setting."""
    if not isinstance(data, dict):
        raise ValueError('Dashboard preferences must be a JSON object.')
    result = deepcopy(data)
    if 'history_visible' in result:
        visible = result.pop('history_visible')
        if type(visible) is not bool:
            raise ValueError('Dashboard history_visible must be a boolean.')
        result.setdefault('history_other_visible', visible)
        result.setdefault('history_total_visible', visible)
    return result



def _defaults(host):
    result = deepcopy(DEFAULTS)
    result['providers'] = list(get_host(host).default_providers)
    return result


def _validated(data, host='omp'):
    if not isinstance(data, dict):
        raise ValueError('Dashboard preferences must be a JSON object.')
    data = migrate_history_visibility(data)
    if data.keys() - DEFAULTS.keys() - _TRANSIENT:
        raise ValueError('Unknown dashboard preference field.')
    result = _defaults(host)
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
    if result['theme'] not in THEME_NAMES:
        raise ValueError('Dashboard theme must be one of: ' + ', '.join(THEME_NAMES) + '.')
    tokens = result['tokens']
    if not isinstance(tokens, dict):
        raise ValueError('Dashboard tokens must map token names to colors.')
    normalized_tokens = {}
    for name, color in tokens.items():
        if name not in TOKEN_NAMES:
            raise ValueError(f'Unknown dashboard token: {name}.')
        normalized_tokens[name] = normalize_color(color)
    result['tokens'] = normalized_tokens
    for field in ('compact', 'enabled', 'commands_visible', 'previous_visible',
                  'history_other_visible', 'history_total_visible'):
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


def _load(path, host='omp'):
    try:
        content = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return _defaults(host)
    except UnicodeError as error:
        raise ValueError(f'Invalid dashboard preferences in {path}.') from error
    try:
        data = json.loads(content)
    except ValueError as error:
        raise ValueError(f'Invalid dashboard preferences JSON in {path}.') from error
    return _validated(data, host)


def load_preferences(profile: str | None = None, *, host: str = 'omp') -> dict:
    path = preferences_path(profile, host=host)
    with _locked(path):
        return _load(path, host)


def update_preferences(profile: str | None, changes: dict, *, host: str = 'omp') -> dict:
    """Patch current preferences under a stable lock; never replace stale snapshots."""
    if not isinstance(changes, dict):
        raise ValueError('Dashboard preference changes must be an object.')
    path = preferences_path(profile, host=host)
    with _locked(path):
        current = _load(path, host)
        current.update(changes)
        current = _validated(current, host)
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
