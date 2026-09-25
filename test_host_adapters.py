"""Host policy and a test-only host exercising real preferences and ledger storage."""
from dataclasses import replace
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import host_adapters
from host_adapters import get_host, HOST_IDS
from preferences import agent_dir, load_preferences, preferences_path, update_preferences
from session_usage import database, ingest, summary


class HostAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {'HOME': str(self.home),
                                               'TMUX': '/tmp/adapter-test,1,0',
                                               'TMUX_PANE': '%1'}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_builtin_profile_roots_follow_current_environment(self):
        omp = get_host('omp')
        claude = get_host('claude')
        with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.home / 'agent-a'),
                                      'CLAUDE_CONFIG_DIR': str(self.home / 'claude-a')}):
            self.assertEqual(agent_dir(host='omp'), self.home / 'agent-a')
            self.assertEqual(agent_dir(host='claude'), self.home / 'claude-a/usage-dashboard')
        with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.home / 'agent-b'),
                                      'CLAUDE_CONFIG_DIR': str(self.home / 'claude-b')}):
            self.assertEqual(omp.data_root(), self.home / 'agent-b')
            self.assertEqual(claude.data_root(), self.home / 'claude-b/usage-dashboard')
            self.assertEqual(omp.data_root('work'), self.home / '.omp/profiles/work/agent')
            self.assertEqual(claude.normalize_profile('../ignored'), None)

    def test_provider_selection_retains_omp_aliases_and_claude_boundary(self):
        omp = get_host('omp')
        claude = get_host('claude')
        self.assertEqual(HOST_IDS, ('omp', 'claude'))
        for alias, provider in (('CODEX', 'openai-codex'), ('copilot', 'github-copilot'),
                                ('gemini', 'google-gemini-cli'), ('GROK', 'xai'),
                                ('deepseek', 'deepseek')):
            with self.subTest(alias=alias):
                self.assertEqual(omp.provider_id(alias), provider)
        self.assertEqual(claude.provider_id('CLAUDE'), 'anthropic')
        with self.assertRaisesRegex(ValueError, 'only supports the Anthropic provider'):
            claude.provider_id('codex')
        with self.assertRaises(ValueError):
            omp.provider_id('../outside')
        with self.assertRaisesRegex(ValueError, 'Unknown dashboard host'):
            get_host('unregistered')

    def test_claude_launch_records_real_child_exit_status(self):
        status_path = self.home / 'exit-status'
        command = get_host('claude').launch_command(
            ['-c', 'raise SystemExit(23)'], binary=sys.executable, status_path=status_path,
        )
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(status_path.read_text(), '23\n')

    def test_registered_third_host_persists_preferences_and_isolates_replayed_usage(self):
        omp = get_host('omp')
        synthetic = replace(
            omp, host_id='synthetic', name='SYNTHETIC', default_providers=('sample-provider',),
            root_resolver=lambda profile: self.home / 'synthetic' / omp.normalize_profile(profile),
        )
        project_a = self.home / 'project-a'
        project_b = self.home / 'project-b'
        project_a.mkdir()
        project_b.mkdir()
        with patch('host_adapters._HOSTS', {**host_adapters._HOSTS, 'synthetic': synthetic}):
            self.assertEqual(load_preferences('work', host='synthetic')['providers'], ['sample-provider'])
            update_preferences('work', {'side': 'left'}, host='synthetic')
            self.assertEqual(load_preferences('work', host='synthetic')['side'], 'left')
            self.assertEqual(load_preferences('personal', host='synthetic')['side'], 'right')
            self.assertEqual(load_preferences('work')['side'], 'right')
            self.assertEqual(preferences_path('work', host='synthetic'),
                             self.home / 'synthetic/work/usage-dashboard.json')

            def event(total):
                return {'session': 'shared-session', 'activation': 'activation',
                        'owner': 'tester', 'socket': '/tmp/adapter-test', 'action': 'start',
                        'entries': [{'id': 'shared-entry', 'at': 60, 'provider': 'sample-provider',
                                     'model': 'model', 'input': total, 'output': 0,
                                     'cacheRead': 0, 'cacheWrite': 0, 'total': total}]}

            ingest(event(17), 'work', cwd=project_a, host='synthetic', now=65)
            ingest(event(999), 'work', cwd=project_a, host='synthetic', now=66)
            ingest(event(23), 'work', cwd=project_b, host='synthetic', now=67)
            first = summary('work', owner='tester', cwd=project_a, host='synthetic', now=70)
            second = summary('work', owner='tester', cwd=project_b, host='synthetic', now=70)
            self.assertEqual(first['current']['providers'][0]['total'], 17)
            self.assertEqual(second['current']['providers'][0]['total'], 23)
            with database('work', host='synthetic') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM tokens').fetchone()[0], 2)
            self.assertFalse((self.home / '.omp/agent/usage-dashboard.sqlite3').exists())
        with self.assertRaisesRegex(ValueError, 'Unknown dashboard host'):
            load_preferences('work', host='synthetic')


if __name__ == '__main__':
    unittest.main()
