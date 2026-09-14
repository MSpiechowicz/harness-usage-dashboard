"""Durable accounting boundaries, without credentials or provider requests."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dashboard import quota_samples, session_lines
from session_usage import active_session, ingest, record_quota, summary


class SessionAccountingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.home / 'agent'),
                                              'OMP_PROFILE': 'default', 'PI_PROFILE': 'default',
                                              'TMUX': '/tmp/accounting-test,1,0', 'TMUX_PANE': '%1'})
        environment.start()
        self.addCleanup(environment.stop)

    def save(self, session='one', activation='a', action='start', entries=(), now=60):
        ingest({'session': session, 'activation': activation, 'action': action,
                'entries': list(entries)}, now=now)

    def entry(self, identity='request-one', at=65, provider='openai-codex', model='gpt-5'):
        return {'id': identity, 'at': at, 'provider': provider, 'model': model, 'input': 100, 'output': 20,
                'cacheRead': 30, 'cacheWrite': 10, 'total': 160}

    def sample(self, at, used, reset=1000, account='account-one'):
        return {'at': at, 'used': used, 'reset': reset, 'provider': 'openai-codex',
                'label': 'Weekly', 'identity': ['openai-codex', account, 'weekly']}

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
        self.assertEqual(report['history'][0]['total'], 320)
        # Closing and reopening connections on every call exercises actual persistence.
        self.assertEqual((self.home / 'agent/usage-dashboard.sqlite3').stat().st_mode & 0o777, 0o600)

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
        self.assertNotIn('+0.00 pp', '\n'.join(line for line, _ in session_lines(summary(), 120, 32)))

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

    def test_quota_rows_appear_only_after_a_comparable_second_sample(self):
        self.save()
        waiting = self.sample(61, .2, account='pending-account')
        waiting['label'] = 'Pending quota'
        record_quota(None, None, 'a', [waiting])
        for compact in (True, False):
            rendered = '\n'.join(line for line, _ in session_lines(summary(), 80, 32, compact))
            self.assertNotIn('Pending quota', rendered)
            self.assertNotIn('Quota change', rendered)
        # A confirmed zero is real data, unlike an unmeasured baseline.
        measured = self.sample(61, .4, account='measured-account')
        measured['label'] = 'Measured quota'
        record_quota(None, None, 'a', [measured, {**measured, 'at': 80}])
        for compact in (True, False):
            rendered = '\n'.join(line for line, _ in session_lines(summary(), 90, 48, compact))
            self.assertNotIn('Pending quota', rendered)
            self.assertIn('Measured quota', rendered)
            self.assertIn('+0.00 pp', rendered)

if __name__ == '__main__':
    unittest.main()
