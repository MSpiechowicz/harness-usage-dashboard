"""User-visible allowance semantics; no live provider calls."""
from argparse import Namespace
from copy import deepcopy
import unittest
from unittest.mock import MagicMock, patch
from dashboard import (allowance_color, change_config, owned_panes, provider_lines, resolve_tokens,
                       section_heading, session_lines, token_chart)
from preferences import DEFAULTS, THEME_NAMES


class RemainingAllowanceTests(unittest.TestCase):
    def render(self, amount):
        data = {'reports': [{'provider': 'openai-codex', 'fetchedAt': 0, 'limits': [
            {'id': 'weekly', 'label': 'Weekly', 'amount': amount},
        ]}]}
        config = {'interval': 60, 'compact': True, 'windows': {}}
        return '\n'.join(text for text, _ in provider_lines(data, 'openai-codex', config, 0, 32))

    def test_used_allowance_is_displayed_as_remaining(self):
        rendered = self.render({'usedFraction': .55})
        self.assertIn('45% left', rendered)
        self.assertNotIn('55% used', rendered)
        self.assertIn('100% left', self.render({'usedFraction': 0}))
        self.assertIn('0% left', self.render({'usedFraction': 1}))
        self.assertNotIn('#', rendered)

    def test_reset_credit_metadata_is_secondary(self):
        data = {'reports': [{'provider': 'openai-codex', 'fetchedAt': 0, 'limits': [
            {'id': 'weekly', 'label': 'Weekly', 'amount': {'usedFraction': .1}},
        ], 'resetCredits': {'availableCount': 2}}]}
        lines = provider_lines(data, 'openai-codex', {'interval': 60, 'compact': True, 'windows': {}}, 0, 32)
        self.assertEqual(next(style for text, style in lines if text.startswith('Reset credits:')), 'secondary')

    def test_reported_remaining_takes_precedence_over_used_estimate(self):
        self.assertIn('20% left', self.render({'remainingFraction': .2, 'usedFraction': .55}))
        self.assertIn('25% left', self.render({'remaining': 25, 'limit': 100}))

    def test_missing_limit_does_not_become_zero_allowance(self):
        rendered = self.render({'remaining': 12.5, 'unit': 'usd'})
        self.assertIn('12.5 usd left', rendered)
        self.assertNotIn('%', rendered)
        self.assertNotIn('%', self.render({}))

    def test_remaining_percentages_distinguish_low_allowance(self):
        self.assertEqual(allowance_color(.15), 'error')
        self.assertEqual(allowance_color(.20), 'error')
        self.assertEqual(allowance_color(.21), 'warn')
        self.assertEqual(allowance_color(.40), 'warn')
        self.assertEqual(allowance_color(.41), 'good')
        self.assertEqual(allowance_color(.90, 'exhausted'), 'error')
    def test_token_chart_uses_chart_design_token(self):
        self.assertTrue(any(style[-1] == 'chart' for _text, style in token_chart([1, 2, 3], 32)
                            if isinstance(style, tuple)))

    def test_empty_token_chart_keeps_axes_and_full_width(self):
        rows = token_chart([0] * 20, 32)
        texts = [text for text, _style in rows]
        self.assertIn('No activity in the last 20m', texts)
        self.assertEqual(texts[1], '')
        self.assertTrue(any('│' in text for text in texts))
        self.assertTrue(any('└' in text and '─' in text for text in texts))
        self.assertTrue(all(len(text) == 32 for text in texts[2:]))
    def test_active_token_chart_leaves_idle_message_slot_empty(self):
        rows = token_chart([0, 1, 2], 32)
        texts = [text for text, _style in rows]
        self.assertEqual(texts[:2], ['', ''])
        self.assertIn('│', texts[2])


    def test_section_divider_uses_secondary_base_color(self):
        _text, style = section_heading('TOKEN RATE', 32, 'tok/min')
        self.assertEqual(style[0], 'secondary')
    @patch('dashboard.curses.mouseinterval')
    @patch('dashboard.curses.mousemask')
    @patch('dashboard.curses.curs_set')
    def test_theme_change_forces_full_repaint(self, _curs_set, _mousemask, _mouseinterval):
        screen = MagicMock()
        screen.getmaxyx.return_value = (30, 80)
        screen.getch.side_effect = SystemExit
        initial = dict(DEFAULTS, profile='default', refresh=0)
        updated = dict(initial, theme='blue')
        colors = {name: 0 for name in ('normal', 'dim', 'secondary', 'title',
                                       'chart', 'good', 'warn', 'error')}
        args = Namespace(owner='%1', profile='default', providers=None, side=None, interval=None)
        with patch('dashboard.defaults', return_value=initial), \
             patch('dashboard.load_config', return_value=updated), \
             patch('dashboard.mux', return_value='0'), \
             patch('dashboard.initialize_colors', return_value=colors), \
             patch('dashboard.session_summary',
                   return_value={'current': None, 'previous': None, 'chart': [],
                                 'history': [], 'total_history': []}):
            with self.assertRaises(SystemExit):
                from dashboard import watch
                watch(screen, args)
        screen.clear.assert_called_once_with()

    @patch('dashboard.curses.mouseinterval')
    @patch('dashboard.curses.mousemask')
    @patch('dashboard.curses.curs_set')
    def test_footer_reserves_margin_after_history(self, _curs_set, _mousemask, _mouseinterval):
        screen = MagicMock()
        screen.getmaxyx.return_value = (8, 80)
        screen.getch.side_effect = SystemExit
        initial = dict(DEFAULTS, providers=['openai-codex'], profile='default', refresh=0)
        model = {'provider': 'openai-codex', 'model': 'model-a', 'thinking_level': 'unknown',
                 'total': 10, 'input': 10, 'output': 0, 'cache_read': 0, 'cache_write': 0}
        history = {'current': None, 'previous': None, 'chart': [],
                   'history': [model], 'total_history': [model]}
        colors = {name: 0 for name in ('normal', 'dim', 'secondary', 'title',
                                       'chart', 'good', 'warn', 'error')}
        args = Namespace(owner=None, profile='default', providers=None, side=None, interval=None)
        with patch('dashboard.defaults', return_value=initial), \
             patch('dashboard.initialize_colors', return_value=colors), \
             patch('dashboard.session_summary', return_value=history), \
             patch('dashboard.FetchJob'), \
             patch('dashboard.session_lines', return_value=[('HISTORY entry', 'normal')]):
            from dashboard import watch
            with self.assertRaises(SystemExit):
                watch(screen, args)
        writes = {call.args[0]: call.args[2] for call in screen.addnstr.call_args_list}
        footer_row = next(row for row, text in writes.items()
                          if text.startswith('┌─ COMMANDS'))
        self.assertEqual(writes[footer_row - 1], '')
        self.assertIn('1 provider | every 60s', writes[footer_row + 1])
        self.assertIn('[Refresh] [Hide]', writes[footer_row + 2])
        self.assertIn('r refresh | q hide | scroll', writes[footer_row + 3])
        self.assertTrue(writes[footer_row + 4].startswith('└'))


class PaneOwnershipTests(unittest.TestCase):
    @patch('dashboard.mux')
    def test_owned_panes_returns_only_panes_tagged_to_owner(self, mux):
        mux.return_value = '%1\t%owner\n%2\t%other\n%3\t%owner\n%4\t\n'

        self.assertEqual(owned_panes('%owner'), ['%1', '%3'])
        mux.assert_called_once_with('list-panes', '-a', '-F',
                                     '#{pane_id}\t#{@omp_usage_owner}')


class NestedCommandTests(unittest.TestCase):
    def test_provider_visibility_and_window_filters_are_independent(self):
        config = deepcopy(DEFAULTS)
        change_config(config, ['providers', 'add', 'codex'])
        change_config(config, ['window', 'hide', 'codex', 'spark'])
        change_config(config, ['providers', 'hide', 'codex'])
        self.assertEqual(config['windows']['openai-codex'], ['spark'])
        self.assertIn('openai-codex', config['hidden'])
        change_config(config, ['window', 'show', 'codex', 'spark'])
        self.assertEqual(config['windows']['openai-codex'], [])
        self.assertIn('openai-codex', config['hidden'])
        change_config(config, ['providers', 'show', 'codex'])
        self.assertNotIn('openai-codex', config['hidden'])

    def test_wrong_section_cannot_enable_the_dashboard(self):
        config = deepcopy(DEFAULTS)
        config['enabled'] = False
        with self.assertRaises(ValueError):
            change_config(config, ['view', 'on'])
        self.assertFalse(config['enabled'])
        change_config(config, ['window', 'on'])
        self.assertTrue(config['enabled'])


    def test_view_details_toggles_persistent_model_breakdowns(self):
        config = deepcopy(DEFAULTS)
        change_config(config, ['view', 'details'])
        self.assertFalse(config['compact'])
        change_config(config, ['view', 'compact'])
        self.assertTrue(config['compact'])
    def test_details_separate_model_blocks_for_readability(self):
        model_fields = {'input': 10, 'output': 2, 'cache_read': 3, 'cache_write': 0}
        history = {
            'chart': [], 'history': [], 'total_history': [],
            'current': {
                'id': 'session-1', 'updated': 0,
                'providers': [{'provider': 'openai-codex', 'total': 15}],
                'models': [
                    {'provider': 'openai-codex', 'model': 'model-a',
                     'thinking_level': 'high', 'total': 15, **model_fields},
                    {'provider': 'openai-codex', 'model': 'model-b',
                     'thinking_level': 'low', 'total': 15, **model_fields},
                ],
                'model_summaries': [], 'quota': [],
            },
            'previous': None,
        }
        texts = [text for text, _style in session_lines(history, 0, 60, compact=False)]
        first = next(index for index, text in enumerate(texts) if text.startswith('model-a'))
        second = next(index for index, text in enumerate(texts) if text.startswith('model-b'))
        self.assertEqual(texts[first - 1], '')
        self.assertEqual(texts[second - 1], '')

    def test_details_separate_history_entries_from_totals(self):
        model = {'provider': 'openai-codex', 'model': 'model-a',
                 'thinking_level': 'high', 'total': 15,
                 'input': 10, 'output': 2, 'cache_read': 3, 'cache_write': 0}
        history = {
            'chart': [], 'history': [model], 'total_history': [model],
            'current': None, 'previous': None,
        }
        texts = [text for text, _style in session_lines(history, 0, 60, compact=False)]
        other = next(index for index, text in enumerate(texts)
                     if text.startswith('Other sessions'))
        other_model = next(index for index, text in enumerate(texts[other + 1:], other + 1)
                           if text.startswith('CODEX / model-a'))
        total = next(index for index, text in enumerate(texts)
                     if text.startswith('Project total'))
        total_model = next(index for index, text in enumerate(texts[total + 1:], total + 1)
                           if text.startswith('CODEX / model-a'))
        self.assertEqual(texts[other_model - 1], '')
        self.assertEqual(texts[total_model - 1], '')

    def test_compact_session_lines_separate_sections(self):
        session = {
            'id': 'session-1', 'updated': 0,
            'providers': [{'provider': 'openai-codex', 'total': 12}],
            'models': [], 'quota': [],
        }
        history = {
            'chart': [], 'history': [{'provider': 'openai-codex', 'total': 8}],
            'total_history': [{'provider': 'openai-codex', 'total': 20}],
            'current': session, 'previous': session,
        }
        texts = [text for text, _style in session_lines(history, 0, 60, compact=True)]
        headings = [
            next(index for index, text in enumerate(texts) if text.startswith(prefix))
            for prefix in ('TOKEN RATE', 'CURRENT SESSION', 'PREVIOUS SESSION', 'HISTORY')
        ]
        self.assertEqual([texts[index - 1] for index in headings[1:]], ['', '', ''])
        project_total = next(index for index, text in enumerate(texts)
                             if text.startswith('Project total'))
        self.assertTrue(texts[project_total - 1].startswith('Other sessions'))
        self.assertEqual(texts[-1], '')

    def test_named_themes_and_custom_tokens_preserve_status_semantics(self):
        for theme in THEME_NAMES:
            config = deepcopy(DEFAULTS)
            change_config(config, ['theme', theme])
            tokens = resolve_tokens(config)
            self.assertEqual(tokens['accent'], theme)
            self.assertEqual(tokens['chart'], theme)
            self.assertEqual(tokens['good'], 'green')
            self.assertEqual(tokens['warn'], 'orange')
            self.assertEqual(tokens['error'], 'red')

        config = deepcopy(DEFAULTS)
        change_config(config, ['theme', 'blue'])
        self.assertEqual(resolve_tokens(config)['secondary'], '#24527a')
        change_config(config, ['theme', 'custom', 'text', '#58A66A'])
        self.assertEqual(config['tokens'], {'text': '#58a66a'})
        self.assertEqual(resolve_tokens(config)['text'], '#58a66a')

        change_config(config, ['theme', 'reset'])
        self.assertEqual(config['theme'], 'green')
        self.assertEqual(config['tokens'], {})

if __name__ == '__main__':
    unittest.main()
