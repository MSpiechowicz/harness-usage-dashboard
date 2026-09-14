"""User-visible allowance semantics; no live provider calls."""
from copy import deepcopy
import unittest

from dashboard import (allowance_color, change_config, provider_lines, resolve_tokens,
                       session_lines, token_chart)
from preferences import DEFAULTS


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
        self.assertEqual(next(style for text, style in lines if text.startswith('Reset credits:')), 'dim')

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

    def test_named_themes_and_custom_tokens_preserve_status_semantics(self):
        config = deepcopy(DEFAULTS)
        change_config(config, ['theme', 'blue'])
        tokens = resolve_tokens(config)
        self.assertEqual(config['theme'], 'blue')
        self.assertEqual(tokens['text'], 'blue')
        self.assertEqual(tokens['muted'], 'blue')
        self.assertEqual(tokens['accent'], 'blue')
        self.assertEqual(tokens['chart'], 'blue')
        self.assertEqual(tokens['good'], 'green')
        self.assertEqual(tokens['warn'], 'orange')
        self.assertEqual(tokens['error'], 'red')

        change_config(config, ['theme', 'color', 'text', '#58A66A'])
        self.assertEqual(config['tokens'], {'text': '#58a66a'})
        self.assertEqual(resolve_tokens(config)['text'], '#58a66a')

        change_config(config, ['theme', 'reset'])
        self.assertEqual(config['theme'], 'green')
        self.assertEqual(config['tokens'], {})
if __name__ == '__main__':
    unittest.main()
