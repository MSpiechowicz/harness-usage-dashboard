"""Durable accounting boundaries, without credentials or provider requests."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dashboard import format_tokens, quota_samples, session_lines
from session_usage import active_session, database, ingest, record_quota, summary


class SessionAccountingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.home / 'agent'),
                                              'CLAUDE_CONFIG_DIR': str(self.home / 'claude-config'),
                                              'OMP_PROFILE': 'default', 'PI_PROFILE': 'default',
                                              'TMUX': '/tmp/accounting-test,1,0', 'TMUX_PANE': '%1'})
        environment.start()
        self.addCleanup(environment.stop)

    def save(self, session='one', activation='a', action='start', entries=(), now=60, cwd=None):
        ingest({'session': session, 'activation': activation, 'action': action,
                'entries': list(entries)}, now=now, cwd=cwd)

    def entry(self, identity='request-one', at=65, provider='openai-codex', model='gpt-5',
              thinking='unknown', total=160):
        return {'id': identity, 'at': at, 'provider': provider, 'model': model,
                'thinkingLevel': thinking, 'input': 100, 'output': 20,
                'cacheRead': 30, 'cacheWrite': 10, 'total': total}

    def sample(self, at, used, reset=1000, account='account-one'):
        return {'at': at, 'used': used, 'reset': reset, 'provider': 'openai-codex',
                'label': 'Weekly', 'identity': ['openai-codex', account, 'weekly']}

    def test_refresh_uses_one_snapshot_while_usage_arrives(self):
        self.save(entries=[self.entry('earlier', at=65, total=370_000)])
        with database() as db:
            db.execute('PRAGMA journal_mode=WAL')

        arrived = False

        @contextmanager
        def concurrent_database(profile=None, cwd=None, *, host='omp'):
            nonlocal arrived
            with database(profile, cwd, host=host) as db:
                def receive_usage(statement):
                    nonlocal arrived
                    if not arrived and 'SELECT provider, model,' in statement and 'thinking_level' not in statement:
                        arrived = True
                        self.save(action='sync',
                                  entries=[self.entry('incoming', at=125, total=17_000)],
                                  now=130)
                db.set_trace_callback(receive_usage)
                yield db

        with patch('session_usage.database', concurrent_database):
            report = summary(now=150, minutes=3)
        self.assertTrue(arrived)
        self.assertEqual(report['current']['providers'][0]['total'], 370_000)
        self.assertEqual(report['current']['model_summaries'][0]['total'], 370_000)
        self.assertEqual(sum(row['total'] for row in report['total_history']), 370_000)
        self.assertEqual(report['chart'], [0, 370_000, 0])

        report = summary(now=150, minutes=3)
        self.assertEqual(report['current']['providers'][0]['total'], 387_000)
        self.assertEqual(sum(row['total'] for row in report['total_history']), 387_000)
        self.assertEqual(report['chart'], [0, 370_000, 17_000])

    def test_task_aggregate_reconciles_in_either_order_and_survives_replay(self):
        for child_first in (False, True):
            with self.subTest(child_first=child_first):
                group = str(child_first)
                aggregate = {**self.entry(f'aggregate-{group}', provider='task (mixed)',
                                         model='mixed / unattributed', total=320),
                             'input': 200, 'output': 40, 'cacheRead': 60, 'cacheWrite': 20,
                             'taskGroup': group, 'taskAggregate': True}
                child = {**self.entry(f'child-{group}', model=f'model-{group}'), 'taskGroup': group}
                self.save(entries=[child if child_first else aggregate])
                self.save(action='sync', entries=[aggregate if child_first else child])
                models = summary(now=90)['current']['models']
                mixed = next(row for row in models if row['provider'] == 'task (mixed)')
                self.assertEqual([mixed[key] for key in ('input', 'output', 'cache_read', 'cache_write', 'total')],
                                 [100, 20, 30, 10, 160])
                # Replaying the original parent result must not restore its full total.
                self.save(activation='resumed', entries=[aggregate, child])
                self.save(action='sync', entries=[{**child, 'id': f'second-child-{group}'}])
                report = summary(now=90)
                self.assertFalse(any(row['provider'] == 'task (mixed)' for row in report['current']['models']))
                model = next(row for row in report['current']['models'] if row['model'] == f'model-{group}')
                self.assertEqual([model[key] for key in ('input', 'output', 'cache_read', 'cache_write', 'total')],
                                 [200, 40, 60, 20, 320])
                self.assertEqual(sum(report['chart']), 320 * (1 + int(child_first)))

    def test_late_child_reconciliation_does_not_reactivate_parent_or_cross_sessions(self):
        aggregate = {**self.entry('aggregate', provider='task (mixed)'), 'taskGroup': 'shared',
                     'taskAggregate': True}
        self.save(entries=[aggregate])
        self.save(session='two', activation='b',
                  entries=[{**self.entry('new-parent-child'), 'taskGroup': 'shared'}])
        self.assertEqual(summary(now=90)['previous']['models'][0]['total'], 160)
        child = {**self.entry('old-parent-child', model='child-model'), 'taskGroup': 'shared'}
        self.save(action='sync', entries=[child])
        report = summary(now=90)
        self.assertEqual(report['current']['id'], 'two')
        self.assertEqual([(row['model'], row['total']) for row in report['previous']['models']],
                         [('child-model', 160)])
        self.assertEqual(sum(row['total'] for row in report['total_history']), 320)

    def test_restart_resume_and_fork_do_not_recount_requests(self):
        entry = self.entry()
        self.save(entries=[entry])
        self.save(action='sync', entries=[entry], now=80)
        self.save(action='stop', now=90)
        self.save(activation='resumed', entries=[entry], now=100)
        self.save(session='two', activation='b', entries=[entry, self.entry('new', 121)], now=120)
        report = summary(now=130)
        self.assertEqual(report['previous']['id'], 'one')
        self.assertEqual(report['previous']['providers'][0]['total'], 160)
        self.assertEqual(report['current']['providers'][0]['total'], 160)
        self.assertEqual(report['history'][0]['total'], 160)
        # Closing and reopening connections on every call exercises actual persistence.
        self.assertEqual((self.home / 'agent/usage-dashboard.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_rejects_exposed_existing_ledger_without_modifying_it(self):
        path = self.home / 'agent' / 'usage-dashboard.sqlite3'
        path.parent.mkdir()
        for mode in (0o644, 0o660):
            with self.subTest(mode=oct(mode)):
                path.write_bytes(b'private existing ledger')
                path.chmod(mode)
                original = path.read_bytes()
                with self.assertRaises(PermissionError):
                    self.save()
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(path.stat().st_mode & 0o777, mode)

    def test_rejects_ledger_symlink_without_modifying_target(self):
        path = self.home / 'agent' / 'usage-dashboard.sqlite3'
        path.parent.mkdir()
        target = self.home / 'target.sqlite3'
        target.write_bytes(b'private existing ledger')
        path.symlink_to(target)

        with self.assertRaises(PermissionError):
            self.save()
        self.assertEqual(target.read_bytes(), b'private existing ledger')
        self.assertTrue(path.is_symlink())

    def test_rejects_writable_parent_without_creating_ledger(self):
        directory = self.home / 'agent'
        directory.mkdir()
        directory.chmod(0o777)
        with self.assertRaises(PermissionError):
            self.save()
        self.assertFalse((directory / 'usage-dashboard.sqlite3').exists())

    def test_rejects_symlinked_parent(self):
        directory = self.home / 'agent'
        target = self.home / 'storage'
        target.mkdir()
        directory.symlink_to(target, target_is_directory=True)
        with self.assertRaises(PermissionError):
            self.save()
        self.assertFalse((target / 'usage-dashboard.sqlite3').exists())

    def test_existing_ledger_migrates_thinking_level_without_losing_tokens(self):
        path = self.home / 'agent' / 'usage-dashboard.sqlite3'
        path.parent.mkdir(parents=True)
        connection = sqlite3.connect(path)
        connection.execute('''CREATE TABLE tokens (
            project TEXT NOT NULL, id TEXT NOT NULL, session TEXT NOT NULL, at REAL NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, input INTEGER NOT NULL, output INTEGER NOT NULL,
            cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL, total INTEGER NOT NULL,
            PRIMARY KEY (project, id)
        )''')
        connection.execute(
            'INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ('project', 'old', 'session', 1, 'provider', 'model', 1, 2, 3, 4, 10),
        )
        connection.commit()
        connection.close()
        path.chmod(0o600)

        with database() as db:
            row = db.execute('SELECT thinking_level, total FROM tokens WHERE id=?', ('old',)).fetchone()
            self.assertEqual(dict(row), {'thinking_level': 'unknown', 'total': 10})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


    def test_history_total_excludes_current_session(self):
        self.save(session='previous', activation='previous',
                  entries=[self.entry('previous-one'), self.entry('previous-two')], now=60)
        self.save(session='current', activation='current', entries=[self.entry('current')], now=100)
        report = summary(now=110)
        self.assertEqual(report['previous']['providers'][0]['total'], 320)
        self.assertEqual(report['current']['providers'][0]['total'], 160)
        self.assertEqual(sum(item['total'] for item in report['history']), 320)
        self.assertEqual(sum(item['total'] for item in report['total_history']), 480)
        rendered = '\n'.join(line for line, _ in session_lines(report, 110, 32))
        self.assertIn('HISTORY OTHER SESSIONS', rendered)
        self.assertIn('HISTORY TOTAL', rendered)
        self.assertNotIn('Other sessions', rendered)
        self.assertNotIn('Project total', rendered)
        self.assertEqual([style for line, style in session_lines(report, 110, 32)
                          if line.startswith('Last recorded ')], ['secondary'])

    def test_history_is_project_scoped_and_total_history_is_available(self):
        first = self.home / 'first-project'
        second = self.home / 'second-project'
        self.save(session='old', activation='first-old', entries=[self.entry('old-entry')],
                  cwd=first, now=60)
        self.save(session='current', activation='first-current', entries=[self.entry('current-entry')],
                  cwd=first, now=100)
        self.save(session='old', activation='second-old', entries=[self.entry('old-entry')],
                  cwd=second, now=120)
        self.save(session='current', activation='second-current', entries=[self.entry('current-entry')],
                  cwd=second, now=140)

        first_report = summary(cwd=first, now=150)
        self.assertEqual(sum(item['total'] for item in first_report['history']), 160)
        self.assertEqual(sum(item['total'] for item in first_report['total_history']), 320)
        self.assertEqual(first_report['previous']['id'], 'old')


    def test_claude_ledger_isolated_by_host_and_project_with_same_identities(self):
        first = self.home / 'first-project'
        second = self.home / 'second-project'
        claude_root = self.home / 'claude-config'
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(claude_root), 'OMP_PROFILE': 'work'}):
            shared = {'session': 'same', 'activation': 'same', 'owner': 'same-owner',
                      'action': 'start', 'entries': [self.entry('same-request')]}
            ingest(shared, profile='default', cwd=first, now=60)
            self.assertIsNone(active_session(cwd=first, owner='same-owner', host='claude'))

            ingest({**shared, 'entries': [self.entry('same-request', total=320)]},
                   profile='../ignored', cwd=first, now=70, host='claude')
            ingest({**shared, 'entries': [self.entry('same-request', total=480)]},
                   cwd=second, now=80, host='claude')
            self.assertEqual(summary(profile='default', cwd=first, owner='same-owner')['current']
                             ['providers'][0]['total'], 160)
            self.assertEqual(summary(profile='work', cwd=first, owner='same-owner', host='claude')
                             ['current']['providers'][0]['total'], 320)
            self.assertEqual(summary(cwd=second, owner='same-owner', host='claude')
                             ['current']['providers'][0]['total'], 480)
            self.assertEqual(summary(cwd=second, owner='same-owner', host='claude')['history'], [])
            self.assertEqual(active_session(cwd=first, owner='same-owner', host='claude')
                             ['session'], 'same')
            self.assertTrue((claude_root / 'usage-dashboard/usage-dashboard.sqlite3').exists())

            sample = self.sample(75, .3)
            record_quota('work', 'same-owner', 'same', [sample], cwd=first, host='claude')
            self.assertEqual(len(summary(cwd=first, owner='same-owner', host='claude')
                                 ['current']['quota']), 1)
            self.assertEqual(summary(profile='default', cwd=first, owner='same-owner')
                             ['current']['quota'], [])

    def test_late_stop_and_replayed_entry_preserve_new_active_session(self):
        project = self.home / 'late-events-project'
        ingest({'session': 'old', 'activation': 'shared', 'action': 'start',
                'entries': [self.entry('old-request')]}, now=60, cwd=project, host='claude')
        ingest({'session': 'new', 'activation': 'shared', 'action': 'start',
                'entries': [self.entry('new-request', total=300)]},
               now=90, cwd=project, host='claude')
        ingest({'session': 'old', 'activation': 'shared', 'action': 'stop',
                'entries': [self.entry('old-request', total=900)]},
               now=100, cwd=project, host='claude')
        self.assertEqual(active_session(cwd=project, host='claude')['session'], 'new')
        self.assertEqual(active_session(cwd=project, host='claude')['activation'], 'shared')
        self.assertEqual(summary(now=110, cwd=project, host='claude')['current']
                         ['providers'][0]['total'], 300)
        self.assertEqual(summary(now=110, cwd=project, host='claude')['history'][0]['total'], 160)

    def test_cli_host_flag_writes_only_claude_ledger(self):
        project = self.home / 'cli-project'
        project.mkdir()
        payload = {'session': 'cli-session', 'activation': 'cli-activation',
                   'action': 'start', 'entries': [self.entry('cli-request')]}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().with_name('session_usage.py')),
             '--host', 'claude', '--profile', '../ignored'],
            input=json.dumps(payload), text=True, capture_output=True, cwd=project,
            env=dict(os.environ, OMP_PROFILE='work'),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary(cwd=project, host='claude')['current']['id'], 'cli-session')
        self.assertIsNone(summary(profile='default', cwd=project)['current'])

    def test_large_token_totals_use_compact_units(self):
        self.assertEqual(format_tokens(999), '999')
        self.assertEqual(format_tokens(1_000), '1k')
        self.assertEqual(format_tokens(12_345), '12.3k')
        self.assertEqual(format_tokens(1_234_567), '1.23M')

    def test_compact_totals_preserve_significant_integer_zeroes(self):
        self.assertEqual(
            [format_tokens(value) for value in (169_000, 170_000, 171_000)],
            ['169k', '170k', '171k'],
        )
        self.assertEqual(format_tokens(99_999), '100k')


    def test_resets_and_resume_gaps_are_not_subtracted_or_charged(self):
        self.save()
        for item in [self.sample(61, .2), self.sample(70, .3), self.sample(80, .02, 2000),
                     self.sample(90, .06, 2000), self.sample(85, .9, 2000)]:
            record_quota(None, None, 'a', [item])
        self.save(action='stop', now=100)
        record_quota(None, None, 'a', [self.sample(110, .9, 2000)])
        self.save(activation='resumed', now=200)
        record_quota(None, None, 'resumed', [self.sample(190, .8, 2000)])
        for item in [self.sample(201, .8, 2000), self.sample(210, .85, 2000)]:
            record_quota(None, None, 'resumed', [item])
        quota = summary(now=220)['current']['quota'][0]
        self.assertAlmostEqual(quota['points'], 19)
        self.assertEqual(quota['segments'], 3)
        self.assertEqual(quota['intervals'], 3)

    def test_decreasing_counter_and_duplicate_sample_begin_no_fake_spend(self):
        self.save()
        for item in [self.sample(61, .6), self.sample(70, .7), self.sample(70, .9),
                     self.sample(80, .5), self.sample(90, .55)]:
            record_quota(None, None, 'a', [item])
        quota = summary()['current']['quota'][0]
        self.assertAlmostEqual(quota['points'], 15)
        self.assertEqual(quota['segments'], 2)

    def test_late_previous_session_poll_cannot_contaminate_new_session(self):
        self.save()
        self.save(session='two', activation='b', now=100)
        record_quota(None, None, 'a', [self.sample(110, .9)])
        record_quota(None, None, 'b', [self.sample(90, .8)])
        self.assertEqual(summary()['current']['quota'], [])
        record_quota(None, None, 'b', [self.sample(111, .9)])
        quota = summary()['current']['quota'][0]
        self.assertEqual(quota['intervals'], 0)
        self.assertNotIn('+0.00%', '\n'.join(line for line, _ in session_lines(summary(), 120, 32)))

    def test_accounts_are_not_paired_by_report_order(self):
        self.save()
        for account, before, after in [('first', .2, .3), ('second', .8, .82)]:
            record_quota(None, None, 'a', [self.sample(61, before, account=account)])
            record_quota(None, None, 'a', [self.sample(80, after, account=account)])
        points = sorted(row['points'] for row in summary()['current']['quota'])
        self.assertAlmostEqual(points[0], 2)
        self.assertAlmostEqual(points[1], 10)
        data = {'reports': [{'provider': 'openai-codex', 'fetchedAt': 61000, 'limits': [
            {'id': 'weekly', 'amount': {'usedFraction': .3}},
        ]}]}
        self.assertEqual(quota_samples(data), [])
        data['reports'][0]['limits'][0]['scope'] = {'accountId': 'private-account'}
        record_quota(None, None, 'a', quota_samples(data))
        self.assertNotIn(b'private-account', (self.home / 'agent/usage-dashboard.sqlite3').read_bytes())

    def test_chart_has_empty_minutes_and_excludes_future_and_previous_usage(self):
        self.save(entries=[self.entry('old', 59), self.entry('now', 121), self.entry('future', 181)])
        report = summary(now=150, minutes=3)
        self.assertEqual(report['chart'], [160, 0, 160])
        self.save(session='two', activation='b', now=155)
        self.assertEqual(summary(now=160, minutes=3)['chart'], [0, 0, 0])

    def test_tmux_servers_and_profiles_have_independent_current_sessions(self):
        self.save()
        with patch.dict(os.environ, {'TMUX': '/tmp/another-server,1,0'}):
            self.save(session='other', activation='b')
            self.assertEqual(active_session()['session'], 'other')
        self.assertEqual(active_session()['session'], 'one')
        with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.home / 'separate-agent')}):
            self.assertIsNone(summary()['current'])
            self.assertEqual(summary()['history'], [])


    def test_explicit_hook_socket_matches_dashboard_pane_when_listener_is_outside_tmux(self):
        project = self.home / 'listener-project'
        payload = {'session': 'hook-session', 'activation': 'hook-activation',
                   'owner': '%1', 'socket': '/tmp/accounting-test', 'action': 'start',
                   'entries': [self.entry('hook-request')]}
        with patch.dict(os.environ, {'TMUX': ''}):
            ingest(payload, cwd=project, host='claude', now=60)
            self.assertIsNone(summary(owner='%1', cwd=project, host='claude')['current'])

        self.assertEqual(active_session(owner='%1', cwd=project, host='claude')['session'],
                         'hook-session')
        self.assertEqual(summary(owner='%1', cwd=project, host='claude')['current']
                         ['providers'][0]['total'], 160)
        with patch.dict(os.environ, {'TMUX': ''}):
            ingest({**payload, 'action': 'stop', 'entries': []},
                   cwd=project, host='claude', now=90)
        self.assertIsNone(summary(owner='%1', cwd=project, host='claude')['current'])
        self.assertEqual(summary(owner='%1', cwd=project, host='claude')['previous']
                         ['id'], 'hook-session')

    def test_explicit_owner_socket_rejects_malformed_or_oversized_values(self):
        project = self.home / 'invalid-socket-project'
        for socket in (4, '\x00', '/tmp/socket\n', 'x' * 4097):
            with self.subTest(socket=socket):
                with self.assertRaises(ValueError):
                    ingest({'session': 'invalid', 'activation': 'invalid', 'socket': socket,
                            'action': 'start'}, cwd=project, host='claude')
        self.assertIsNone(active_session(cwd=project, host='claude'))

    def test_model_switch_preserves_routes_without_multiplying_shared_quota(self):
        self.save(entries=[
            self.entry('first', model='gpt-5'),
            self.entry('second', model='gpt-5-mini'),
            self.entry('third', provider='anthropic', model='claude-sonnet'),
        ])
        for sample in (self.sample(61, .2), self.sample(80, .25)):
            record_quota(None, None, 'a', [sample])
        report = summary(now=100)
        self.assertEqual({row['provider']: row['total'] for row in report['current']['providers']},
                         {'openai-codex': 320, 'anthropic': 160})
        self.assertEqual({(row['provider'], row['model']): row['total'] for row in report['current']['models']},
                         {('openai-codex', 'gpt-5'): 160, ('openai-codex', 'gpt-5-mini'): 160,
                          ('anthropic', 'claude-sonnet'): 160})
        self.assertEqual(len(report['current']['quota']), 1)
        self.assertAlmostEqual(report['current']['quota'][0]['points'], 5)
        self.save(session='next', activation='next', now=120)
        previous = summary(now=130)['previous']
        self.assertEqual(previous['models'], report['current']['models'])
        self.assertEqual(previous['quota'], report['current']['quota'])

    def test_thinking_levels_are_stored_and_model_summaries_are_combined(self):
        self.save(entries=[
            self.entry('luna-max', model='Luna', thinking='max', total=5000),
            self.entry('luna-xhigh', model='Luna', thinking='xhigh', total=4000),
            self.entry('opus', model='Opus 5', thinking='medium'),
        ])

        report = summary(now=100)
        current = report['current']
        levels = {(item['model'], item['thinking_level']): item['total'] for item in current['models']}
        combined = {(item['model'], item['total']) for item in current['model_summaries']}
        rendered = '\n'.join(line for line, _ in session_lines(report, 100, 80, compact=False))

        self.assertEqual(levels, {('Luna', 'max'): 5000, ('Luna', 'xhigh'): 4000, ('Opus 5', 'medium'): 160})
        self.assertEqual(combined, {('Luna', 9000), ('Opus 5', 160)})
        self.assertIn('Luna - Max tokens', rendered)
        self.assertIn('Luna - xHigh tokens', rendered)
        self.assertNotIn('Luna (summary) tokens', rendered)
        self.assertNotIn('Luna (summary)', rendered)
        self.assertIn('Opus 5 - Medium tokens', rendered)

    def test_detailed_mode_keeps_model_breakdowns_in_history(self):
        self.save(session='previous', activation='previous', entries=[
            self.entry('previous-luna-high', model='Luna', thinking='high', total=5000),
            self.entry('previous-luna-xhigh', model='Luna', thinking='xhigh', total=4000),
            self.entry('previous-opus', model='Opus 5', thinking='medium'),
        ], now=60)
        self.save(session='current', activation='current', entries=[
            self.entry('current-luna-high', model='Luna', thinking='high', total=5000),
            self.entry('current-luna-xhigh', model='Luna', thinking='xhigh', total=4000),
            self.entry('current-opus', model='Opus 5', thinking='medium'),
        ], now=100)

        report = summary(now=110)
        rendered = '\n'.join(line for line, _ in session_lines(report, 110, 80, compact=False))

        self.assertIn('Luna - High tokens', rendered)
        self.assertIn('Luna - xHigh tokens', rendered)
        self.assertNotIn('Luna (summary) tokens', rendered)
        self.assertIn('CODEX / Luna - High tokens', rendered)
        self.assertIn('CODEX / Luna - xHigh tokens', rendered)
        self.assertNotIn('CODEX / Luna (summary)', rendered)
        self.assertIn('Opus 5 - Medium tokens', rendered)
        self.assertIn('In 100 / out 20', rendered)
        self.assertIn('Cache r 30 / w 10', rendered)
        self.assertEqual(
            {(item['provider'], item['model'], item['thinking_level'], item['input'], item['output'],
              item['cache_read'], item['cache_write'])
             for item in report['history']},
            {('openai-codex', 'Luna', 'high', 100, 20, 30, 10),
             ('openai-codex', 'Luna', 'xhigh', 100, 20, 30, 10),
             ('openai-codex', 'Opus 5', 'medium', 100, 20, 30, 10)},
        )


    def test_shared_model_bucket_is_observed_once(self):
        self.save()
        limits = [{'id': 'weekly', 'label': 'Weekly',
                   'scope': {'accountId': 'same-account', 'modelId': model, 'shared': True},
                   'amount': {'usedFraction': .2}} for model in ('large', 'small')]
        data = {'reports': [{'provider': 'openai-codex', 'fetchedAt': 61000, 'limits': limits}]}
        record_quota(None, None, 'a', quota_samples(data))
        data['reports'][0]['fetchedAt'] = 80000
        for limit in limits:
            limit['amount']['usedFraction'] = .25
        record_quota(None, None, 'a', quota_samples(data))
        quota = summary()['current']['quota']
        self.assertEqual(len(quota), 1)
        self.assertAlmostEqual(quota[0]['points'], 5)
        self.assertEqual(quota[0]['intervals'], 1)

    def test_quota_rows_require_a_visible_accumulated_change(self):
        self.save(entries=[self.entry(model='Quota model')])
        waiting = self.sample(61, .2, account='pending-account')
        waiting['label'] = 'Pending quota'
        record_quota(None, None, 'a', [waiting])
        for compact in (True, False):
            rendered = '\n'.join(line for line, _ in session_lines(summary(), 80, 32, compact))
            self.assertNotIn('Pending quota', rendered)
            self.assertNotIn('Quota change', rendered)
        # Repeated unchanged polls and sub-display increases stay hidden.
        measured = self.sample(61, .4, account='measured-account')
        measured['label'] = 'Measured quota'
        record_quota(None, None, 'a', [measured, {**measured, 'at': 80}])
        for at, used in ((90, .4), (100, .40004), (110, .40008), (120, .40008)):
            record_quota(None, None, 'a', [{**measured, 'at': at, 'used': used}])
            report = summary()
            for compact in (True, False):
                with self.subTest(at=at, compact=compact):
                    rendered = '\n'.join(line for line, _ in session_lines(report, at, 48, compact))
                    self.assertNotIn('Pending quota', rendered)
                    self.assertNotIn('+0.00%', rendered)
                    if at < 110:
                        self.assertNotIn('Measured quota', rendered)
                        self.assertNotIn('Quota change', rendered)
                    elif compact:
                        self.assertIn('Measured quota', rendered)
                        self.assertIn('+0.01%', rendered)
                        lines = rendered.splitlines()
                        heading = lines.index('Quota change (observed)')
                        self.assertEqual(lines[heading - 1], '')
                    else:
                        quota = next(item for item in report['current']['quota']
                                     if item['label'] == 'Measured quota')
                        self.assertAlmostEqual(quota['points'], .008)
                        self.assertGreater(quota['intervals'], 0)
                        self.assertNotIn('Measured quota', rendered)
                        self.assertNotIn('+0.01%', rendered)
                        self.assertNotIn('Quota change (observed)', rendered)
                        self.assertNotIn('[' + quota['key'][:6] + ']', rendered)
                        self.assertNotIn('observation segments', rendered)
                        self.assertNotIn('Last sample', rendered)
                        self.assertNotIn('% = percentage-point change', rendered)
                        self.assertNotIn('Account-wide; not exact billing', rendered)
                        self.assertIn('CURRENT SESSION', rendered)
                        self.assertIn('Quota model tokens', rendered)
                        self.assertIn('In 100 / out 20', rendered)
                        self.assertIn('HISTORY TOTAL', rendered)

if __name__ == '__main__':
    unittest.main()
