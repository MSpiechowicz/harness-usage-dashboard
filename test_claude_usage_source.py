"""Behavioral coverage for observed Claude statusLine allowance windows."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest.mock import patch

from claude_bridge import start_receiver
from claude_usage_source import fetch_usage
from session_usage import database


class ClaudeAllowanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': self.tmp.name,
                                           'TMUX': '', 'TMUX_PANE': ''})
        self.home.start()
        self.addCleanup(self.home.stop)
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.receiver = start_receiver(owner='pane-allowance', cwd=str(self.project))
        self.addCleanup(self.receiver.close)
        self.post('/hook', {'hook_event_name': 'SessionStart', 'session_id': 'session-A'})

    def post(self, path, data):
        request = urllib.request.Request(self.receiver.address + path,
            headers={'Authorization': 'Bearer ' + self.receiver.auth,
                     'Content-Type': 'application/json'},
            data=json.dumps(data).encode(), method='POST')
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status

    def report(self, now=None):
        return fetch_usage(owner='pane-allowance', cwd=str(self.project), now=now)

    def test_pro_max_windows_map_to_anthropic_report_without_account_or_quota_history(self):
        reset = time.time() + 3600
        self.post('/statusline', {'session_id': 'session-A', 'rate_limits': {
            'five_hour': {'used_percentage': 37.5, 'resets_at': reset},
            'seven_day': {'used_percentage': 84, 'resets_at': reset + 86400}}})
        result = self.report()
        self.assertEqual(len(result['reports']), 1)
        report = result['reports'][0]
        self.assertEqual(report['provider'], 'anthropic')
        self.assertEqual([(limit['id'], limit['amount']['usedFraction'])
                          for limit in report['limits']],
                         [('claude:five_hour', .375), ('claude:seven_day', .84)])
        self.assertEqual([limit['window']['resetsAt'] for limit in report['limits']],
                         [int(reset * 1000), int((reset + 86400) * 1000)])
        self.assertTrue(all('accountId' not in limit['scope'] for limit in report['limits']))
        with database(cwd=str(self.project), host='claude') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM quota').fetchone()[0], 0)
        command = [sys.executable, str(Path(__file__).with_name('claude_usage_source.py')),
                   '--owner', 'pane-allowance']
        cli = subprocess.run(command, cwd=self.project, env=os.environ.copy(),
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(cli.returncode, 0)
        self.assertEqual(json.loads(cli.stdout)['reports'][0]['provider'], 'anthropic')

    def test_absent_partial_stale_and_expired_windows_never_infer_zero(self):
        self.assertEqual(self.report()['reports'], [])
        reset = time.time() + 700
        self.post('/statusline', {'session_id': 'session-A', 'rate_limits': {
            'five_hour': {'used_percentage': 0, 'resets_at': reset}}})
        result = self.report()
        self.assertEqual(len(result['reports'][0]['limits']), 1)
        self.assertEqual(result['reports'][0]['limits'][0]['amount']['usedFraction'], 0)
        self.post('/statusline', {'session_id': 'session-A'})
        self.assertEqual(self.report()['reports'], [])
        self.post('/statusline', {'session_id': 'session-A', 'rate_limits': {
            'seven_day': {'used_percentage': 20}}})
        partial = self.report()['reports'][0]['limits'][0]
        self.assertEqual(partial['amount']['usedFraction'], .2)
        self.assertNotIn('window', partial)
        self.post('/statusline', {'session_id': 'session-A', 'rate_limits': {
            'five_hour': {'used_percentage': 0, 'resets_at': reset}}})
        self.assertEqual(self.report()['reports'][0]['limits'][0]['id'], 'claude:five_hour')
        self.assertEqual(self.report(now=time.time() + 400)['reports'], [])
        self.assertEqual(self.report(now=reset + 1)['reports'], [])
        self.assertIn('unknown, not zero', self.report(now=reset + 1)['dashboardNote'])

    def test_new_session_and_wrong_project_never_reuse_previous_limits(self):
        reset = time.time() + 3600
        self.post('/statusline', {'session_id': 'session-A', 'rate_limits': {
            'five_hour': {'used_percentage': 75, 'resets_at': reset}}})
        self.post('/hook', {'hook_event_name': 'SessionEnd', 'session_id': 'session-A', 'reason': 'clear'})
        self.post('/hook', {'hook_event_name': 'SessionStart', 'session_id': 'session-B', 'source': 'clear'})
        self.assertEqual(self.report()['reports'], [])
        self.post('/statusline', {'session_id': 'session-A', 'rate_limits': {
            'five_hour': {'used_percentage': 40, 'resets_at': reset}}})
        self.assertEqual(self.report()['reports'], [])
        self.assertEqual(fetch_usage(owner='other', cwd=str(self.project))['reports'], [])
        other = Path(self.tmp.name) / 'different-project'
        other.mkdir()
        self.assertEqual(fetch_usage(owner='pane-allowance', cwd=str(other))['reports'], [])

    def test_invalid_statusline_values_and_private_fields_not_stored(self):
        self.post('/statusline', {'session_id': 'session-A', 'rate_limits': {
            'five_hour': {'used_percentage': -1, 'resets_at': time.time() + 300},
            'seven_day': {'used_percentage': '50', 'resets_at': time.time() + 300}},
            'user_email': 'PRIVATE_EMAIL_CANARY', 'prompt': 'PRIVATE_PROMPT_CANARY'})
        self.assertEqual(self.report()['reports'], [])
        with database(cwd=str(self.project), host='claude') as db:
            dump = '\n'.join(db.iterdump())
        self.assertNotIn('PRIVATE_EMAIL_CANARY', dump)
        self.assertNotIn('PRIVATE_PROMPT_CANARY', dump)


if __name__ == '__main__':
    unittest.main()
