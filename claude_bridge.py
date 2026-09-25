#!/usr/bin/env python3
"""Per-invocation Claude Code telemetry and hook receiver; stores only allowlisted counts."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


from session_usage import database, ingest, owner_key, project_id

MAX_BODY = 256 * 1024
MAX_RECORDS = 1024
MAX_STRING = 256
MAX_TOKENS = 2**53 - 1


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_STRING and '\x00' not in value


def _attribute_map(attributes):
    if not isinstance(attributes, list) or len(attributes) > 128:
        return {}
    result = {}
    for attribute in attributes:
        if not isinstance(attribute, dict) or not _text(attribute.get('key')):
            continue
        key = attribute['key']
        if key not in ('session.id', 'event.name', 'event.timestamp', 'event.sequence',
                       'model', 'input_tokens', 'output_tokens', 'cache_read_tokens',
                       'cache_creation_tokens', 'request_id', 'client_request_id'):
            continue
        value = attribute.get('value')
        if not isinstance(value, dict) or len(value) != 1:
            continue
        kind = 'intValue' if key in ('event.sequence', 'input_tokens', 'output_tokens',
                                    'cache_read_tokens', 'cache_creation_tokens') else 'stringValue'
        if kind in value:
            result[key] = value[kind]
    return result


def _stamp(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 64:
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return moment.timestamp() if moment.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _count(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 16:
        value = int(value)
    return value if type(value) is int and 0 <= value <= MAX_TOKENS else None


def _statusline_text(payload):
    limits = payload.get('rate_limits') if isinstance(payload, dict) else None
    parts = []
    if isinstance(limits, dict):
        for key, label in (('five_hour', '5h'), ('seven_day', '7d')):
            window = limits.get(key)
            used = window.get('used_percentage') if isinstance(window, dict) else None
            if (isinstance(used, (int, float)) and not isinstance(used, bool)
                    and math.isfinite(used) and 0 <= used <= 100):
                parts.append(f'{label} {100 - used:.0f}% left')
    return 'Claude ' + (' | '.join(parts) if parts else 'usage unknown')


def _tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS claude_capture (
        project TEXT NOT NULL, owner TEXT NOT NULL, activation TEXT NOT NULL,
        session TEXT NOT NULL, started REAL NOT NULL, last_event REAL,
        accepted INTEGER NOT NULL DEFAULT 0, incomplete INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (project, owner, activation))''')
    db.execute('''CREATE TABLE IF NOT EXISTS claude_limits (
        project TEXT NOT NULL, owner TEXT NOT NULL, session TEXT NOT NULL,
        activation TEXT NOT NULL, observed REAL NOT NULL, five_pct REAL,
        five_reset REAL, seven_pct REAL, seven_reset REAL,
        PRIMARY KEY (project, owner))''')


def capture_availability(profile=None, *, owner=None, cwd=None, now=None):
    """Normalized capture health for the selected project and owner; no implied zeros."""
    now = time.time() if now is None else now
    with database(profile, cwd, host='claude') as db:
        _tables(db)
        active = db.execute('SELECT activation FROM active WHERE project=? AND owner=?',
                            (project_id(cwd), owner_key(owner))).fetchone()
        row = (db.execute('''SELECT * FROM claude_capture WHERE project=? AND owner=? AND activation=?''',
                          (project_id(cwd), owner_key(owner), active['activation'])).fetchone()
               if active and active['activation'] else None)
    if row is None:
        return {'status': 'unavailable', 'reason': 'No active Claude capture.', 'lastEventAt': None}
    if row['incomplete']:
        status, reason = 'incomplete', 'Some Claude events were rejected or incomplete.'
    elif row['accepted'] == 0:
        status, reason = 'incomplete', 'No complete API request has been captured yet.'
    elif row['last_event'] is None or now - row['last_event'] > 300:
        status, reason = 'incomplete', 'Claude capture is stale; recent usage is unknown.'
    else:
        status, reason = 'active', 'Claude API requests are being captured.'
    return {'status': status, 'reason': reason,
            'lastEventAt': int(row['last_event'] * 1000) if row['last_event'] is not None else None}


class Receiver:
    def __init__(self, session=None, activation=None, owner=None, cwd=None, profile=None):
        if session is not None and not _text(session):
            raise ValueError('Invalid Claude session identity.')
        if activation is not None and not _text(activation):
            raise ValueError('Invalid Claude activation identity.')
        self.profile = profile
        self.cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
        self.owner = owner
        self.socket = None
        self.project = project_id(self.cwd)
        self.owner_identity = owner_key(owner)
        self._bound = owner is not None
        self.auth = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._sessions = {}
        self._current = None
        self._initial = (session, activation or secrets.token_hex(16)) if session else None
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # HTTP logs could include secrets or raw caller data.

            def do_POST(self):
                if self.path not in ('/v1/logs', '/hook', '/statusline'):
                    self.send_error(404)
                    return
                if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + receiver.auth):
                    self.send_error(403)
                    return
                if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                    if self.path == '/v1/logs':
                        with receiver._lock:
                            receiver._incomplete(receiver._current)
                    self.send_error(415)
                    return
                length = self.headers.get('Content-Length', '')
                if (len(length) > 8 or not length.isascii() or
                        not length.isdecimal() or int(length) > MAX_BODY):
                    if self.path == '/v1/logs':
                        with receiver._lock:
                            receiver._incomplete(receiver._current)
                    self.send_error(413)
                    return
                raw = self.rfile.read(int(length))
                try:
                    payload = json.loads(raw)
                    if self.path == '/v1/logs':
                        receiver._logs(payload)
                    elif self.path == '/hook':
                        receiver._hook(payload)
                    else:
                        receiver._statusline(payload)
                except (ValueError, TypeError, KeyError, sqlite3.Error, OverflowError, RecursionError):
                    with receiver._lock:
                        receiver._incomplete(receiver._current)
                    self.send_error(400)
                    return
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

        self._http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._http.daemon_threads = True
        self.address = f'http://127.0.0.1:{self._http.server_port}'
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._thread.start()

    def environment(self):
        return {
            'CLAUDE_CODE_ENABLE_TELEMETRY': '1',
            'OTEL_LOGS_EXPORTER': 'otlp',
            'OTEL_EXPORTER_OTLP_LOGS_PROTOCOL': 'http/json',
            'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT': self.address + '/v1/logs',
            'OTEL_EXPORTER_OTLP_LOGS_HEADERS': 'Authorization=Bearer ' + self.auth,
            'CLAUDE_USAGE_BRIDGE_URL': self.address,
            'CLAUDE_USAGE_BRIDGE_AUTH': self.auth,
        }

    def settings(self, include_status_line=True):
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), 'hook'])
        result = {'hooks': {
            'SessionStart': [{'hooks': [{'type': 'command', 'command': command}]}],
            'SessionEnd': [{'hooks': [{'type': 'command', 'command': command}]}],
        }}
        if include_status_line:
            result['statusLine'] = {
                'type': 'command',
                'command': shlex.join([sys.executable, str(Path(__file__).resolve()), 'statusline']),
            }
        return result

    def _activate(self, session, activation):
        self._sessions[session] = activation
        self._current = session
        ingest({'session': session, 'activation': activation, 'owner': self.owner,
                'socket': self.socket, 'action': 'start'},
               self.profile, cwd=self.cwd, host='claude')
        with database(self.profile, self.cwd, host='claude') as db:
            _tables(db)
            db.execute('''INSERT OR IGNORE INTO claude_capture
                          (project, owner, activation, session, started) VALUES (?, ?, ?, ?, ?)''',
                       (self.project, self.owner_identity, activation, session, time.time()))

    def _hook(self, payload):
        if not isinstance(payload, dict) or not _text(payload.get('session_id')):
            raise ValueError('Missing Claude hook session.')
        session = payload['session_id']
        pane = payload.get('owner')
        socket = payload.get('tmux_socket')
        if pane is not None and (not isinstance(pane, str) or
                                 len(pane) > 16 or not pane.startswith('%') or
                                 not pane[1:].isascii() or not pane[1:].isdecimal()):
            raise ValueError('Invalid Claude pane.')
        if socket is not None and (not isinstance(socket, str) or len(socket) > 256 or
                                   (socket and not socket.startswith('/')) or
                                   any(char in socket for char in '\x00\r\n')):
            raise ValueError('Invalid Claude tmux socket.')
        with self._lock:
            if payload.get('hook_event_name') == 'SessionStart':
                if pane and not self._bound:
                    self.owner = pane
                    self.socket = socket if socket is not None else ''
                    self.owner_identity = owner_key(pane, socket=self.socket)
                    self._bound = True
                elif pane and (pane != self.owner or
                               (socket is not None and self.socket is not None and socket != self.socket)):
                    raise ValueError('Claude pane changed during capture.')
                if session in self._sessions and self._current == session:
                    return
                initial = self._initial
                activation = initial[1] if initial and initial[0] == session else secrets.token_hex(16)
                self._initial = None
                self._activate(session, activation)
            elif payload.get('hook_event_name') == 'SessionEnd':
                activation = self._sessions.get(session)
                if activation is None:
                    raise ValueError('Unmatched Claude hook session.')
                ingest({'session': session, 'activation': activation, 'owner': self.owner,
                        'socket': self.socket, 'action': 'stop'},
                       self.profile, cwd=self.cwd, host='claude')
                if self._current == session:
                    self._current = None
            else:
                raise ValueError('Unsupported Claude hook event.')

    def _incomplete(self, session):
        activation = self._sessions.get(session)
        if activation is None:
            return
        with database(self.profile, self.cwd, host='claude') as db:
            _tables(db)
            db.execute('''UPDATE claude_capture SET incomplete=1
                          WHERE project=? AND owner=? AND activation=?''',
                       (self.project, self.owner_identity, activation))

    def _logs(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('resourceLogs'), list):
            raise ValueError('Invalid OTLP logs.')
        records = []
        for resource in payload['resourceLogs']:
            if not isinstance(resource, dict) or not isinstance(resource.get('scopeLogs'), list):
                raise ValueError('Invalid OTLP resource.')
            for scope in resource['scopeLogs']:
                if not isinstance(scope, dict) or not isinstance(scope.get('logRecords'), list):
                    raise ValueError('Invalid OTLP scope.')
                records.extend(scope['logRecords'])
                if len(records) > MAX_RECORDS:
                    raise ValueError('Too many OTLP records.')
        with self._lock:
            for record in records:
                if not isinstance(record, dict):
                    self._incomplete(self._current)
                    continue
                # Never inspect, persist or forward the event body or unknown attributes.
                fields = _attribute_map(record.get('attributes'))
                if fields.get('event.name') != 'api_request':
                    continue
                session = fields.get('session.id')
                if not _text(session) or session not in self._sessions:
                    self._incomplete(self._current)
                    continue
                counts = [_count(fields.get(name)) for name in (
                    'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens')]
                at = _stamp(fields.get('event.timestamp'))
                sequence = _count(fields.get('event.sequence'))
                model = fields.get('model')
                if any(value is None for value in counts) or at is None or sequence is None or not _text(model):
                    self._incomplete(session)
                    continue
                request_id = fields.get('request_id') or fields.get('client_request_id')
                if _text(request_id):
                    identity = [session, 'request', request_id]
                else:
                    identity = [session, 'event', fields['event.timestamp'], sequence, model, counts]
                identifier = 'claude:' + hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
                entry = {'id': identifier, 'provider': 'anthropic', 'model': model,
                         'at': at, 'input': counts[0], 'output': counts[1],
                         'cacheRead': counts[2], 'cacheWrite': counts[3], 'total': sum(counts)}
                activation = self._sessions[session]
                ingest({'session': session, 'activation': activation, 'owner': self.owner,
                        'socket': self.socket, 'entries': [entry]},
                       self.profile, cwd=self.cwd, host='claude')
                with database(self.profile, self.cwd, host='claude') as db:
                    _tables(db)
                    db.execute('''UPDATE claude_capture SET accepted=accepted+1, last_event=?
                                  WHERE project=? AND owner=? AND activation=?''',
                               (time.time(), self.project, self.owner_identity, activation))

    def _statusline(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Invalid Claude status line.')
        session = payload.get('session_id')
        if not _text(session):
            session = (payload.get('session') or {}).get('id') if isinstance(payload.get('session'), dict) else None
        limits = payload.get('rate_limits')
        if not isinstance(limits, dict):
            limits = {}
        if not _text(session):
            return
        with self._lock:
            activation = self._sessions.get(session)
            if activation is None or self._current != session:
                return
            windows = []
            for key in ('five_hour', 'seven_day'):
                window = limits.get(key)
                if not isinstance(window, dict):
                    windows.extend((None, None))
                    continue
                used, reset = window.get('used_percentage'), window.get('resets_at')
                if (isinstance(used, bool) or not isinstance(used, (int, float)) or
                        not math.isfinite(used) or not 0 <= used <= 100):
                    windows.extend((None, None))
                    continue
                if (isinstance(reset, (int, float)) and not isinstance(reset, bool) and
                        math.isfinite(reset) and reset <= time.time()):
                    windows.extend((None, None))
                    continue
                if (isinstance(reset, bool) or not isinstance(reset, (int, float)) or
                        not math.isfinite(reset) or reset > time.time() + 31 * 86400):
                    reset = None
                windows.extend((float(used), float(reset) if reset is not None else None))
            with database(self.profile, self.cwd, host='claude') as db:
                _tables(db)
                db.execute('''INSERT OR REPLACE INTO claude_limits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                           (self.project, self.owner_identity, session, activation, time.time(), *windows))

    def close(self):
        self._http.shutdown()
        self._http.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def start_receiver(session=None, activation=None, owner=None, cwd=None, profile=None):
    """Launch a loopback-only receiver; caller owns its lifetime and environment."""
    return Receiver(session, activation, owner, cwd, profile)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _loopback_bridge_url(url):
    if not url or any(ord(char) <= 32 or char == '\\' for char in url):
        return False
    if '?' in url or '#' in url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host, separator, port_text = parts.netloc.partition(':')
    return (parts.scheme == 'http' and parts.hostname == '127.0.0.1' and
            parts.username is None and parts.password is None and
            host == '127.0.0.1' and separator and
            port_text.isascii() and port_text.isdecimal() and
            port is not None and port > 0 and not parts.path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('hook', 'statusline'))
    args = parser.parse_args(argv)
    url = os.environ.get('CLAUDE_USAGE_BRIDGE_URL', '')
    token = os.environ.get('CLAUDE_USAGE_BRIDGE_AUTH', '')
    if not _loopback_bridge_url(url) or not token:
        if args.mode == 'statusline':
            print('Claude usage unknown')
        return 0
    raw = sys.stdin.buffer.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        if args.mode == 'statusline':
            print('Claude usage unknown')
        return 0
    try:
        payload = json.loads(raw)
        if args.mode == 'statusline':
            print(_statusline_text(payload))
        if args.mode == 'hook' and isinstance(payload, dict):
            tmux = os.environ.get('TMUX', '')
            pane = os.environ.get('TMUX_PANE', '')
            payload = {'hook_event_name': payload.get('hook_event_name'),
                       'session_id': payload.get('session_id'),
                       'reason': payload.get('reason')}
            if pane.startswith('%') and pane[1:].isascii() and pane[1:].isdecimal():
                payload['owner'] = pane
                payload['tmux_socket'] = tmux.rsplit(',', 2)[0]
        if args.mode == 'statusline' and isinstance(payload, dict):
            limits = payload.get('rate_limits')
            session = payload.get('session_id')
            if not _text(session):
                nested = payload.get('session')
                session = nested.get('id') if isinstance(nested, dict) else None
            if not _text(session):
                session = None
            payload = {'session_id': session,
                       'rate_limits': {key: {
                           'used_percentage': value.get('used_percentage'),
                           'resets_at': value.get('resets_at')}
                           for key, value in limits.items()
                           if key in ('five_hour', 'seven_day') and isinstance(value, dict)}
                       if isinstance(limits, dict) else None}
        request = urllib.request.Request(
            url + ('/hook' if args.mode == 'hook' else '/statusline'),
            data=json.dumps(payload, allow_nan=False).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(
                request, timeout=2):
            pass
    except (ValueError, OSError, urllib.error.URLError):
        if args.mode == 'statusline' and not isinstance(locals().get('payload'), dict):
            print('Claude usage unknown')
        pass  # Hooks never echo payload or disrupt Claude execution.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
