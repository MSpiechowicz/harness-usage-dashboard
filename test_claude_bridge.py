"""Consumer-facing tests for the Claude loopback capture boundary."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from claude_bridge import MAX_BODY, capture_availability, main, start_receiver
from claude_usage_source import fetch_usage
from session_usage import database, summary


class ClaudeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': self.tmp.name, 'TMUX': '', 'TMUX_PANE': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cwd = Path(self.tmp.name) / 'project'
        self.cwd.mkdir()
        self.other = Path(self.tmp.name) / 'other'
        self.other.mkdir()
        self.receiver = start_receiver(owner='pane-1', cwd=str(self.cwd))
        self.addCleanup(self.receiver.close)

    def send(self, path, data, token=None, content_type='application/json'):
        request = urllib.request.Request(
            self.receiver.address + path, data=json.dumps(data).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + (token or self.receiver.auth),
                     'Content-Type': content_type})
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status

    def hook(self, event, session, **fields):
        return self.send('/hook', {'hook_event_name': event, 'session_id': session, **fields})

    def event(self, session, timestamp, sequence, tokens=(12, 7, 4, 2), **extra):
        fields = {'session.id': session, 'event.name': 'api_request',
                  'event.timestamp': timestamp, 'event.sequence': sequence,
                  'model': 'claude-test', 'input_tokens': tokens[0],
                  'output_tokens': tokens[1], 'cache_read_tokens': tokens[2],
                  'cache_creation_tokens': tokens[3], **extra}
        attributes = [{'key': key, 'value': {
            'intValue' if isinstance(value, int) else 'stringValue': value}}
            for key, value in fields.items()]
        return {'resourceLogs': [{'resource': {'attributes': []}, 'scopeLogs': [{
            'logRecords': [{'body': {'stringValue': 'claude_code.api_request'},
                            'attributes': attributes}]}]}]}

    def test_normal_event_is_scoped_and_hook_start_uses_actual_session(self):
        self.hook('SessionStart', 'real-resumed-id', source='resume')
        self.send('/v1/logs', self.event('real-resumed-id', '2026-09-25T12:00:00.123Z', 0))
        current = summary(owner='pane-1', cwd=str(self.cwd), host='claude')['current']
        self.assertEqual(current['id'], 'real-resumed-id')
        self.assertEqual(current['providers'][0]['total'], 25)
        self.assertEqual(current['providers'][0]['cache_read'], 4)
        self.assertEqual(capture_availability(owner='pane-1', cwd=str(self.cwd))['status'], 'active')
        self.assertIsNone(summary(owner='pane-2', cwd=str(self.cwd), host='claude')['current'])
        self.assertEqual(summary(owner='pane-1', cwd=str(self.other), host='claude')['total_history'], [])
        self.assertEqual(summary(owner='pane-1', cwd=str(self.cwd), host='omp')['total_history'], [])

    def test_duplicate_batches_and_sequence_restart_dedupe_real_requests(self):
        self.hook('SessionStart', 'resumed')
        first = self.event('resumed', '2026-09-25T12:00:00.123Z', 0)
        second = self.event('resumed', '2026-09-25T12:01:00.123Z', 0)
        self.send('/v1/logs', first)
        self.send('/v1/logs', first)
        self.hook('SessionEnd', 'resumed', reason='other')
        self.hook('SessionStart', 'resumed', source='resume')
        self.send('/v1/logs', first)
        self.send('/v1/logs', second)
        report = summary(owner='pane-1', cwd=str(self.cwd), host='claude')
        self.assertEqual(report['current']['providers'][0]['total'], 50)
        with database(cwd=str(self.cwd), host='claude') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tokens').fetchone()[0], 2)

    def test_late_old_session_does_not_move_new_active_owner(self):
        self.hook('SessionStart', 'before')
        self.hook('SessionEnd', 'before', reason='clear')
        self.hook('SessionStart', 'after', source='clear')
        self.send('/v1/logs', self.event('before', '2026-09-25T12:00:00Z', 0))
        self.send('/v1/logs', self.event('after', '2026-09-25T12:01:00Z', 0))
        report = summary(owner='pane-1', cwd=str(self.cwd), host='claude')
        self.assertEqual(report['current']['id'], 'after')
        self.assertEqual(report['current']['providers'][0]['total'], 25)
        self.assertEqual(report['previous']['id'], 'before')
        self.assertEqual(report['previous']['providers'][0]['total'], 25)

    def test_invalid_auth_bodies_and_partial_counts_never_invent_usage(self):
        self.hook('SessionStart', 'selected')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.send('/v1/logs', self.event('selected', '2026-09-25T12:00:00Z', 1), token='wrong')
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        request = urllib.request.Request(
            self.receiver.address + '/v1/logs', data=b'x' * (MAX_BODY + 1), method='POST',
            headers={'Authorization': 'Bearer ' + self.receiver.auth, 'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 413)
        error.exception.close()
        incomplete = self.event('selected', '2026-09-25T12:00:00Z', 1)
        incomplete['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes'] = [
            entry for entry in incomplete['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes']
            if entry['key'] != 'cache_read_tokens']
        self.send('/v1/logs', incomplete)
        self.assertEqual(summary(owner='pane-1', cwd=str(self.cwd), host='claude')['total_history'], [])
        self.assertEqual(capture_availability(owner='pane-1', cwd=str(self.cwd))['status'], 'incomplete')
        self.send('/v1/logs', self.event('unknown', '2026-09-25T12:01:00Z', 0))
        self.assertEqual(summary(owner='pane-1', cwd=str(self.cwd), host='claude')['total_history'], [])

    def test_private_canaries_are_discarded_not_persisted_or_displayed(self):
        self.hook('SessionStart', 'safe-session', transcript_path='SECRET_TRANSCRIPT')
        event = self.event('safe-session', '2026-09-25T12:00:00Z', 0)
        record = event['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
        record['body'] = {'stringValue': 'PRIVATE_PROMPT_CANARY'}
        record['attributes'].extend([
            {'key': 'prompt', 'value': {'stringValue': 'PRIVATE_PROMPT_CANARY'}},
            {'key': 'user.email', 'value': {'stringValue': 'PRIVATE_EMAIL_CANARY'}},
            {'key': 'api_key', 'value': {'stringValue': 'PRIVATE_KEY_CANARY'}},
        ])
        self.send('/v1/logs', event)
        with database(cwd=str(self.cwd), host='claude') as db:
            dump = '\n'.join(db.iterdump())
        for canary in ('PRIVATE_PROMPT_CANARY', 'PRIVATE_EMAIL_CANARY', 'PRIVATE_KEY_CANARY',
                       'SECRET_TRANSCRIPT'):
            self.assertNotIn(canary, dump)

    def test_hook_and_statusline_commands_do_not_echo_json(self):
        self.hook('SessionStart', 'from-command')
        environment = {**os.environ, **self.receiver.environment()}
        command = [sys.executable, str(Path(__file__).with_name('claude_bridge.py')), 'statusline']
        data = {'session_id': 'from-command', 'rate_limits': {
            'five_hour': {'used_percentage': 25, 'resets_at': time.time() + 3600}},
            'private': 'PRIVATE_CANARY'}
        result = subprocess.run(command, input=json.dumps(data), text=True, capture_output=True,
                                env=environment, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), 'Claude 5h 75% left')
        self.assertNotIn('PRIVATE_CANARY', result.stdout + result.stderr)
        hook = subprocess.run([*command[:-1], 'hook'], input=json.dumps({
            'hook_event_name': 'SessionEnd', 'session_id': 'from-command',
            'transcript_path': 'PRIVATE_CANARY'}), text=True, capture_output=True,
            env=environment, timeout=5)
        self.assertEqual(hook.returncode, 0)
        self.assertEqual(hook.stdout, '')
        self.assertEqual(hook.stderr, '')
        self.assertNotIn('PRIVATE_CANARY', subprocess.run(
            [sys.executable, str(Path(__file__).with_name('claude_usage_source.py')),
             '--owner', 'pane-1'], text=True, capture_output=True,
            cwd=self.cwd, env=environment, timeout=5).stdout)

    def test_helper_rejects_ambiguous_bridge_urls_without_network_or_bearer_disclosure(self):
        canary = 'PRIVATE_BEARER_CANARY'
        invalid_urls = (
            'http://127.0.0.1:7@external.example:80',
            'http://127.0.0.1:7@127.0.0.1:80',
            'http://127.0.0.1:0',
            'http://127.0.0.1:65536',
            'http://127.0.0.1:80/path',
            'http://127.0.0.1:80?query',
            'http://127.0.0.1:80#fragment',
            'http://127.0.0.1:80\\@external.example:80',
            'http://127.0.0.1:80\n@external.example:80',
        )
        for url in invalid_urls:
            for mode, expected_output in (('hook', ''), ('statusline', 'Claude usage unknown\n')):
                with self.subTest(url=url, mode=mode):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (patch.dict(os.environ, {
                            'CLAUDE_USAGE_BRIDGE_URL': url, 'CLAUDE_USAGE_BRIDGE_AUTH': canary}),
                          patch('urllib.request.build_opener',
                                side_effect=AssertionError('invalid URL reached opener')),
                          patch('urllib.request.urlopen',
                                side_effect=AssertionError('invalid URL reached network')),
                          redirect_stdout(stdout), redirect_stderr(stderr)):
                        self.assertEqual(main([mode]), 0)
                    self.assertEqual(stdout.getvalue(), expected_output)
                    self.assertEqual(stderr.getvalue(), '')
                    self.assertNotIn(canary, stdout.getvalue() + stderr.getvalue())

    def test_helper_uses_direct_loopback_even_when_http_proxy_is_configured(self):
        self.hook('SessionStart', 'proxy-free')
        payload = {'session_id': 'proxy-free', 'rate_limits': {
            'five_hour': {'used_percentage': 25, 'resets_at': time.time() + 3600}}}
        stdout = io.StringIO()
        environment = {**self.receiver.environment(), 'http_proxy': 'http://127.0.0.1:9',
                       'HTTP_PROXY': 'http://127.0.0.1:9', 'no_proxy': '', 'NO_PROXY': ''}
        with (patch.dict(os.environ, environment),
              patch('sys.stdin', io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))),
              patch('urllib.request.getproxies', return_value={'http': 'http://127.0.0.1:9'}),
              patch('urllib.request.proxy_bypass', return_value=False),
              patch('urllib.request._opener', None),
              redirect_stdout(stdout)):
            self.assertEqual(main(['statusline']), 0)
        self.assertEqual(stdout.getvalue().strip(), 'Claude 5h 75% left')
        self.assertEqual(fetch_usage(owner='pane-1', cwd=str(self.cwd))['reports'][0]['limits'][0]
                         ['amount']['usedFraction'], 0.25)

    def test_statusline_command_reports_nested_and_top_level_session_allowances(self):
        environment = {**os.environ, **self.receiver.environment()}
        command = [sys.executable, str(Path(__file__).with_name('claude_bridge.py')), 'statusline']
        reset = time.time() + 3600
        for identity, session, used in (
                ({'session': {'id': 'nested-session', 'private': 'PRIVATE_CANARY'}},
                 'nested-session', 25),
                ({'session_id': 'top-level-session'}, 'top-level-session', 40),
                ({'session_id': 17, 'session': {'id': 'nested-after-invalid-top'}},
                 'nested-after-invalid-top', 60)):
            with self.subTest(session=session):
                self.hook('SessionStart', session)
                data = {**identity, 'rate_limits': {
                    'five_hour': {'used_percentage': used, 'resets_at': reset}},
                    'private': 'PRIVATE_CANARY'}
                result = subprocess.run(command, input=json.dumps(data), text=True,
                                        capture_output=True, env=environment, timeout=5)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), f'Claude 5h {100 - used}% left')
                self.assertNotIn('PRIVATE_CANARY', result.stdout + result.stderr)
                report = fetch_usage(owner='pane-1', cwd=str(self.cwd))['reports']
                self.assertEqual(len(report), 1)
                self.assertEqual([(limit['id'], limit['amount']['usedFraction'])
                                  for limit in report[0]['limits']],
                                 [('claude:five_hour', used / 100)])
                self.assertEqual(report[0]['limits'][0]['window']['resetsAt'],
                                 int(reset * 1000))
        self.hook('SessionStart', 'no-valid-identity')
        invalid = subprocess.run(command, input=json.dumps({
            'session_id': 17, 'session': {'id': 'x' * 257},
            'rate_limits': {'five_hour': {'used_percentage': 75, 'resets_at': reset}}}),
            text=True, capture_output=True, env=environment, timeout=5)
        self.assertEqual(invalid.returncode, 0)
        self.assertEqual(fetch_usage(owner='pane-1', cwd=str(self.cwd))['reports'], [])
        with database(cwd=str(self.cwd), host='claude') as db:
            self.assertNotIn('PRIVATE_CANARY', '\n'.join(db.iterdump()))

    def test_hook_binds_actual_tmux_socket_and_pane_not_listener_environment(self):
        with start_receiver(cwd=str(self.cwd)) as independent:
            environment = {**os.environ, **independent.environment(),
                           'TMUX': '/tmp/tmux-owned/default,99,0', 'TMUX_PANE': '%9'}
            command = [sys.executable, str(Path(__file__).with_name('claude_bridge.py')), 'hook']
            started = subprocess.run(command, input=json.dumps({
                'hook_event_name': 'SessionStart', 'session_id': 'actual-from-hook'}),
                text=True, capture_output=True, env=environment, timeout=5)
            self.assertEqual(started.returncode, 0)
            self.assertEqual(started.stdout + started.stderr, '')
            with patch.dict(os.environ, {'TMUX': environment['TMUX'], 'TMUX_PANE': '%9'}):
                current = summary(owner='%9', cwd=str(self.cwd), host='claude')['current']
                self.assertEqual(current['id'], 'actual-from-hook')
                self.assertEqual(capture_availability(owner='%9', cwd=str(self.cwd))['status'],
                                 'incomplete')
            self.assertIsNone(summary(owner='%9', cwd=str(self.cwd), host='claude')['current'])


if __name__ == '__main__':
    unittest.main()
