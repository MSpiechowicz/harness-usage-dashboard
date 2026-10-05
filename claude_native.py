#!/usr/bin/env python3
"""Private JSON accounting and presentation boundary for the native Claude mod."""
import json
import os
import re
import sqlite3
import sys
import time

import dashboard_view as view
from preferences import agent_dir, load_preferences, transform_preferences
from session_usage import database, finite, ingest, owner_key, project_id, summary

VERSION = 1
MAX_INPUT = 2 * 1024 * 1024
ALLOWANCE_MAX_AGE = 300
COUNTERS = ('input', 'output', 'cacheRead', 'cacheWrite', 'total')
_ID = re.compile(r'(?:claude:)?[0-9a-f]{64}')
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/ +()-]{0,199}')


class RequestError(ValueError):
    def __init__(self, message, code='invalid_request'):
        super().__init__(message)
        self.code = code


def _identity(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise RequestError('Invalid native session identity.')
    return value


def _context(request):
    cwd = request.get('cwd')
    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or '\x00' in cwd:
        raise RequestError('Invalid project directory.')
    return cwd, *(_identity(request.get(name)) for name in ('owner', 'session', 'activation'))


def _capture_schema(db):
    # Keep the historical bridge table shape; never read obsolete claude_limits.
    db.execute('''CREATE TABLE IF NOT EXISTS claude_capture (
        project TEXT NOT NULL, owner TEXT NOT NULL, activation TEXT NOT NULL,
        session TEXT NOT NULL, started REAL NOT NULL, last_event REAL,
        accepted INTEGER NOT NULL DEFAULT 0, incomplete INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (project, owner, activation))''')


def _migration_pending():
    return os.path.lexists(agent_dir(host='claude') / 'installation.json')


def _entries(values):
    if not isinstance(values, list) or len(values) > 2000:
        raise RequestError('Invalid native usage entries.')
    entries = []
    for entry in values:
        if not isinstance(entry, dict):
            raise RequestError('Invalid native usage entry.')
        counts = [entry.get(name) for name in COUNTERS]
        if (not all(type(value) is int and 0 <= value <= 2**53 - 1 for value in counts)
                or counts[-1] != sum(counts[:4])):
            raise RequestError('Native usage requires complete exact token counts.')
        if not isinstance(entry.get('id'), str) or not _ID.fullmatch(entry['id']):
            raise RequestError('Native usage requires a stable identity digest.')
        if entry.get('provider') != 'anthropic':
            raise RequestError('Native Claude usage only supports Anthropic.')
        model = entry.get('model')
        if not isinstance(model, str) or not _LABEL.fullmatch(model):
            raise RequestError('Invalid native model identifier.')
        at = entry.get('at')
        if not finite(at) or at < 0:
            raise RequestError('Invalid native usage timestamp.')
        cleaned = {'id': entry['id'], 'provider': 'anthropic', 'model': model, 'at': at,
                   **dict(zip(COUNTERS, counts))}
        if 'thinkingLevel' in entry:
            level = entry['thinkingLevel']
            if not isinstance(level, str) or not _LABEL.fullmatch(level):
                raise RequestError('Invalid native thinking level.')
            cleaned['thinkingLevel'] = level
        entries.append(cleaned)
    return entries


def capture(request, now):
    cwd, owner, session, activation = _context(request)
    if _migration_pending():
        raise RequestError('Legacy Claude installation remains. Run claude_migrate.py dry-run, then --apply explicitly before native capture.',
                           'migration_required')
    action = request.get('action')
    if action not in ('start', 'record', 'stop') or type(request.get('incomplete')) is not bool:
        raise RequestError('Invalid native capture action or gap state.')
    entries = _entries(request.get('entries'))
    project, identity = project_id(cwd), owner_key(owner, socket='')
    with database(cwd=cwd, host='claude') as db:
        _capture_schema(db)
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('''SELECT session FROM claude_capture
                                 WHERE project=? AND owner=? AND activation=?''',
                              (project, identity, activation)).fetchone()
        if existing and existing['session'] != session:
            raise RequestError('Native activation belongs to a different session.')
        if action != 'start' and not existing:
            raise RequestError('Native capture has not started for this activation.', 'capture_not_started')
        db.execute('''INSERT OR IGNORE INTO claude_capture
                      (project, owner, activation, session, started) VALUES (?, ?, ?, ?, ?)''',
                   (project, identity, activation, session, now))
        effective_action = 'record' if action == 'start' and existing else action
        ingest({'session': session, 'activation': activation, 'owner': owner, 'socket': '',
                'action': effective_action, 'entries': entries}, cwd=cwd, now=now, host='claude', _db=db)
        # Health follows durable reports, including zero, never changed replay counts.
        if entries:
            identifiers = [entry['id'] for entry in entries]
            placeholders = ','.join('?' for _ in identifiers)
            event_at = db.execute(
                f'SELECT MAX(at) FROM tokens WHERE project=? AND session=? AND id IN ({placeholders})',
                (project, session, *identifiers)).fetchone()[0]
            if event_at is not None:
                db.execute('''UPDATE claude_capture SET accepted=1,
                              last_event=MAX(COALESCE(last_event, 0), ?)
                              WHERE project=? AND owner=? AND activation=?''',
                           (event_at, project, identity, activation))
        if request['incomplete']:
            db.execute('''UPDATE claude_capture SET incomplete=1
                          WHERE project=? AND owner=? AND activation=?''',
                       (project, identity, activation))
    return {'session': session, 'activation': activation}


def _health(db, cwd, owner, session, activation):
    if _migration_pending():
        return {'state': 'unknown', 'reason': 'Legacy installation remains; run claude_migrate.py before native capture.',
                'lastEventAt': None}
    row = db.execute('''SELECT * FROM claude_capture
                        WHERE project=? AND owner=? AND session=? AND activation=?''',
                     (project_id(cwd), owner_key(owner, socket=''), session, activation)).fetchone()
    if not row:
        return {'state': 'unknown', 'reason': 'No native capture for this activation.', 'lastEventAt': None}
    if row['incomplete']:
        state, reason = 'incomplete', 'Native usage coverage is unavailable or unknown; totals include observed reports only.'
    elif row['accepted']:
        state, reason = 'available', 'Native token reports observed.'
    else:
        state, reason = 'unknown', 'No complete native usage reported yet.'
    return {'state': state, 'reason': reason, 'lastEventAt': row['last_event']}


def _allowance(value, session, activation, active, now):
    if value is None:
        return [], 'No native allowance reported yet.'
    if not isinstance(value, dict):
        raise RequestError('Invalid native allowance.')
    if (value.get('session'), value.get('activation')) != (session, activation):
        return [], 'Allowance belongs to another activation.'
    if not active or (active['session'], active['activation']) != (session, activation):
        return [], 'No active native allowance session.'
    observed = value.get('observedAt')
    if not finite(observed) or observed < 0:
        raise RequestError('Invalid allowance observation timestamp.')
    if observed < active['since'] or observed > now or now - observed > ALLOWANCE_MAX_AGE:
        return [], 'Native allowance observation is stale.'
    windows = value.get('windows')
    if not isinstance(windows, list) or len(windows) > 100:
        raise RequestError('Invalid native allowance windows.')
    limits = []
    for window in windows:
        if not isinstance(window, dict):
            raise RequestError('Invalid native allowance window.')
        identifier, label = window.get('id'), window.get('label')
        if not all(isinstance(item, str) and _LABEL.fullmatch(item) for item in (identifier, label)):
            raise RequestError('Invalid native allowance label.')
        used, reset = window.get('usedFraction'), window.get('resetsAt')
        if not finite(used) or used < 0 or (reset is not None and (not finite(reset) or reset < 0)):
            raise RequestError('Invalid native allowance values.')
        if reset is not None and reset <= now:
            continue
        limits.append({'id': identifier, 'label': label, 'amount': {'usedFraction': used},
                       'window': {'resetsAt': reset * 1000 if reset is not None else None}})
    if not limits:
        return [], 'No current native allowance windows.'
    return [{'provider': 'anthropic', 'fetchedAt': observed * 1000,
             'limits': limits, 'metadata': {'source': 'native-mod'}}], ''


def _styled_rows(rows, width):
    aliases = {'normal': 'text', 'dim': 'muted', 'title': 'accent'}
    result = []
    for text, style in rows:
        text = view.clean(text)[:width]
        emphasis = None
        if isinstance(style, tuple):
            base, start, end, highlight = style
            start, end = min(start, len(text)), min(end, len(text))
            if start < end:
                emphasis = {'start': start, 'end': end, 'token': aliases.get(highlight, highlight)}
        else:
            base = style
        result.append({'text': text, 'token': aliases.get(base, base), 'emphasis': emphasis})
    return result


def snapshot(request, now):
    cwd, owner, session, activation = _context(request)
    width = request.get('width')
    if type(width) is not int or not 1 <= width <= 500:
        raise RequestError('Invalid native dashboard width.')
    preferences = load_preferences(host='claude')
    with database(cwd=cwd, host='claude') as db:
        _capture_schema(db)
        # History, allowance ownership and capture health share one ledger snapshot.
        history = summary(owner=owner, cwd=cwd, host='claude', socket='', now=now, _db=db)
        active = db.execute('SELECT session, activation, since FROM active WHERE project=? AND owner=?',
                            (project_id(cwd), owner_key(owner, socket=''))).fetchone()
        health = _health(db, cwd, owner, session, activation)
    reports, note = _allowance(request.get('allowance'), session, activation, active, now)
    rows = view.session_lines(history, now, width, preferences['compact'],
                              preferences['previous_visible'], preferences['history_other_visible'],
                              preferences['history_total_visible'], host='claude',
                              chart_type=preferences['chart_type'])
    data = {'reports': reports, 'dashboardNote': note}
    allowance_visible = ('anthropic' in preferences['providers']
                         and 'anthropic' not in preferences['hidden'])
    # Registered project usage already shows capture works; only a real gap stays a warning.
    registered = any(history.get(key) for key in ('previous', 'history', 'total_history')) or bool(
        (history.get('current') or {}).get('providers'))
    capture_warning = health['state'] == 'incomplete' or (health['state'] == 'unknown' and not registered)
    if allowance_visible or capture_warning:
        rows.append(view.section_heading(view.NAMES['anthropic'], width))
        if allowance_visible:
            rows.extend(view.provider_lines(data, 'anthropic', {**preferences, 'interval': ALLOWANCE_MAX_AGE},
                                            now, width, host='claude'))
        if capture_warning:
            if allowance_visible:
                rows.append(('', 'dim'))
            rows.extend([('Token capture ' + health['state'], 'warn'), (health['reason'], 'dim')])
        rows.append(('', 'dim'))
    if preferences['commands_visible']:
        rows.extend([view.section_heading('COMMANDS', width),
                     ('/usage-dashboard window on|off|focus|refresh', 'dim'),
                     ('/usage-dashboard view compact|details', 'dim'),
                     ('/usage-dashboard chart bars|dots|trace', 'dim')])
    return {'history': history, 'reports': reports, 'preferences': preferences, 'capture': health,
            'tokens': view.resolve_tokens(preferences), 'rows': _styled_rows(rows, width),
            'session': session, 'activation': activation}


def preferences(request):
    words = request.get('words')
    if (not isinstance(words, list) or len(words) > 5
            or not all(isinstance(word, str) and len(word) <= 256 for word in words)):
        raise RequestError('Invalid native dashboard command.')
    if not words:
        words = ['view', 'list']
    if words == ['init']:
        raise RequestError(view.NATIVE_HELP, 'unsupported_command')
    if words[0] == 'position' or words[:2] == ['window', 'interval']:
        raise RequestError('Native dashboards have no position or polling interval control.', 'unsupported_command')
    if words[:2] in (['window', 'focus'], ['window', 'refresh']):
        raise RequestError('Native Pane actions must be handled by the mod.', 'unsupported_command')

    def mutate(current):
        try:
            return view.change_config(current, words, host='claude')
        except ValueError:
            raise RequestError(view.NATIVE_HELP, 'unsupported_command') from None

    saved = transform_preferences(None, mutate, host='claude')
    return {'preferences': saved, 'text': '\n'.join(view.clean(line) for line in view.describe(saved, native=True).splitlines())}


def handle_request(request, *, now=None):
    if not isinstance(request, dict) or type(request.get('version')) is not int or request['version'] != VERSION:
        raise RequestError('Unsupported native helper request version.')
    now = time.time() if now is None else now
    op = request.get('op')
    if op == 'capture':
        result = capture(request, now)
    elif op == 'snapshot':
        result = snapshot(request, now)
    elif op == 'preferences':
        result = preferences(request)
    else:
        raise RequestError('Unsupported native helper operation.')
    return {'version': VERSION, 'ok': True, **result}


def main():
    try:
        payload = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(payload) > MAX_INPUT:
            raise RequestError('Native helper request is too large.')
        try:
            request = json.loads(payload)
        except (ValueError, UnicodeError):
            raise RequestError('Invalid native helper JSON.') from None
        response = handle_request(request)
        status = 0
    except RequestError as error:
        response = {'version': VERSION, 'ok': False, 'error': {'code': error.code, 'message': str(error)}}
        status = 1
    except (OSError, sqlite3.Error, ValueError):
        response = {'version': VERSION, 'ok': False, 'error': {
            'code': 'storage_unavailable', 'message': 'Native dashboard storage is unavailable or invalid; check local files and permissions.'}}
        status = 1
    except Exception:
        response = {'version': VERSION, 'ok': False, 'error': {
            'code': 'helper_failed', 'message': 'Native dashboard helper failed.'}}
        status = 1
    print(json.dumps(response, ensure_ascii=True, allow_nan=False))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
