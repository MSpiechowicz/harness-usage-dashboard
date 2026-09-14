#!/usr/bin/env python3
"""Project-scoped token ledger and explicitly observed account-quota changes."""
import argparse
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time

from preferences import agent_dir


LEGACY_SCOPE = 'legacy'


@lru_cache(maxsize=64)
def _project_id(cwd):
    path = Path(cwd).expanduser().resolve()
    if path.is_file():
        path = path.parent
    for parent in (path, *path.parents):
        if (parent / '.git').exists():
            path = parent
            break
    return hashlib.sha256(str(path).encode('utf-8')).hexdigest()


def project_id(cwd=None):
    """Return a stable, non-reversible identifier for the current project."""
    return _project_id(str(Path(cwd or os.getcwd()).expanduser()))


def _table_exists(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_has_project(connection, table):
    return any(row['name'] == 'project' for row in connection.execute(f'PRAGMA table_info({table})'))


def _create_schema(connection):
    connection.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
            project TEXT NOT NULL, id TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL,
            PRIMARY KEY (project, id)
        );
        CREATE TABLE IF NOT EXISTS active (
            project TEXT NOT NULL, owner TEXT NOT NULL, session TEXT NOT NULL, activation TEXT NOT NULL,
            since REAL NOT NULL, previous TEXT, PRIMARY KEY (project, owner)
        );
        CREATE TABLE IF NOT EXISTS tokens (
            project TEXT NOT NULL, id TEXT NOT NULL, session TEXT NOT NULL, at REAL NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, input INTEGER NOT NULL, output INTEGER NOT NULL,
            cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL, total INTEGER NOT NULL,
            PRIMARY KEY (project, id)
        );
        CREATE INDEX IF NOT EXISTS tokens_project_session_time ON tokens(project, session, at);
        CREATE TABLE IF NOT EXISTS quota (
            project TEXT NOT NULL, session TEXT NOT NULL, activation TEXT NOT NULL, key TEXT NOT NULL,
            provider TEXT NOT NULL, label TEXT NOT NULL, segment INTEGER NOT NULL,
            reset TEXT NOT NULL, first REAL NOT NULL, last REAL NOT NULL,
            start REAL NOT NULL, end REAL NOT NULL, samples INTEGER NOT NULL,
            PRIMARY KEY (project, session, activation, key, segment)
        );
    ''')


@contextmanager
def database(profile=None, cwd=None):
    path = agent_dir(profile) / 'usage-dashboard.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            legacy_tables = []
            for table in ('sessions', 'active', 'tokens', 'quota'):
                if _table_exists(connection, table) and not _table_has_project(connection, table):
                    if table == 'tokens':
                        connection.execute('DROP INDEX IF EXISTS tokens_session_time')
                    legacy = table + '_legacy'
                    connection.execute(f'ALTER TABLE {table} RENAME TO {legacy}')
                    legacy_tables.append((table, legacy))
            _create_schema(connection)
            columns = {
                'sessions': 'id, started, updated',
                'active': 'owner, session, activation, since, previous',
                'tokens': 'id, session, at, provider, model, input, output, cache_read, cache_write, total',
                'quota': 'session, activation, key, provider, label, segment, reset, first, last, start, end, samples',
            }
            for table, legacy in legacy_tables:
                connection.execute(
                    f'INSERT INTO {table} (project, {columns[table]}) '
                    f'SELECT ?, {columns[table]} FROM {legacy}', (LEGACY_SCOPE,)
                )
                connection.execute(f'DROP TABLE {legacy}')
        with connection:
            yield connection
    finally:
        connection.close()


def owner_key(owner=None):
    socket = os.environ.get('TMUX', '').rsplit(',', 2)[0]
    return socket + ':' + (owner or os.environ.get('TMUX_PANE', 'standalone'))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def ingest(payload, profile=None, now=None, cwd=None):
    now = time.time() if now is None else now
    project = project_id(cwd)
    session, activation = payload['session'], payload['activation']
    if not all(isinstance(value, str) and value for value in (session, activation)):
        raise ValueError('Session identity is missing.')
    owner = owner_key(payload.get('owner'))
    with database(profile, cwd) as db:
        db.execute('INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?)', (project, session, now, now))
        active = db.execute('SELECT * FROM active WHERE project=? AND owner=?', (project, owner)).fetchone()
        if payload.get('action') == 'start':
            previous = active['session'] if active and active['session'] != session else (active['previous'] if active else None)
            if not active or active['activation'] != activation:
                db.execute('INSERT OR REPLACE INTO active VALUES (?, ?, ?, ?, ?, ?)',
                           (project, owner, session, activation, now, previous))
        for entry in payload.get('entries', []):
            counts = [entry.get(field) for field in ('input', 'output', 'cacheRead', 'cacheWrite', 'total')]
            if not all(type(value) is int and 0 <= value <= 2**53 - 1 for value in counts):
                raise ValueError('Invalid session token counts.')
            if not finite(entry.get('at')):
                raise ValueError('Invalid session usage timestamp.')
            if not all(isinstance(entry.get(field), str) and entry[field] for field in ('id', 'provider', 'model')):
                raise ValueError('Invalid session usage identity.')
            db.execute('INSERT OR IGNORE INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (project, entry['id'], session, entry['at'], entry['provider'], entry['model'], *counts))
        db.execute('UPDATE sessions SET updated=? WHERE project=? AND id=?', (now, project, session))
        if payload.get('action') == 'stop':
            # Retain the last session pointer for the next startup, but reject late polls.
            db.execute('UPDATE active SET activation=? WHERE project=? AND owner=? AND activation=?',
                       ('', project, owner, activation))


def active_session(profile=None, owner=None, cwd=None):
    project = project_id(cwd)
    with database(profile, cwd) as db:
        row = db.execute('SELECT * FROM active WHERE project=? AND owner=?', (project, owner_key(owner))).fetchone()
        return dict(row) if row else None


def record_quota(profile, owner, activation, samples, cwd=None):
    """Never bridge an inactive interval, changed window, or decreasing counter."""
    if not activation:
        return
    project = project_id(cwd)
    with database(profile, cwd) as db:
        active = db.execute('SELECT * FROM active WHERE project=? AND owner=?',
                            (project, owner_key(owner))).fetchone()
        if not active or active['activation'] != activation:
            return
        for sample in samples:
            at, value = sample['at'], sample['used']
            if not finite(at) or not finite(value) or value < 0 or at < active['since']:
                continue
            key = hashlib.sha256(json.dumps(sample['identity'], sort_keys=True).encode()).hexdigest()
            previous = db.execute('''SELECT * FROM quota
                                     WHERE project=? AND session=? AND activation=? AND key=?
                                     ORDER BY segment DESC LIMIT 1''',
                                  (project, active['session'], activation, key)).fetchone()
            if previous and at <= previous['last']:
                continue
            reset = json.dumps(sample.get('reset'))
            boundary = previous and (reset != previous['reset'] or value < previous['end'])
            if previous and not boundary:
                db.execute('''UPDATE quota SET last=?, end=?, samples=samples+1
                              WHERE project=? AND session=? AND activation=? AND key=? AND segment=?''',
                           (at, value, project, active['session'], activation, key, previous['segment']))
            else:
                segment = previous['segment'] + 1 if previous else 0
                db.execute('INSERT INTO quota VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (project, active['session'], activation, key, sample['provider'], sample['label'],
                            segment, reset, at, at, value, value, 1))

def summary(profile=None, owner=None, now=None, minutes=20, cwd=None):
    now = time.time() if now is None else now
    project = project_id(cwd)
    with database(profile, cwd) as db:
        active = db.execute('SELECT * FROM active WHERE project=? AND owner=?',
                            (project, owner_key(owner))).fetchone()
        current = active['session'] if active and active['activation'] else None
        previous = active['previous'] if current else (active['session'] if active else None)
        if not previous:
            row = db.execute('''SELECT id FROM sessions
                                WHERE project=? AND id != ? ORDER BY updated DESC LIMIT 1''',
                             (project, current or '')).fetchone()
            previous = row['id'] if row else None
        result = {
            'current': None, 'previous': None, 'history': [], 'total_history': [], 'chart': [0] * minutes,
        }
        for name, identity in (('current', current), ('previous', previous)):
            if not identity:
                continue
            session = db.execute('SELECT * FROM sessions WHERE project=? AND id=?',
                                 (project, identity)).fetchone()
            if not session:
                continue
            models = [dict(row) for row in db.execute('''SELECT provider, model,
                      SUM(input) AS input, SUM(output) AS output, SUM(cache_read) AS cache_read,
                      SUM(cache_write) AS cache_write, SUM(total) AS total
                      FROM tokens WHERE project=? AND session=? GROUP BY provider, model''',
                                                     (project, identity))]
            providers = {}
            for model in models:
                totals = providers.setdefault(model['provider'], {'provider': model['provider'],
                    'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0, 'total': 0})
                for field in ('input', 'output', 'cache_read', 'cache_write', 'total'):
                    totals[field] += model[field]
            quota = db.execute('''SELECT provider, label, key, SUM(end-start)*100 AS points,
                                  SUM(samples-1) AS intervals, COUNT(*) AS segments, MAX(last) AS last
                                  FROM quota WHERE project=? AND session=? GROUP BY provider, label, key''',
                               (project, identity)).fetchall()
            result[name] = {**dict(session), 'providers': list(providers.values()), 'models': models,
                            'quota': [dict(row) for row in quota]}
        if current:
            minute = int(now // 60)
            for row in db.execute('''SELECT CAST(at / 60 AS INTEGER) AS minute, SUM(total) AS total
                                     FROM tokens WHERE project=? AND session=? AND at>=? AND at<=? GROUP BY minute''',
                                  (project, current, (minute - minutes + 1) * 60, now)):
                index = row['minute'] - minute + minutes - 1
                if 0 <= index < minutes:
                    result['chart'][index] = row['total']
        result['history'] = [dict(row) for row in db.execute(
            'SELECT provider, model, SUM(total) AS total FROM tokens '
            'WHERE project=? AND session != ? GROUP BY provider, model',
            (project, current or ''))]
        result['total_history'] = [dict(row) for row in db.execute(
            'SELECT provider, model, SUM(total) AS total FROM tokens '
            'WHERE project=? GROUP BY provider, model',
            (project,))]
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile')
    args = parser.parse_args()
    try:
        ingest(json.load(sys.stdin), args.profile)
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
        print('Could not save dashboard session usage.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
