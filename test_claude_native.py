"""Native accounting, privacy, lifecycle and allowance boundaries in disposable storage."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from claude_native import RequestError, handle_request
from preferences import agent_dir, load_preferences, preferences_path, update_preferences
from session_usage import active_session, database, ingest, summary

ROOT = Path(__file__).resolve().parent


class NativeHelperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.cwd = self.home / 'project'
        self.cwd.mkdir()
        environment = patch.dict(os.environ, {
            'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': str(self.home / 'claude'),
            'TMUX': '/private/inherited/tmux,123,0', 'TMUX_PANE': '%9',
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.context = {'version': 1, 'cwd': str(self.cwd), 'owner': 'native-one',
                        'session': 'session-one', 'activation': 'activation-one'}

    def entry(self, identity='request', at=101, **changes):
        return {'id': hashlib.sha256(identity.encode()).hexdigest(), 'at': at,
                'provider': 'anthropic', 'model': 'claude-sonnet', 'thinkingLevel': 'high',
                'input': 12, 'output': 7, 'cacheRead': 4, 'cacheWrite': 2, 'total': 25, **changes}

    def capture(self, action='start', entries=(), now=100, **changes):
        return handle_request({**self.context, 'op': 'capture', 'action': action,
                               'entries': list(entries), 'incomplete': False, **changes}, now=now)

    def snapshot(self, now=110, **changes):
        return handle_request({**self.context, 'op': 'snapshot', 'width': 80,
                               'allowance': None, **changes}, now=now)

    def assert_chart_frame(self, result, chart_type, awaiting=False, plotted=False):
        rows = result['rows']
        heading = 'TOKEN TRACE' if chart_type == 'trace' else 'TOKEN RATE'
        headings = [index for index, row in enumerate(rows) if row['text'].startswith(heading)]
        self.assertEqual(len(headings), 1)
        start = headings[0] + 1
        notes = [row for row in rows if 'awaiting' in row['text'].lower()]
        self.assertEqual(len(notes), int(awaiting))
        if awaiting:
            self.assertIs(rows[start], notes[0])
            self.assertIn('native', notes[0]['text'].lower())
            self.assertEqual(notes[0]['token'], 'warn')
            start += 1

        frame = rows[start:start + 9]
        self.assertEqual(len(frame), 9)
        self.assertEqual(frame[0]['text'], '')
        plot = frame[1:7]
        self.assertTrue(all(row['text'][5] in '│|' for row in plot))
        marks = ''.join(row['text'][6:] for row in plot).strip(' \u2800')
        self.assertEqual(bool(marks), plotted)
        for row in plot:
            self.assertEqual(bool(row['emphasis']), plotted)
            if plotted:
                self.assertEqual(row['emphasis']['token'], 'chart')
        self.assertEqual(frame[7]['text'][:5].strip(), '0')
        self.assertIn(frame[7]['text'][5], '└+')
        self.assertTrue(set(frame[7]['text'][6:]) <= set('─-'))
        self.assertTrue(frame[8]['text'].strip().startswith('-'))
        self.assertTrue(frame[8]['text'].endswith('now'))
        self.assertEqual(rows[start + 9]['text'], '')
        self.assertTrue(rows[start + 10]['text'].startswith('CURRENT SESSION'))
        return rows[start + 11]

    def invoke(self, request):
        # Exercise the real JSON subprocess with curses deliberately unavailable.
        command = [sys.executable, '-c',
                   'import os, runpy, sys; sys.path.insert(0, os.path.dirname(sys.argv[1])); '
                   'sys.modules["curses"] = None; '
                   'runpy.run_path(sys.argv[1], run_name="__main__")', str(ROOT / 'claude_native.py')]
        return subprocess.run(command, input=json.dumps(request), text=True,
                              capture_output=True, env=dict(os.environ), cwd=self.cwd, timeout=10)

    def test_owners_ignore_inherited_tmux_and_history_remains_host_project_scoped(self):
        ingest({'session': 'old-omp', 'activation': 'omp', 'action': 'start',
                'owner': 'native-one', 'entries': [self.entry('omp')]}, cwd=self.cwd, now=90)
        self.capture(entries=[self.entry()])
        self.capture(owner='native-two', session='session-two', activation='activation-two',
                     entries=[self.entry('second', input=2, output=0, cacheRead=0, cacheWrite=0, total=2)])
        with patch.dict(os.environ, {'TMUX': '/another/socket,44,1', 'TMUX_PANE': '%44'}):
            self.assertEqual(self.snapshot()['history']['current']['providers'][0]['total'], 25)
            second = self.snapshot(owner='native-two', session='session-two', activation='activation-two')
            self.assertEqual(second['history']['current']['providers'][0]['total'], 2)
            self.assertIsNone(active_session(owner='native-one', cwd=self.cwd, host='claude'))
            self.assertEqual(active_session(owner='native-one', cwd=self.cwd, host='claude', socket='')['session'],
                             'session-one')
        isolated = summary(owner='native-one', cwd=self.home / 'another-project', host='claude', socket='')
        self.assertEqual(isolated['total_history'], [])
        self.assertEqual(summary(owner='native-one', cwd=self.cwd)['current']['id'], 'old-omp')

    def test_unknown_chart_becomes_reported_zero_then_positive_and_thinking_survives(self):
        self.capture()
        for chart_type in ('bars', 'dots', 'trace'):
            with self.subTest(chart_type=chart_type, state='unknown'):
                update_preferences(None, {'chart_type': chart_type}, host='claude')
                first = self.snapshot()
                self.assertEqual(first['capture']['state'], 'unknown')
                self.assertEqual(first['history']['current']['providers'], [])
                self.assertFalse(any(first['history']['chart']))
                current = self.assert_chart_frame(first, chart_type, awaiting=True)
                self.assertIn('unknown', current['text'].lower())
                self.assertEqual(current['token'], 'muted')
                self.assertFalse(any('0 tokens' in row['text'] for row in first['rows']))

        zero = self.entry(input=0, output=0, cacheRead=0, cacheWrite=0, total=0)
        self.capture('record', [zero], now=102)
        for chart_type in ('bars', 'dots', 'trace'):
            with self.subTest(chart_type=chart_type, state='zero'):
                update_preferences(None, {'chart_type': chart_type}, host='claude')
                result = self.snapshot()
                self.assertEqual(result['capture']['state'], 'available')
                self.assertEqual(result['history']['current']['providers'][0]['total'], 0)
                self.assertEqual(result['history']['current']['models'][0]['thinking_level'], 'high')
                self.assertFalse(any(result['history']['chart']))
                current = self.assert_chart_frame(result, chart_type)
                self.assertIn('tokens', current['text'].lower())
                self.assertEqual(current['text'].split()[-1], '0')
                self.assertEqual(current['token'], 'text')

        self.capture('record', [self.entry('positive')], now=103)
        for chart_type in ('bars', 'dots', 'trace'):
            with self.subTest(chart_type=chart_type, state='positive'):
                update_preferences(None, {'chart_type': chart_type}, host='claude')
                result = self.snapshot()
                self.assertEqual(result['history']['current']['providers'][0]['total'], 25)
                self.assertEqual(sum(result['history']['chart']), 25)
                current = self.assert_chart_frame(result, chart_type, plotted=True)
                self.assertEqual(current['text'].split()[-1], '25')

        handle_request({'version': 1, 'op': 'preferences', 'words': ['view', 'details']})
        self.assertTrue(any('High tokens' in row['text'] for row in self.snapshot()['rows']))

    def test_partial_counters_do_not_enter_ledger_and_gap_is_sticky(self):
        self.capture()
        incomplete = self.entry()
        del incomplete['cacheRead']
        with self.assertRaises(RequestError):
            self.capture('record', [incomplete])
        self.assertEqual(self.snapshot()['history']['total_history'], [])
        self.capture('record', incomplete=True)
        for chart_type in ('bars', 'dots', 'trace'):
            with self.subTest(chart_type=chart_type, observed=False):
                update_preferences(None, {'chart_type': chart_type}, host='claude')
                result = self.snapshot()
                self.assertEqual(result['capture']['state'], 'incomplete')
                current = self.assert_chart_frame(result, chart_type, awaiting=True)
                self.assertIn('unknown', current['text'].lower())
                self.assertTrue(any('capture incomplete' in row['text'].lower()
                                    and row['token'] == 'warn' for row in result['rows']))
        self.capture('record', [self.entry()], now=103)
        for chart_type in ('bars', 'dots', 'trace'):
            with self.subTest(chart_type=chart_type, observed=True):
                update_preferences(None, {'chart_type': chart_type}, host='claude')
                result = self.snapshot()
                self.assertEqual(result['capture']['state'], 'incomplete')
                self.assertEqual(result['history']['current']['providers'][0]['total'], 25)
                self.assertEqual(sum(result['history']['chart']), 25)
                current = self.assert_chart_frame(result, chart_type, plotted=True)
                self.assertEqual(current['text'].split()[-1], '25')
                self.assertTrue(any('capture incomplete' in row['text'].lower()
                                    and row['token'] == 'warn' for row in result['rows']))

    def test_replay_and_late_stop_never_rebind_current_or_change_first_report(self):
        self.capture(entries=[self.entry()])
        self.capture('record', [self.entry(input=100, total=113)], now=102)
        self.capture(session='session-two', activation='activation-two',
                     entries=[self.entry('new')], now=103)
        self.capture('stop', [self.entry(input=900, total=913)], now=104)
        self.capture('start', [self.entry()], now=105)
        active = active_session(owner='native-one', cwd=self.cwd, host='claude', socket='')
        self.assertEqual((active['session'], active['activation']), ('session-two', 'activation-two'))
        report = self.snapshot(session='session-two', activation='activation-two')['history']
        self.assertEqual(report['current']['providers'][0]['total'], 25)
        self.assertEqual(report['previous']['providers'][0]['total'], 25)
        self.assertEqual(sum(row['total'] for row in report['total_history']), 50)
        self.capture('stop', session='session-two', activation='activation-two', now=106)
        self.capture('start', session='session-two', activation='activation-two', now=107)
        self.assertIsNone(self.snapshot(session='session-two', activation='activation-two')['history']['current'])

    def test_cross_session_replay_does_not_claim_new_usage_or_steal_history(self):
        self.capture(entries=[self.entry()])
        self.capture(session='branch', activation='branch-activation', entries=[self.entry()], now=102)
        result = self.snapshot(session='branch', activation='branch-activation')
        self.assertEqual(result['capture']['state'], 'unknown')
        self.assertEqual(result['history']['current']['providers'], [])
        self.assertEqual(result['history']['previous']['providers'][0]['total'], 25)
        self.assertEqual(sum(row['total'] for row in result['history']['history']), 25)
        self.assertFalse(any(result['history']['chart']))
        for chart_type in ('bars', 'dots', 'trace'):
            with self.subTest(chart_type=chart_type):
                update_preferences(None, {'chart_type': chart_type}, host='claude')
                current = self.assert_chart_frame(
                    self.snapshot(session='branch', activation='branch-activation'),
                    chart_type, awaiting=True)
                self.assertIn('unknown', current['text'].lower())

    def test_invalid_batch_never_commits_its_valid_prefix(self):
        self.capture()
        for value in (True, -1, 1.5, 2**53):
            with self.subTest(value=value), self.assertRaises(RequestError):
                self.capture('record', [self.entry('valid-prefix'), self.entry('invalid', input=value)])
        self.assertEqual(self.snapshot()['history']['total_history'], [])

    def test_historical_limits_and_preferences_are_read_without_rewrite(self):
        path = preferences_path(host='claude')
        path.parent.mkdir(parents=True)
        saved = b'{"side":"left","interval":180,"theme":"claude","chart_type":"dots"}\n'
        path.write_bytes(saved)
        ingest({'owner': 'native-one', 'socket': '', 'session': 'historical',
                'activation': 'old-activation', 'action': 'start', 'entries': [self.entry('historic')]},
               cwd=self.cwd, now=90, host='claude')
        with database(cwd=self.cwd, host='claude') as db:
            db.execute('CREATE TABLE claude_limits (observed REAL, five_pct REAL)')
            db.execute('INSERT INTO claude_limits VALUES (100, 25)')
        result = self.snapshot(session='historical', activation='old-activation')
        self.assertEqual(result['history']['current']['providers'][0]['total'], 25)
        self.assertEqual(result['reports'], [])
        self.assertEqual(result['capture']['state'], 'unknown')
        self.assertEqual(path.read_bytes(), saved)
        with database(cwd=self.cwd, host='claude') as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM claude_limits').fetchone()), (100, 25))

    def test_private_extras_never_persist_or_escape_helper_stdout(self):
        canary = 'PRIVATE_PROMPT_EMAIL_API_KEY_CANARY'
        event = self.entry(prompt=canary, messages=[canary], email=canary, apiKey=canary,
                           transcript_path=canary)
        result = self.invoke({**self.context, 'op': 'capture', 'action': 'start',
                              'entries': [event], 'incomplete': False, 'private': canary})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(canary, result.stdout + result.stderr)
        with database(cwd=self.cwd, host='claude') as db:
            self.assertNotIn(canary, '\n'.join(db.iterdump()))
        result = self.invoke({**self.context, 'op': 'snapshot', 'width': 48, 'allowance': None})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(canary, result.stdout + result.stderr)
        bad = self.entry(model='secret\n' + canary)
        result = self.invoke({**self.context, 'op': 'capture', 'action': 'record',
                              'entries': [bad], 'incomplete': False})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertNotIn(canary, result.stdout + result.stderr)

    def test_allowance_requires_live_matching_fresh_observation_and_never_persists(self):
        self.capture()
        allowance = {'session': 'session-one', 'activation': 'activation-one', 'observedAt': 101,
                     'windows': [{'id': 'five-hour', 'label': '5 hour', 'usedFraction': 0, 'resetsAt': 200}]}
        valid = self.snapshot(allowance=allowance)
        self.assertEqual(valid['reports'][0]['limits'][0]['amount']['usedFraction'], 0)
        self.assertTrue(any('100% left' in row['text'] for row in valid['rows']))
        for changes, now in (({'session': 'other'}, 110), ({'activation': 'other'}, 110),
                             ({'observedAt': 99}, 110), ({'observedAt': 111}, 110), ({}, 402)):
            with self.subTest(changes=changes, now=now):
                result = self.snapshot(allowance={**allowance, **changes}, now=now)
                self.assertEqual(result['reports'], [])
                self.assertFalse(any('100% left' in row['text'] for row in result['rows']))
        expired = deepcopy(allowance)
        expired['windows'][0]['resetsAt'] = 109
        self.assertEqual(self.snapshot(allowance=expired)['reports'], [])
        self.assertEqual(self.snapshot()['reports'], [])
        with database(cwd=self.cwd, host='claude') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM quota').fetchone()[0], 0)
            self.assertNotIn('five-hour', '\n'.join(db.iterdump()))
        self.capture('stop')
        self.assertEqual(self.snapshot(allowance=allowance)['reports'], [])

    @staticmethod
    def is_claude_heading(row):
        emphasis = row['emphasis']
        return (row['text'].startswith('CLAUDE ') and emphasis
                and emphasis['start'] == 0 and emphasis['token'] == 'accent')

    def claude_section(self, result):
        rows = result['rows']
        headings = [index for index, row in enumerate(rows) if self.is_claude_heading(row)]
        self.assertEqual(len(headings), 1)
        start = headings[0] + 1
        # The next heading ends the section; inner separators only space its groups.
        end = next((index for index in range(start, len(rows))
                    if rows[index]['emphasis'] and rows[index]['emphasis']['start'] == 0
                    and rows[index]['emphasis']['token'] == 'accent'), len(rows))
        while end > start and not rows[end - 1]['text']:
            end -= 1
        return rows[start:end]

    def test_allowance_and_capture_share_one_section_but_keep_independent_states(self):
        self.capture()
        allowance = {'session': 'session-one', 'activation': 'activation-one', 'observedAt': 101,
                     'windows': [{'id': 'five-hour', 'label': '5 hour', 'usedFraction': 0, 'resetsAt': 200}]}
        unknown = self.snapshot(allowance=allowance, width=160)
        section = self.claude_section(unknown)
        self.assertEqual(unknown['capture']['state'], 'unknown')
        self.assertEqual(unknown['reports'][0]['limits'][0]['amount']['usedFraction'], 0)
        self.assertTrue(any('100% left' in row['text'] for row in section))
        self.assertTrue(any(row['text'] == unknown['capture']['reason'] for row in section))
        self.assertTrue(any(row['token'] == 'warn' and 'capture unknown' in row['text'].lower()
                            for row in section))

        zero = self.entry(input=0, output=0, cacheRead=0, cacheWrite=0, total=0)
        self.capture('record', [zero], now=102)
        available = self.snapshot(width=160)
        section = self.claude_section(available)
        self.assertEqual(available['capture']['state'], 'available')
        self.assertEqual(available['reports'], [])
        self.assertEqual(available['history']['current']['providers'][0]['total'], 0)
        self.assertTrue(any(row['token'] == 'warn' for row in section))
        self.assertFalse(any('Token capture' in row['text'] for row in available['rows']))

        self.capture('record', [self.entry('positive', at=103)], now=103, incomplete=True)
        incomplete = self.snapshot(allowance=allowance, width=160)
        section = self.claude_section(incomplete)
        self.assertEqual(incomplete['capture']['state'], 'incomplete')
        self.assertEqual(incomplete['history']['current']['providers'][0]['total'], 25)
        self.assertEqual(sum(incomplete['history']['chart']), 25)
        self.assertTrue(any('100% left' in row['text'] for row in section))
        self.assertTrue(any(row['text'] == incomplete['capture']['reason'] for row in section))
        self.assertTrue(any(row['token'] == 'warn' and 'capture incomplete' in row['text'].lower()
                            for row in section))

    def test_unknown_capture_stays_quiet_once_project_usage_is_registered(self):
        self.capture(entries=[self.entry()])
        self.capture(now=120, session='session-two', activation='activation-two')
        result = self.snapshot(session='session-two', activation='activation-two', now=130)
        self.assertEqual(result['capture']['state'], 'unknown')
        self.assertFalse(any('Token capture' in row['text'] for row in result['rows']))
        self.assertFalse(any(row['text'] == result['capture']['reason'] for row in result['rows']))

    def test_hidden_or_removed_allowance_keeps_warning_only_section_independent_of_commands(self):
        self.capture()
        allowance = {'session': 'session-one', 'activation': 'activation-one', 'observedAt': 101,
                     'windows': [{'id': 'five-hour', 'label': 'Hidden allowance label',
                                  'usedFraction': 0, 'resetsAt': 200}]}
        provider_states = ({'providers': ['anthropic'], 'hidden': ['anthropic']},
                           {'providers': [], 'hidden': []})
        for incomplete in (False, True):
            if incomplete:
                self.capture('record', incomplete=True, now=102)
            for provider_state in provider_states:
                for commands_visible in (False, True):
                    with self.subTest(incomplete=incomplete, providers=provider_state,
                                      commands_visible=commands_visible):
                        update_preferences(None, {**provider_state, 'commands_visible': commands_visible},
                                           host='claude')
                        result = self.snapshot(allowance=allowance, width=160)
                        section = self.claude_section(result)
                        self.assertEqual(result['capture']['state'], 'incomplete' if incomplete else 'unknown')
                        self.assertTrue(result['reports'])
                        self.assertTrue(any(row['token'] == 'warn' and 'Token capture' in row['text']
                                            for row in section))
                        self.assertTrue(any(row['text'] == result['capture']['reason'] for row in section))
                        self.assertFalse(any('Hidden allowance label' in row['text']
                                             or '100% left' in row['text'] for row in result['rows']))
                        self.assertFalse(any(row['text'].startswith('COMMANDS ') for row in section))

        self.capture(entries=[self.entry('new', at=120)], now=120,
                     session='session-two', activation='activation-two')
        for provider_state in provider_states:
            update_preferences(None, provider_state, host='claude')
            result = self.snapshot(session='session-two', activation='activation-two', now=130)
            self.assertEqual(result['capture']['state'], 'available')
            self.assertFalse(any(self.is_claude_heading(row) for row in result['rows']))
            self.assertFalse(any('Token capture' in row['text'] for row in result['rows']))

    def test_refresh_never_mixes_history_with_another_activation_allowance(self):
        self.capture(entries=[self.entry()])
        with database(cwd=self.cwd, host='claude') as db:
            db.execute('PRAGMA journal_mode=WAL')
        allowance = {'session': 'session-one', 'activation': 'activation-one', 'observedAt': 101,
                     'windows': [{'id': 'five-hour', 'label': '5 hour', 'usedFraction': 0, 'resetsAt': 200}]}

        def switch_after_history(*args, **kwargs):
            history = summary(*args, **kwargs)
            self.capture(session='new', activation='new-activation', now=103,
                         entries=[self.entry('new', input=2, output=0, cacheRead=0, cacheWrite=0, total=2)])
            return history

        with patch('claude_native.summary', side_effect=switch_after_history):
            result = self.snapshot(allowance=allowance)
        self.assertEqual(result['history']['current']['id'], 'session-one')
        self.assertEqual(result['history']['current']['providers'][0]['total'], 25)
        self.assertEqual(result['reports'][0]['limits'][0]['amount']['usedFraction'], 0)
        refreshed = self.snapshot(session='new', activation='new-activation', allowance=allowance)
        self.assertEqual(refreshed['history']['current']['id'], 'new')
        self.assertEqual(refreshed['reports'], [])

    def test_old_record_blocks_only_capture_without_changing_saved_history(self):
        self.capture(entries=[self.entry()])
        record = agent_dir(host='claude') / 'installation.json'
        record.write_text('{"private":"unchanged-old-record"}')
        before = record.read_bytes()
        with self.assertRaises(RequestError) as error:
            self.capture('record', [self.entry('blocked')])
        self.assertEqual(error.exception.code, 'migration_required')
        result = self.snapshot()
        self.assertEqual(result['capture']['state'], 'unknown')
        self.assertEqual(result['history']['current']['providers'][0]['total'], 25)
        self.assertEqual(record.read_bytes(), before)

    def test_unsupported_controls_preserve_saved_layout_and_interval(self):
        update_preferences(None, {'side': 'left', 'interval': 180}, host='claude')
        for words in (['position', 'right'], ['window', 'interval', '15']):
            with self.assertRaises(RequestError):
                handle_request({'version': 1, 'op': 'preferences', 'words': words})
        result = handle_request({'version': 1, 'op': 'preferences', 'words': ['chart', 'trace']})
        self.assertEqual((result['preferences']['side'], result['preferences']['interval']), ('left', 180))
        self.assertNotIn('180s', result['text'])
        self.assertNotIn('/ left', result['text'])
        for enabled in (False, True):
            handle_request({'version': 1, 'op': 'preferences', 'words': ['window', 'on' if enabled else 'off']})
            self.assertEqual(load_preferences(host='claude')['enabled'], enabled)

    def test_invalid_preferences_are_visible_errors_not_empty_success(self):
        path = preferences_path(host='claude')
        path.parent.mkdir(parents=True)
        path.write_text('{private-corrupt-canary')
        for request in ({**self.context, 'op': 'snapshot', 'width': 32, 'allowance': None},
                        {'version': 1, 'op': 'preferences', 'words': ['chart', 'trace']}):
            result = self.invoke(request)
            self.assertNotEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertFalse(response['ok'])
            self.assertEqual(response['error']['code'], 'storage_unavailable')
            self.assertNotIn('private-corrupt-canary', result.stdout + result.stderr)

    def test_symlinked_storage_is_rejected_without_touching_target(self):
        target = self.home / 'target'
        target.mkdir()
        (self.home / 'claude').symlink_to(target, target_is_directory=True)
        result = self.invoke({**self.context, 'op': 'capture', 'action': 'start',
                              'entries': [], 'incomplete': False})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_native_rows_are_terminal_safe_and_styled_at_narrow_widths(self):
        self.capture()
        handle_request({'version': 1, 'op': 'preferences',
                        'words': ['theme', 'custom', 'accent', '#123456']})
        for observed in (False, True):
            if observed:
                self.capture('record', [self.entry()], now=102)
            for chart_type in ('bars', 'dots', 'trace'):
                update_preferences(None, {'chart_type': chart_type}, host='claude')
                for width in (1, 10, 20, 48):
                    with self.subTest(observed=observed, chart_type=chart_type, width=width):
                        result = self.snapshot(width=width)
                        self.assertEqual(result['tokens']['accent'], '#123456')
                        if width >= 20:
                            self.assert_chart_frame(result, chart_type,
                                                    awaiting=not observed, plotted=observed)
                        for row in result['rows']:
                            self.assertLessEqual(len(row['text']), width)
                            self.assertFalse(any(ord(char) < 32 or ord(char) == 127
                                                 for char in row['text']))
                            self.assertIn(row['token'], result['tokens'])
                            if row['emphasis']:
                                self.assertLess(row['emphasis']['start'], row['emphasis']['end'])
                                self.assertLessEqual(row['emphasis']['end'], len(row['text']))
                                self.assertIn(row['emphasis']['token'], result['tokens'])


if __name__ == '__main__':
    unittest.main()
