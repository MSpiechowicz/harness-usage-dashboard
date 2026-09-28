"""User-visible allowance semantics; no live provider calls."""
from argparse import Namespace
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from copy import deepcopy
import curses
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import urllib.request
from unittest.mock import MagicMock, patch
import host_adapters
from host_adapters import HostAdapter
from session_usage import ingest, record_quota, summary

from dashboard import (ROOT, FetchJob, allowance_color, change_config, clean, command_box_rows,
                       control, defaults, launch, load_config, owned_panes, provider_lines,
                       resolve_tokens, section_heading, session_lines, token_chart, tmux_binary, watch)
from claude_bridge import start_receiver
from claude_usage_source import fetch_usage as fetch_claude_usage
from preferences import (CHART_TYPES, DEFAULTS, THEME_NAMES, load_preferences,
                         preferences_path, update_preferences)


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

    def test_details_keep_credits_adjacent_to_final_window_id(self):
        data = {'reports': [{'provider': 'openai-codex', 'fetchedAt': 0, 'limits': [
            {'id': 'hourly', 'label': 'Hourly', 'amount': {'usedFraction': .2}},
            {'id': 'weekly', 'label': 'Weekly', 'amount': {'usedFraction': .45}},
        ], 'resetCredits': {'availableCount': 1}}]}
        config = {'interval': 60, 'compact': False, 'windows': {}}
        rows = [text for text, _ in provider_lines(data, 'openai-codex', config, 0, 32)]
        self.assertTrue(any('80% left' in text for text in rows))
        self.assertTrue(any('55% left' in text for text in rows))
        self.assertIn('ID: hourly', rows)
        self.assertEqual(rows[rows.index('ID: weekly') + 1], 'Reset credits: 1')
        self.assertFalse(any('% used' in text for text in rows))
        self.assertFalse(any('Source age:' in text for text in rows))

    def test_cached_source_warning_survives_in_both_views(self):
        data = {'reports': [{'provider': 'openai-codex', 'fetchedAt': 0, 'limits': []}]}
        for compact in (True, False):
            with self.subTest(compact=compact):
                config = {'interval': 60, 'compact': compact, 'windows': {}}
                rows = [text for text, _ in provider_lines(data, 'openai-codex', config, 66, 32)]
                self.assertIn('Source age: 66s (cached)', rows)

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
        self.assertEqual(texts[0], '')
        self.assertTrue(all('│' in text for text in texts[1:7]))
        self.assertEqual(texts[7], '   0 └' + '─' * 26)
        self.assertTrue(all(len(text) == 32 for text in texts[1:]))

    def test_active_token_chart_keeps_one_margin_row_before_chart(self):
        rows = token_chart([0, 1, 2], 32)
        texts = [text for text, _style in rows]
        self.assertEqual(texts[0], '')
        self.assertIn('│', texts[1])

    def test_chart_axes_share_one_baseline_and_time_origin(self):
        active = [0] * 10 + [1000] + [0] * 9
        for encoding, vertical, corner, horizontal in (
            ('utf-8', '│', '└', '─'),
            ('ascii', '|', '+', '-'),
        ):
            with patch.object(sys, 'stdout', io.TextIOWrapper(io.BytesIO(), encoding=encoding)):
                for width in (20, 30, 32):
                    for values in (active, [0] * 20):
                        for mode in CHART_TYPES:
                            with self.subTest(encoding=encoding, width=width,
                                              active=any(values), mode=mode):
                                rows = token_chart(values, width, mode)
                                self.assertEqual(len(rows), 9)
                                self.assertEqual(rows[0], ('', 'dim'))
                                first = 1
                                count = 6
                                plot = rows[first:first + count]
                                baseline = rows[first + count][0]
                                labels = rows[first + count + 1][0]
                                self.assertTrue(all(text[5] == vertical and len(text) == width
                                                    for text, _ in plot))
                                self.assertEqual(plot[0][0][:4], '  1k' if any(values) else '    ')
                                self.assertTrue(all(text[:4] == '    ' for text, _ in plot[1:]))
                                self.assertEqual(baseline, '   0 ' + corner
                                                 + horizontal * (width - 6))
                                self.assertEqual(labels, ' ' * 6
                                                 + '-20m'.ljust(width - 9) + 'now')
                                self.assertEqual(sum(text[:4].strip() == '0'
                                                     for text, _ in plot), 0)
                                self.assertEqual(len(baseline), width)
                                self.assertEqual(len(labels), width)
                                end = width if any(values) else 6
                                self.assertTrue(all(style == ('secondary', 6, end, 'chart')
                                                    for _text, style in plot))
                                if mode == 'trace':
                                    self.assertNotEqual(plot[-1][0][-1], vertical)
                                    self.assertEqual(baseline[-1], horizontal)


    def test_chart_views_keep_bars_and_dots_and_fit_narrow_panes(self):
        values = [0, 1, 4, 0, 30, 70, 120, 20, 0, 1,
                  1000, 40, 10, 0, 500, 60, 90, 0, 2, 200]
        with patch.object(sys, 'stdout', io.TextIOWrapper(io.BytesIO(), encoding='utf-8')):
            for width in (20, 30, 32):
                with self.subTest(width=width):
                    bars = token_chart(values, width, 'bars')
                    dots = token_chart(values, width, 'dots')
                    trace = token_chart(values, width, 'trace')
                    self.assertEqual(bars, token_chart(values, width))
                    self.assertEqual(len(bars), 9)
                    self.assertEqual(len(dots), 9)
                    self.assertEqual(len(trace), 9)
                    self.assertEqual(bars[1][0][:4], '  1k')
                    self.assertEqual(dots[1][0][:4], '  1k')
                    self.assertTrue(any('█' in text for text, _ in bars))
                    self.assertTrue(all(set(text.split('│', 1)[1]) <= {' ', '●'}
                                        for text, _ in dots[1:7]))
                    self.assertTrue(any('⠁' <= char <= '⣿'
                                        for text, _ in trace[1:7] for char in text))
                    for rows in (bars, dots, trace):
                        self.assertIn('-20m', rows[-1][0])
                        self.assertTrue(all(len(text) == width for text, _ in rows[1:]))
                        for text, style in rows:
                            if isinstance(style, tuple):
                                self.assertLessEqual(style[1], style[2])
                                self.assertLessEqual(style[2], len(text))
                            self.assertEqual(clean(text), text)
                    self.assertEqual(trace[1][0][:4], '  1k')
                    self.assertEqual(trace[4][0][:4], '    ')
                    self.assertEqual(trace[7][0][:4], '   0')
                    self.assertTrue(trace[8][0].strip().endswith('now'))

    @staticmethod
    def trace_pixels(rows):
        dots = ((1, 2, 4, 64), (8, 16, 32, 128))
        return {(2 * x + side, 4 * y + offset)
                for y, (line, _) in enumerate(rows[1:7])
                for x, cell in enumerate(line[6:])
                for side, bits in enumerate(dots)
                for offset, bit in enumerate(bits)
                if (ord(cell) - 0x2800) & bit}

    def test_trace_connects_nonzero_samples_and_touches_idle_edges(self):
        values = [0, 1, 4, 0, 30, 70, 120, 20, 0, 1,
                  1000, 40, 10, 0, 500, 60, 90, 0, 2, 200]
        rows = token_chart(values, 32, 'trace')
        pixels = self.trace_pixels(rows)
        self.assertEqual([row[0][5] for row in rows[1:7]], ['│'] * 6)
        self.assertTrue(all(row[1] == ('secondary', 6, 32, 'chart')
                            for row in rows[1:7]))
        for x, expected_y in ((3, 23), (5, 23), (27, 0),
                              (38, 12), (48, 23), (51, 18)):
            with self.subTest(x=x, y=expected_y):
                self.assertTrue(any((x, y) in pixels
                                    for y in range(max(0, expected_y - 1),
                                                   min(23, expected_y + 1) + 1)))
        self.assertIn((0, 23), pixels)
        # Adjacent positive samples retain a connected, column-spanning rise and fall.
        self.assertTrue(all(any((x, y) in pixels for y in range(24))
                            for x in range(24, 33)))

    def test_trace_zero_runs_do_not_draw_a_second_horizontal_baseline(self):
        values = [0] * 5 + [1000] + [0] * 14
        rows = token_chart(values, 32, 'trace')
        pixels = self.trace_pixels(rows)
        self.assertIn((13, 0), pixels)
        self.assertTrue(all(11 <= x <= 16 for x, _ in pixels))
        self.assertEqual({x for x, y in pixels if y == 23}, {11, 16})
        self.assertEqual(rows[7][0], '   0 └' + '─' * 26)

    def test_trace_isolated_peak_meets_plot_bottom_on_both_edges(self):
        values = [0] * 5 + [1000] + [0] * 14
        with patch.object(sys, 'stdout', io.TextIOWrapper(io.BytesIO(), encoding='utf-8')):
            for width in (20, 30, 32):
                with self.subTest(width=width):
                    pixels = self.trace_pixels(token_chart(values, width, 'trace'))
                    max_x = 2 * (width - 6) - 1
                    before = round(4 * max_x / 19)
                    after = round(6 * max_x / 19)
                    self.assertIn((before, 23), pixels)
                    self.assertIn((after, 23), pixels)
                    self.assertTrue(all(before <= x <= after for x, _ in pixels))

    def test_trace_same_column_zero_and_peak_connect_to_plot_bottom(self):
        rows = token_chart([0, 1000, 0, 0, 0], 7, 'trace')
        pixels = self.trace_pixels(rows)
        self.assertEqual(pixels, {(0, y) for y in range(24)})

    def test_trace_tiny_positive_reaches_bottom_plot_subpixel_without_zero_runs(self):
        values = [0] * 5 + [1000, 1] + [0] * 13
        pixels = self.trace_pixels(token_chart(values, 32, 'trace'))
        self.assertIn((16, 23), pixels)
        self.assertIn((19, 23), pixels)
        self.assertFalse(any((x, 23) in pixels for x in range(20, 52)))
        self.assertIn((11, 23), pixels)

        positive_run = [0] * 5 + [1000, 1, 1] + [0] * 12
        pixels = self.trace_pixels(token_chart(positive_run, 32, 'trace'))
        self.assertTrue(all((x, 23) in pixels for x in range(16, 20)))
        self.assertIn((21, 23), pixels)
        self.assertFalse(any((x, 23) in pixels for x in range(22, 52)))

    def test_ascii_trace_zero_runs_are_clear_at_narrow_widths(self):
        values = [0] * 5 + [1000] + [0] * 14
        with patch.object(sys, 'stdout', io.TextIOWrapper(io.BytesIO(), encoding='ascii')):
            for width in (20, 30, 32):
                with self.subTest(width=width):
                    rows = token_chart(values, width, 'trace')
                    plot = [text[6:] for text, _ in rows[1:7]]
                    self.assertTrue(all(text.isascii() and len(text) == width
                                        for text, _ in rows[1:]))
                    self.assertTrue(all(line[:2].strip() == '' and line[11:].strip() == ''
                                        for line in plot))
                    self.assertTrue(any(char in '/\\|' for line in plot for char in line))
                    if width == 32:
                        self.assertTrue(all(line[4] == line[9] == ' '
                                            for line in plot))
                    self.assertEqual(rows[7][0], '   0 +' + '-' * (width - 6))


    def test_bars_and_dots_show_tiny_positive_and_peak_on_six_rows(self):
        values = [1] + [0] * 18 + [48]
        for encoding, vertical, dot, fraction in (
            ('utf-8', '│', '●', '▁'),
            ('ascii', '|', 'o', '.'),
        ):
            with patch.object(sys, 'stdout', io.TextIOWrapper(io.BytesIO(), encoding=encoding)):
                dots = [text for text, _ in token_chart(values, 32, 'dots')]
                bars = [text for text, _ in token_chart(values, 32, 'bars')]
            with self.subTest(encoding=encoding):
                self.assertEqual(dots[1][6:], ' ' * 25 + dot)
                self.assertEqual(dots[6][6], dot)
                self.assertEqual(bars[1][5], vertical)
                self.assertEqual(bars[6][6], fraction)
                self.assertEqual(bars[1][6:], ' ' * 25 + ('█' if encoding == 'utf-8' else '@'))
                self.assertEqual(bars[7][5], '└' if encoding == 'utf-8' else '+')

    def test_trace_peak_is_linear_and_segment_has_no_missing_columns(self):
        values = [0, 500, 1000, 500] + [0] * 16
        rows = token_chart(values, 32, 'trace')
        pixels = self.trace_pixels(rows)
        self.assertEqual(rows[1][0][:4], '  1k')
        self.assertIn((5, 0), pixels)
        self.assertTrue(any((3, y) in pixels for y in (11, 12)))
        self.assertTrue(any((10, y) in pixels for y in range(12, 23)))
        columns = {x for x, _ in pixels}
        self.assertTrue(set(range(1, 11)) <= columns)
        self.assertEqual(columns, set(range(min(columns), max(columns) + 1)))
        self.assertTrue(columns <= set(range(12)))


    def test_trace_idle_is_empty_but_retains_honest_labels(self):
        with patch.object(sys, 'stdout', io.TextIOWrapper(io.BytesIO(), encoding='utf-8')):
            rows = token_chart([0] * 20, 32, 'trace')
        self.assertEqual(rows[0], ('', 'dim'))
        self.assertTrue(all(set(text[6:]) == {'⠀'} for text, _ in rows[1:7]))
        self.assertEqual(rows[7][0][:4], '   0')
        self.assertEqual(clean('⣀⡇⠤⠀'), '⣀⡇⠤⠀')
        self.assertEqual(clean('░╱▓'), '???')

    def test_chart_ascii_fallback_retains_trace_and_legacy_modes(self):
        with patch.object(sys, 'stdout', io.TextIOWrapper(io.BytesIO(), encoding='ascii')):
            for mode in CHART_TYPES:
                for width in (20, 30, 32):
                    with self.subTest(mode=mode, width=width):
                        rows = token_chart([0, 500, 1000, 500] + [0] * 16,
                                           width, mode)
                        self.assertTrue(all(text.isascii() for text, _ in rows))
                        self.assertTrue(all(len(text) == width for text, _ in rows[1:]))
                        self.assertIn('now', rows[-1][0])
                        if mode == 'trace':
                            plot = [text[6:] for text, _ in rows[1:7]]
                            self.assertTrue(any('/' in row or '\\' in row for row in plot))
                            self.assertTrue(any('|' in row for row in plot))
                            self.assertTrue(all(any(plot[y][x] != ' ' for y in range(6))
                                                for x in range(3)))
                            cells = {(x, y) for y, line in enumerate(plot)
                                     for x, char in enumerate(line) if char != ' '}
                            visited = {next(iter(cells))}
                            frontier = list(visited)
                            while frontier:
                                x, y = frontier.pop()
                                neighbors = {(x + dx, y + dy) for dx in (-1, 0, 1)
                                             for dy in (-1, 0, 1)}
                                fresh = (neighbors & cells) - visited
                                visited.update(fresh)
                                frontier.extend(fresh)
                            self.assertEqual(visited, cells)
                            idle = token_chart([0] * 20, width, mode)
                            self.assertTrue(all(text[6:].strip() == ''
                                                for text, _ in idle[1:7]))
                        else:
                            self.assertTrue(any(marker in ''.join(text for text, _ in rows)
                                                for marker in 'o@'))

    def test_session_chart_option_keeps_existing_positional_arguments(self):
        session = {'id': 'active', 'updated': 0, 'providers': [], 'models': [], 'quota': []}
        history = {'chart': [0] * 10 + [1000] + [0] * 8 + [200],
                   'current': session, 'previous': None, 'history': [], 'total_history': []}
        rows = session_lines(history, 0, 32, True, False, False, False,
                             chart_type='trace')
        text = '\n'.join(line for line, _ in rows)
        self.assertIn('TOKEN TRACE', text)
        self.assertIn('⡇', text)
        narrow = session_lines(history, 0, 20, chart_type='trace')
        self.assertEqual(narrow[0][0], 'TOKEN TRACE tok/min')

    def test_chart_sections_keep_one_blank_row_on_each_side(self):
        session = {'id': 'active', 'updated': 0, 'providers': [], 'models': [], 'quota': []}
        for mode in CHART_TYPES:
            for values in ([0] * 20, [0] * 5 + [1000] + [0] * 14):
                with self.subTest(mode=mode, active=any(values)):
                    history = {'chart': values, 'current': session, 'previous': None,
                               'history': [], 'total_history': []}
                    texts = [text for text, _ in session_lines(history, 0, 32,
                                                               chart_type=mode)]
                    self.assertEqual(texts[1], '')
                    self.assertTrue(texts[9].strip().endswith('now'))
                    self.assertEqual(texts[10], '')
                    self.assertTrue(texts[11].startswith('CURRENT SESSION'))

    @patch('dashboard.curses.mouseinterval')
    @patch('dashboard.curses.mousemask')
    @patch('dashboard.curses.curs_set')
    def test_live_watch_draws_selected_chart(self, _curs_set, _mousemask, _mouseinterval):
        session = {'id': 'active', 'updated': 0, 'providers': [], 'models': [], 'quota': []}
        history = {'chart': [0] * 10 + [1000] + [0] * 8 + [200],
                   'current': session, 'previous': None, 'history': [], 'total_history': []}
        screen = MagicMock()
        screen.getmaxyx.return_value = (24, 34)
        screen.getch.side_effect = SystemExit
        args = Namespace(owner=None, profile='default', providers=None, side=None, interval=None)
        with patch('dashboard.defaults', return_value=dict(DEFAULTS, profile='default',
                                                           providers=[], chart_type='trace')), \
             patch('dashboard.initialize_colors', return_value={}), \
             patch('dashboard.session_summary', return_value=history):
            with self.assertRaises(SystemExit):
                watch(screen, args)
        drawn = '\n'.join(call.args[2] for call in screen.addnstr.call_args_list)
        self.assertIn('TOKEN TRACE', drawn)
        self.assertIn('⡇', drawn)

    def test_once_prints_selected_chart(self):
        from dashboard import main
        session = {'id': 'active', 'updated': 0, 'providers': [], 'models': [], 'quota': []}
        history = {'chart': [0] * 10 + [1000] + [0] * 8 + [200],
                   'current': session, 'previous': None, 'history': [], 'total_history': []}
        sink = io.BytesIO()
        output = io.TextIOWrapper(sink, encoding='utf-8')
        with patch('dashboard.defaults', return_value=dict(DEFAULTS, profile='default',
                                                           providers=[], chart_type='trace')), \
             patch('dashboard.session_summary', return_value=history), \
             patch.object(sys, 'argv', ['dashboard.py', 'watch', '--once']), \
             redirect_stdout(output):
            self.assertEqual(main(), 0)
        output.flush()
        rendered = sink.getvalue().decode('utf-8')
        self.assertIn('TOKEN TRACE', rendered)
        self.assertIn('⡇', rendered)

    def test_section_divider_uses_secondary_base_color(self):
        _text, style = section_heading('TOKEN RATE', 32, 'tok/min')
        self.assertEqual(style[0], 'secondary')

    @patch('dashboard.curses.mouseinterval')
    @patch('dashboard.curses.mousemask')
    @patch('dashboard.curses.curs_set')
    def test_live_watch_draws_section_labels_bold(self, _curs_set, _mousemask, _mouseinterval):
        session = {'id': 'active12', 'updated': 0, 'providers': [], 'models': [], 'quota': []}
        model = {'provider': 'openai-codex', 'total': 1}
        history = {'chart': [0] * 19 + [200], 'current': session, 'previous': session,
                   'history': [model], 'total_history': [model]}
        config = dict(DEFAULTS, profile='default', providers=['github-copilot'],
                      chart_type='trace', refresh=0)
        args = Namespace(owner=None, profile='default', providers=None, side=None, interval=None)
        labels = ('TOKEN TRACE', 'CURRENT SESSION', 'PREVIOUS SESSION',
                  'HISTORY OTHER SESSIONS', 'HISTORY TOTAL', 'COPILOT', 'Account 1', 'Account 2')
        limits = [{'id': 'monthly', 'label': 'Monthly', 'amount': {'usedFraction': .25}}]
        accounts = {'reports': [{'provider': 'github-copilot', 'fetchedAt': time.time() * 1000,
                                 'limits': limits} for _ in range(2)]}

        def draw(has_colors, width):
            screen = MagicMock()
            screen.getmaxyx.return_value = (60, width)
            screen.getch.side_effect = [-1, SystemExit]
            screen.erase.side_effect = screen.addnstr.reset_mock
            job = MagicMock(started=0)
            job.finish.return_value = (accounts, None)
            with ExitStack() as stack:
                for name, value in (('has_colors', has_colors), ('start_color', None),
                                    ('use_default_colors', None), ('init_pair', None)):
                    stack.enter_context(patch(f'dashboard.curses.{name}', return_value=value))
                stack.enter_context(patch('dashboard.curses.color_pair', side_effect=lambda n: n << 8))
                stack.enter_context(patch('dashboard.curses.COLORS', 256, create=True))
                stack.enter_context(patch('dashboard.curses.COLOR_PAIRS', 64, create=True))
                stack.enter_context(patch('dashboard.defaults', return_value=config))
                stack.enter_context(patch('dashboard.session_summary', return_value=history))
                stack.enter_context(patch('dashboard.active_session', return_value=None))
                stack.enter_context(patch('dashboard.FetchJob', return_value=job))
                stack.enter_context(patch('dashboard.time.monotonic', side_effect=range(100)))
                with self.assertRaises(SystemExit):
                    watch(screen, args)
            return [call.args for call in screen.addnstr.call_args_list]

        for has_colors in (True, False):
            for width, expected in ((40, labels), (22, ('TOKEN TRACE', 'Account 1', 'Account 2'))):
                with self.subTest(colors=has_colors, width=width):
                    calls = draw(has_colors, width)
                    for label in expected:
                        label_attrs = [attr for _row, _col, text, _n, attr in calls if text == label]
                        self.assertTrue(label_attrs, label)
                        self.assertTrue(all(attr & curses.A_BOLD for attr in label_attrs), label)
                    rules = [attr for _row, _col, text, _n, attr in calls
                             if any(text.startswith(label + ' ') for label in expected)]
                    self.assertTrue(rules)
                    self.assertFalse(any(attr & curses.A_BOLD for attr in rules))
                    chart = [attr for _row, _col, text, _n, attr in calls
                             if any('\u2800' <= char <= '\u28ff' for char in text)]
                    self.assertTrue(chart)
                    self.assertFalse(any(attr & curses.A_BOLD for attr in chart))

    def test_command_box_uses_one_border_color_and_capitalized_actions(self):
        rows = command_box_rows(1, 60, '', 40)
        self.assertEqual(clean('┌─┐│└┘'), '┌─┐│└┘')
        self.assertNotIn('?', ''.join(text for text, _style in rows))
        self.assertEqual(rows[0][1], 'secondary')
        self.assertEqual(rows[-1][1], 'secondary')
        for text, style in rows[1:4]:
            self.assertEqual(style[:3], ('secondary', 1, len(text) - 1))
        self.assertIn('r Refresh | q Hide | Scroll', rows[3][0])
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
        self.assertIn('r Refresh | q Hide | Scroll', writes[footer_row + 3])
        self.assertTrue(writes[footer_row + 4].startswith('└'))

class CommandsVisibilityTests(unittest.TestCase):
    def test_old_live_config_migrates_history_visibility_before_fallback_merge(self):
        fallback = dict(DEFAULTS, profile='default', refresh=0)
        old = dict(fallback, compact=False, history_visible=False)
        del old['history_other_visible']
        del old['history_total_visible']
        with patch('dashboard.mux', return_value=json.dumps(old)):
            config = load_config('%1', fallback)
        self.assertFalse(config['history_other_visible'])
        self.assertFalse(config['history_total_visible'])
        self.assertFalse(config['compact'])
        self.assertTrue(config['enabled'])
        change_config(config, ['history-other', 'show'])
        self.assertTrue(config['history_other_visible'])
        self.assertFalse(config['history_total_visible'])
        change_config(config, ['history-total', 'show'])
        self.assertTrue(config['history_total_visible'])

    def test_live_chart_migration_precedes_fallback_merge_without_rewriting_pane(self):
        fallback = dict(DEFAULTS, profile='work', refresh=0, chart_type='trace')
        for mode in ('line', 'area', 'heatmap', 'lollipop'):
            with self.subTest(mode=mode):
                raw = json.dumps({'chart_type': mode, 'history_visible': False})
                with patch('dashboard.mux', return_value=raw) as mux:
                    config = load_config('%1', fallback)
                self.assertEqual(config['chart_type'], 'bars')
                self.assertFalse(config['history_other_visible'])
                self.assertFalse(config['history_total_visible'])
                self.assertEqual(fallback['chart_type'], 'trace')
                self.assertEqual(mux.call_count, 1)

        for raw in ('{}', '{"history_visible": true}'):
            with self.subTest(raw=raw), patch('dashboard.mux', return_value=raw):
                self.assertEqual(load_config('%1', fallback)['chart_type'], 'trace')
        for value in ('pie', None, 0, ['line']):
            with self.subTest(value=value), \
                 patch('dashboard.mux', return_value=json.dumps({'chart_type': value})), \
                 self.assertRaises(ValueError):
                load_config('%1', fallback)


    def test_visibility_commands_change_only_the_target_section(self):
        config = deepcopy(DEFAULTS)
        for section in ('commands', 'previous', 'history-other', 'history-total'):
            with self.subTest(section=section):
                change_config(config, [section, 'hide'])
                self.assertFalse(config[f'{section.replace("-", "_")}_visible'])
                change_config(config, [section, 'show'])
                self.assertTrue(config[f'{section.replace("-", "_")}_visible'])
        self.assertNotIn('history_visible', config)

    def test_session_sections_hide_independently_without_changing_usage(self):
        session = {'id': 'session-1', 'updated': 0,
                   'providers': [{'provider': 'openai-codex', 'total': 12000}],
                   'models': [], 'quota': []}
        model = {'provider': 'openai-codex', 'model': 'model-a', 'input': 100,
                 'output': 20, 'cache_read': 30, 'cache_write': 10}
        history = {'chart': [0, 1, 2], 'current': session, 'previous': session,
                   'history': [dict(model, total=8000)],
                   'total_history': [dict(model, total=20000)]}
        original = deepcopy(history)
        for compact in (True, False):
            for previous, other, total in (
                    (previous, other, total)
                    for previous in (False, True)
                    for other in (False, True)
                    for total in (False, True)):
                with self.subTest(compact=compact, previous=previous, other=other, total=total):
                    rows = session_lines(history, 0, 60, compact, previous, other, total)
                    text = '\n'.join(line for line, _ in rows)
                    self.assertEqual('PREVIOUS SESSION' in text, previous)
                    self.assertEqual('HISTORY OTHER SESSIONS' in text, other)
                    self.assertEqual('HISTORY TOTAL' in text, total)
                    self.assertNotIn('Other sessions', text)
                    self.assertNotIn('Project total', text)
                    self.assertIn('CURRENT SESSION', text)
                    self.assertIn('TOKEN RATE', text)
                    self.assertIn('12k', text)
                    self.assertFalse(any(not rows[i][0] and not rows[i + 1][0]
                                         for i in range(len(rows) - 1)))
        self.assertEqual(history, original)

    def test_empty_project_has_no_hidden_section_headings(self):
        history = {'current': None, 'previous': None, 'chart': [],
                   'history': [], 'total_history': []}
        for compact in (True, False):
            text = '\n'.join(line for line, _ in session_lines(
                history, 0, 32, compact, False, True, True))
            self.assertIn('Waiting for OMP session', text)
            self.assertNotIn('PREVIOUS SESSION', text)
            self.assertNotIn('HISTORY OTHER SESSIONS', text)
            self.assertNotIn('HISTORY TOTAL', text)

    def test_live_toggle_reclaims_space_and_clamps_scroll_offset(self):
        config = dict(DEFAULTS, profile='default', refresh=0)
        frames = []
        screen = MagicMock()
        screen.getmaxyx.return_value = (12, 40)
        screen.refresh.side_effect = lambda: frames.append(
            {call.args[0]: call.args[2] for call in screen.addnstr.call_args_list})
        screen.erase.side_effect = screen.addnstr.reset_mock
        screen.getch.side_effect = [curses.KEY_NPAGE, -1, -1, SystemExit]
        updates = [config, config, dict(config, commands_visible=False), config]
        args = Namespace(owner='%1', profile='default', providers=None, side=None, interval=None)
        with ExitStack() as stack:
            for name in ('curs_set', 'mousemask', 'mouseinterval'):
                stack.enter_context(patch('dashboard.curses.' + name))
            stack.enter_context(patch('dashboard.defaults', return_value=config))
            stack.enter_context(patch('dashboard.initialize_colors', return_value={}))
            stack.enter_context(patch('dashboard.mux', return_value='0'))
            stack.enter_context(patch('dashboard.load_config', side_effect=updates))
            stack.enter_context(patch('dashboard.time.monotonic', side_effect=[0, 1, 2, 3]))
            stack.enter_context(patch('dashboard.session_summary', return_value={}))
            stack.enter_context(patch('dashboard.session_lines',
                                      side_effect=lambda *_, **__: [(f'entry {i}', 'normal') for i in range(12)]))
            with self.assertRaises(SystemExit):
                watch(screen, args)
        self.assertTrue(any('COMMANDS' in text for text in frames[0].values()))
        self.assertFalse(any('COMMANDS' in text for text in frames[2].values()))
        self.assertEqual(frames[2][10], 'Choose Add to select a provider.')
        self.assertEqual(frames[2][0], 'entry 4')
        self.assertTrue(any('COMMANDS' in text for text in frames[3].values()))

    def test_hidden_footer_click_does_not_close_sidebar(self):
        config = dict(DEFAULTS, commands_visible=False, profile='default', refresh=0)
        screen = MagicMock()
        screen.getmaxyx.return_value = (12, 40)
        screen.getch.side_effect = [curses.KEY_MOUSE, SystemExit]
        args = Namespace(owner=None, profile='default', providers=None, side=None, interval=None)
        with ExitStack() as stack:
            for name in ('curs_set', 'mousemask', 'mouseinterval'):
                stack.enter_context(patch('dashboard.curses.' + name))
            stack.enter_context(patch('dashboard.curses.getmouse',
                                      return_value=(0, 14, 8, 0, curses.BUTTON1_CLICKED)))
            stack.enter_context(patch('dashboard.defaults', return_value=config))
            stack.enter_context(patch('dashboard.initialize_colors', return_value={}))
            stack.enter_context(patch('dashboard.session_summary', return_value={}))
            stack.enter_context(patch('dashboard.session_lines', return_value=[]))
            with self.assertRaises(SystemExit):
                watch(screen, args)


class DashboardShutdownTests(unittest.TestCase):
    @patch('dashboard.os.killpg')
    def test_refresh_worker_escalates_quickly_when_stuck(self, killpg):
        job = FetchJob.__new__(FetchJob)
        job.process = MagicMock(pid=123, poll=MagicMock(return_value=None))
        job.process.wait.side_effect = [
            subprocess.TimeoutExpired('omp', 0.25),
            None,
        ]
        job.output = MagicMock()
        job.error = MagicMock()

        job.close()

        self.assertEqual(killpg.call_args_list, [
            ((123, signal.SIGTERM), {}),
            ((123, signal.SIGKILL), {}),
        ])
        job.process.wait.assert_any_call(timeout=0.25)


class DashboardPollingTests(unittest.TestCase):
    def setUp(self):
        self.screen = MagicMock()
        self.screen.getmaxyx.return_value = (12, 80)
        self.args = Namespace(owner=None, profile='default', providers=None,
                              side=None, interval=None)
        self.config = dict(DEFAULTS, providers=['openai-codex'], profile='default', refresh=0)
        self.colors = {name: 0 for name in ('normal', 'dim', 'secondary', 'title',
                                            'chart', 'good', 'warn', 'error')}
        self.history = {'current': None, 'previous': None, 'chart': [],
                        'history': [], 'total_history': []}

    def run_watch(self, fetch_jobs, ticks, keys):
        self.screen.getch.side_effect = keys
        jobs = iter(fetch_jobs)
        with patch('dashboard.curses.curs_set'), \
             patch('dashboard.curses.mousemask'), \
             patch('dashboard.curses.mouseinterval'), \
             patch('dashboard.defaults', return_value=self.config), \
             patch('dashboard.initialize_colors', return_value=self.colors), \
             patch('dashboard.session_summary', return_value=self.history), \
             patch('dashboard.active_session', return_value=None), \
             patch('dashboard.record_quota'), \
             patch('dashboard.FetchJob', side_effect=lambda *_args, **_kwargs: next(jobs)), \
             patch('dashboard.time.monotonic', side_effect=ticks):
            with self.assertRaises(SystemExit):
                watch(self.screen, self.args)

    def test_resumes_provider_poll_after_dashboard_loop_was_suspended(self):
        first = MagicMock(started=0)
        first.finish.return_value = None
        second = MagicMock(started=6)
        second.finish.return_value = ({'reports': []}, None)

        self.run_watch([first, second], [0, 6, 7], [-1, -1, SystemExit])

        first.close.assert_called_once_with()
        self.assertEqual(first.finish.call_count, 0)
        self.assertEqual(second.finish.call_count, 1)

    def test_failed_refresh_retries_before_full_poll_interval(self):
        first = MagicMock(started=0)
        first.finish.return_value = (None, 'Refresh failed')
        second = MagicMock(started=16)
        second.finish.return_value = ({'reports': []}, None)

        with patch('dashboard.RESUME_GAP_SECONDS', 100):
            self.run_watch([first, second], [0, 1, 15, 16], [-1, -1, -1, SystemExit])

        self.assertEqual(first.finish.call_count, 1)
        first.close.assert_called_once_with()
        self.assertEqual(second.finish.call_count, 0)


class PaneOwnershipTests(unittest.TestCase):
    @patch('dashboard.mux')
    def test_owned_panes_returns_only_panes_tagged_to_owner(self, mux):
        mux.return_value = '%1\t%owner\n%2\t%other\n%3\t%owner\n%4\t\n'

        self.assertEqual(owned_panes('%owner'), ['%1', '%3'])
        mux.assert_called_once_with('list-panes', '-a', '-F',
                                     '#{pane_id}\t#{@omp_usage_owner}')


class PassthroughContainmentTests(unittest.TestCase):
    def test_claude_rejects_image_passthrough(self):
        with self.assertRaisesRegex(ValueError, 'only in OMP'):
            change_config(deepcopy(dict(DEFAULTS)), ['images', 'on'], host='claude')

    @patch('dashboard.os.execv', side_effect=RuntimeError('stop launch'))
    @patch('dashboard.shutil.get_terminal_size', return_value=Namespace(columns=120, lines=40))
    @patch('dashboard.tmux_binary', return_value='/tmux')
    @patch('dashboard.mux')
    def test_new_dashboard_disables_terminal_passthrough(self, mux, _tmux_binary,
                                                         _terminal_size, _execv):
        mux.return_value = '%1'
        args = Namespace(profile=None, native=True)
        with patch.dict('dashboard.os.environ', {'TMUX': ''}), \
             self.assertRaisesRegex(RuntimeError, 'stop launch'):
            launch(args, [])

        passthrough = [call.args for call in mux.call_args_list
                       if 'allow-passthrough' in call.args]
        self.assertEqual(len(passthrough), 1)
        self.assertEqual(passthrough[0][-2:], ('allow-passthrough', 'off'))


IMAGE_ENVIRONMENT = ('KITTY_WINDOW_ID', 'GHOSTTY_RESOURCES_DIR', 'PI_FORCE_IMAGE_PROTOCOL',
                     'PI_NO_KITTY_PLACEHOLDERS', 'PI_KITTY_PLACEHOLDERS', 'HERDR_ENV',
                     'HERDR_PANE_ID', 'HERDR_TAB_ID', 'HERDR_WORKSPACE_ID', 'STY', 'ZELLIJ',
                     'TMUX_PANE', 'PI_PROFILE')


class OmpImagePassthroughTests(unittest.TestCase):
    """Exercise pane-scoped passthrough against an isolated tmux server."""

    def setUp(self):
        try:
            tmux_binary()
        except ValueError:
            self.skipTest('tmux 3.3+ not installed')
        import dashboard
        self.dashboard = dashboard
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {
            'TMUX': '', 'OMP_PROFILE': 'default',
            'PI_CODING_AGENT_DIR': str(Path(directory.name) / 'agent')}))
        for name in IMAGE_ENVIRONMENT:
            os.environ.pop(name, None)
        os.environ['GHOSTTY_RESOURCES_DIR'] = '/Applications/Ghostty.app/Contents/Resources/ghostty'
        stack.enter_context(patch('dashboard.SOCKET_NAME', 'omp-usage-test-' + os.urandom(6).hex()))
        self.mux = dashboard.mux
        self.owner = self.mux('new-session', '-d', '-x', '160', '-y', '40', '-P', '-F',
                              '#{pane_id}', 'sleep 300')
        stack.callback(self.mux, 'kill-server')
        # A dashboard-owned launch window starts with window-wide passthrough off.
        self.mux('set-option', '-w', '-t', self.owner, 'allow-passthrough', 'off')
        self.sidebar = self.mux('split-window', '-h', '-d', '-t', self.owner, '-P', '-F',
                                '#{pane_id}', 'sleep 300')
        self.mux('set-option', '-p', '-t', self.sidebar, '@omp_usage_owner', self.owner)
        self.args = Namespace(owner=self.owner, profile='default', providers=None,
                              side=None, interval=None)

    def run_control(self, *words, owner=True):
        self.args.owner = self.owner if owner else None
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            control(self.args, list(words))
        return output.getvalue(), errors.getvalue()

    def pane_value(self, pane):
        return self.mux('show-options', '-p', '-v', '-q', '-t', pane, 'allow-passthrough')

    def requested(self):
        return load_preferences('default')['images_enabled']

    def test_default_init_blocks_owner_and_detach_unsets_owner_option(self):
        output, _ = self.run_control('init')

        self.assertIn('Images off', output)
        self.assertEqual(self.pane_value(self.owner), 'off')
        self.assertEqual(self.pane_value(self.sidebar), 'off')
        self.run_control('detach')
        self.assertEqual(self.pane_value(self.owner), '')

    def test_images_on_passes_only_owner_pane_and_detach_restores_prior_value(self):
        self.mux('set-option', '-p', '-t', self.owner, 'allow-passthrough', 'all')

        output, _ = self.run_control('images', 'on')
        self.run_control('images', 'on')

        self.assertIn('Images on (best effort)', output)
        self.assertIn('can still draw over the sidebar', output)
        self.assertTrue(self.requested())
        self.assertEqual(self.pane_value(self.owner), 'on')
        self.assertEqual(self.pane_value(self.sidebar), 'off')
        self.assertEqual(self.mux('show-options', '-w', '-v', '-t', self.owner,
                                  'allow-passthrough'), 'off')
        self.run_control('detach')
        self.assertEqual(self.pane_value(self.owner), 'all')
        self.assertEqual(self.mux('show-options', '-p', '-v', '-q', '-t', self.owner,
                                  '@omp_usage_passthrough'), '')

    def test_saved_request_applies_on_init_and_images_off_blocks_inherited_passthrough(self):
        update_preferences('default', {'images_enabled': True})
        self.mux('set-option', '-w', '-t', self.owner, 'allow-passthrough', 'on')

        self.run_control('init')
        self.assertEqual(self.pane_value(self.owner), 'on')
        self.assertEqual(self.pane_value(self.sidebar), 'off')
        output, _ = self.run_control('images', 'off')

        self.assertIn('Images off', output)
        self.assertFalse(self.requested())
        self.assertEqual(self.pane_value(self.owner), 'off')
        self.assertEqual(self.pane_value(self.sidebar), 'off')
        self.assertEqual(owned_panes(self.owner), [self.sidebar])

    def test_detach_keeps_owner_value_edited_outside_dashboard(self):
        self.run_control('images', 'on')
        self.mux('set-option', '-p', '-t', self.owner, 'allow-passthrough', 'all')

        self.run_control('detach')

        self.assertEqual(self.pane_value(self.owner), 'all')

    def test_detach_restores_owner_even_when_sidebar_kill_fails(self):
        self.mux('set-option', '-p', '-t', self.owner, 'allow-passthrough', 'all')
        self.run_control('images', 'on')
        real = self.mux

        def failing(*args):
            if args[0] == 'kill-pane':
                raise subprocess.CalledProcessError(1, 'tmux kill-pane')
            return real(*args)

        with patch('dashboard.mux', side_effect=failing), \
             self.assertRaises(subprocess.CalledProcessError):
            self.run_control('detach')

        self.assertEqual(self.pane_value(self.owner), 'all')
        self.assertEqual(self.mux('show-options', '-p', '-v', '-q', '-t', self.owner,
                                  '@omp_usage_passthrough'), '')

    def test_pane_split_in_after_images_on_is_revoked_before_status(self):
        self.run_control('images', 'on')
        foreign = self.mux('split-window', '-v', '-d', '-t', self.owner, '-P', '-F',
                           '#{pane_id}', 'sleep 300')

        listed, _ = self.run_control('view', 'list')

        self.assertIn('requested but inactive: other panes share this tmux window', listed)
        self.assertEqual(self.pane_value(self.owner), 'off')
        self.assertTrue(self.requested())
        self.mux('kill-pane', '-t', foreign)
        self.run_control('init')
        self.assertEqual(self.pane_value(self.owner), 'on')

    def test_sidebar_poll_revokes_images_after_pane_split_in(self):
        self.run_control('images', 'on')
        self.mux('split-window', '-v', '-d', '-t', self.owner, '-P', '-F', '#{pane_id}',
                 'sleep 300')
        screen = MagicMock()
        screen.getmaxyx.return_value = (40, 34)
        screen.getch.side_effect = SystemExit
        colors = {name: 0 for name in ('normal', 'dim', 'secondary', 'title',
                                       'chart', 'good', 'warn', 'error')}
        history = {'current': None, 'previous': None, 'chart': [],
                   'history': [], 'total_history': []}
        job = MagicMock(started=0)
        job.finish.return_value = None

        with patch('dashboard.curses.curs_set'), patch('dashboard.curses.mousemask'), \
             patch('dashboard.curses.mouseinterval'), \
             patch('dashboard.initialize_colors', return_value=colors), \
             patch('dashboard.session_summary', return_value=history), \
             patch('dashboard.active_session', return_value=None), \
             patch('dashboard.FetchJob', return_value=job), \
             self.assertRaises(SystemExit):
            watch(screen, self.args)

        self.assertEqual(self.pane_value(self.owner), 'off')

    def test_unsupported_images_on_is_rejected_without_saving_request(self):
        cases = {
            'unknown terminal': {'GHOSTTY_RESOURCES_DIR': None},
            'forced sixel': {'PI_FORCE_IMAGE_PROTOCOL': 'sixel'},
            'placeholders disabled': {'PI_NO_KITTY_PLACEHOLDERS': '1'},
            'sibling pane': {},
        }
        for name, environment in cases.items():
            with self.subTest(name), patch.dict(os.environ, {}):
                for key, value in environment.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                if name == 'sibling pane':
                    sibling = self.mux('split-window', '-v', '-d', '-t', self.owner, '-P', '-F',
                                       '#{pane_id}', 'sleep 300')
                try:
                    with self.assertRaisesRegex(ValueError, 'Images unavailable'):
                        self.run_control('images', 'on')
                finally:
                    if name == 'sibling pane':
                        self.mux('kill-pane', '-t', sibling)
                self.assertFalse(self.requested())
                self.assertEqual(self.pane_value(self.owner), 'off')

    def test_unrelated_command_keeps_external_owner_edit_until_detach(self):
        for first in (('images', 'on'), ('init',)):
            with self.subTest(first=first):
                update_preferences('default', {'images_enabled': False})
                self.mux('set-option', '-p', '-u', '-t', self.owner, 'allow-passthrough')
                self.run_control(*first)
                self.mux('set-option', '-p', '-t', self.owner, 'allow-passthrough', 'all')

                output, _ = self.run_control('theme', 'blue')
                listed, _ = self.run_control('view', 'list')

                self.assertIn('requested but not applied', output)
                self.assertIn('set to all outside the dashboard', listed)
                self.assertEqual(self.pane_value(self.owner), 'all')
                self.assertEqual(self.pane_value(self.sidebar), 'off')
                self.run_control('detach')
                self.assertEqual(self.pane_value(self.owner), 'all')
                self.sidebar = self.mux('split-window', '-h', '-d', '-t', self.owner, '-P', '-F',
                                        '#{pane_id}', 'sleep 300')
                self.mux('set-option', '-p', '-t', self.sidebar, '@omp_usage_owner', self.owner)

    def test_failed_images_off_save_keeps_current_session_blocked(self):
        self.run_control('images', 'on')

        with patch('dashboard.update_preferences', side_effect=OSError('disk full')), \
             self.assertRaisesRegex(ValueError, 'later launches will still request images'):
            self.run_control('images', 'off')
        output, _ = self.run_control('init')

        self.assertIn('Images off', output)
        self.assertEqual(self.pane_value(self.owner), 'off')
        self.assertTrue(self.requested())

    def run_cli(self, *words):
        socket = self.mux('display-message', '-p', '#{socket_path}')
        env = {**os.environ, 'TMUX': socket + ',0,0'}
        env.pop('TMUX_PANE', None)
        return subprocess.run(
            [sys.executable, str(ROOT / 'dashboard.py'), 'control', '--owner', self.owner,
             '--', *words], env=env, capture_output=True, text=True, timeout=30)

    def corrupt_preferences(self):
        path = preferences_path('default')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{not json', encoding='utf-8')
        return path

    def test_cli_detach_restores_owner_despite_malformed_preferences(self):
        self.run_control('images', 'on')
        self.assertEqual(self.pane_value(self.owner), 'on')
        path = self.corrupt_preferences()

        result = self.run_cli('detach')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pane_value(self.owner), '')
        self.assertEqual(self.mux('show-options', '-p', '-v', '-q', '-t', self.owner,
                                  '@omp_usage_passthrough'), '')
        self.assertNotIn(self.sidebar, self.mux('list-panes', '-a', '-F', '#{pane_id}').split())
        self.assertEqual(path.read_text(encoding='utf-8'), '{not json')

    def test_cli_images_off_blocks_owner_but_reports_malformed_preferences_unsaved(self):
        self.run_control('images', 'on')
        path = self.corrupt_preferences()

        result = self.run_cli('images', 'off')

        self.assertEqual(result.returncode, 1)
        self.assertIn('blocked in this OMP pane', result.stderr)
        self.assertIn('saved preference was not updated', result.stderr)
        self.assertEqual(self.pane_value(self.owner), 'off')
        self.assertEqual(self.pane_value(self.sidebar), 'off')
        self.assertFalse(load_config(self.owner, dict(DEFAULTS))['images_enabled'])
        self.assertEqual(path.read_text(encoding='utf-8'), '{not json')
        self.assertEqual(self.run_cli('detach').returncode, 0)
        self.assertEqual(self.pane_value(self.owner), '')

    def test_repeated_images_command_saves_request_changed_outside_pane(self):
        for command, external in (('on', False), ('off', True)):
            with self.subTest(command=command):
                self.run_control('images', command)
                update_preferences('default', {'images_enabled': external})

                self.run_control('images', command)

                self.assertEqual(self.requested(), command == 'on')
                self.run_control('init')
                self.assertEqual(self.pane_value(self.owner), command)

    def test_saved_request_on_unsupported_relaunch_reports_inactive(self):
        update_preferences('default', {'images_enabled': True})
        os.environ['PI_FORCE_IMAGE_PROTOCOL'] = 'iterm2'

        output, errors = self.run_control('init')

        self.assertIn('requested but inactive', output)
        self.assertIn('requested but inactive', errors)
        self.assertEqual(self.pane_value(self.owner), 'off')
        self.assertTrue(self.requested())

    def test_failed_preference_save_turns_new_request_back_off(self):
        with patch('dashboard.update_preferences', side_effect=OSError('disk full')), \
             self.assertRaisesRegex(OSError, 'disk full'):
            self.run_control('images', 'on')

        self.assertFalse(self.requested())
        self.assertEqual(self.pane_value(self.owner), 'off')
        self.assertFalse(load_config(self.owner, dict(DEFAULTS))['images_enabled'])

    def test_request_without_tmux_owner_is_saved_as_pending(self):
        output, _ = self.run_control('images', 'on', owner=False)

        self.assertIn('pending', output)
        self.assertTrue(self.requested())
        os.environ.pop('GHOSTTY_RESOURCES_DIR')
        self.run_control('images', 'off', owner=False)
        with self.assertRaisesRegex(ValueError, 'Images unavailable'):
            self.run_control('images', 'on', owner=False)
        self.assertFalse(self.requested())


class OmpLaunchContractTests(unittest.TestCase):
    def test_profile_and_native_extension_preserve_omp_argument_order(self):
        args = Namespace(host='omp', profile='work', native=False)
        with patch.dict(os.environ, {'TMUX': ''}), \
             patch('dashboard.defaults', return_value={'enabled': True}), \
             patch('dashboard.extension_path', return_value=Path('/extension.js')), \
             patch('host_adapters.shutil.which', return_value='/omp'), \
             patch('dashboard.shutil.get_terminal_size',
                   return_value=Namespace(columns=120, lines=40)), \
             patch('dashboard.mux', side_effect=RuntimeError('capture')) as mux:
            for native, expected in (
                    (False, ['env', 'OMP_USAGE_LAUNCHER=1', '/omp', '-e', '/extension.js']),
                    (True, ['env', 'OMP_USAGE_LAUNCHER=0', '/omp'])):
                args.native = native
                with self.subTest(native=native), self.assertRaisesRegex(RuntimeError, 'capture'):
                    launch(args, ['--model', 'selected'])
                command = shlex.split(mux.call_args.args[-1])
                self.assertEqual(command, [*expected, '--model', 'selected', '--profile', 'work'])


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

    def test_chart_command_changes_only_chart_type(self):
        config = deepcopy(DEFAULTS)
        for chart_type in CHART_TYPES:
            with self.subTest(chart_type=chart_type):
                before = deepcopy(config)
                change_config(config, ['chart', chart_type])
                self.assertEqual(config, {**before, 'chart_type': chart_type})

    def test_chart_control_reports_and_persists_selected_mode(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'HOME': directory, 'TMUX_PANE': ''}, clear=True):
            args = Namespace(host='omp', owner=None, profile='work', providers=None,
                             side=None, interval=None)
            path = preferences_path('work')
            before = load_preferences('work')
            self.assertFalse(path.exists())
            output = io.StringIO()
            with redirect_stdout(output):
                control(args, ['chart', 'bars'])
            self.assertIn('chart bars', output.getvalue())
            self.assertEqual(load_preferences('work'), {**before, 'chart_type': 'bars'})
            self.assertFalse(path.exists())

            for chart_type in CHART_TYPES:
                if chart_type == 'bars':
                    continue
                with self.subTest(chart_type=chart_type):
                    before = load_preferences('work')
                    output = io.StringIO()
                    with redirect_stdout(output):
                        control(args, ['chart', chart_type])
                    self.assertIn('chart ' + chart_type, output.getvalue())
                    self.assertEqual(load_preferences('work'),
                                     {**before, 'chart_type': chart_type})
                    self.assertEqual(json.loads(path.read_text())['chart_type'], chart_type)

            before = load_preferences('work')
            output = io.StringIO()
            with redirect_stdout(output):
                control(args, ['chart', 'bars'])
            self.assertIn('chart bars', output.getvalue())
            self.assertEqual(load_preferences('work'), {**before, 'chart_type': 'bars'})
            self.assertEqual(json.loads(path.read_text())['chart_type'], 'bars')

    def test_chart_control_rejects_invalid_type_and_arity_without_saving(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'HOME': directory, 'TMUX_PANE': ''}, clear=True):
            args = Namespace(host='omp', owner=None, profile='work', providers=None,
                             side=None, interval=None)
            with redirect_stdout(io.StringIO()):
                control(args, ['chart', 'dots'])
            path = preferences_path('work')
            original = path.read_bytes()
            for words in (['chart'], ['chart', 'pie'], ['chart', 'LINE'],
                          *(['chart', mode] for mode in ('line', 'area', 'heatmap', 'lollipop')),
                          ['chart', 'trace', 'extra'], ['view', 'line']):
                with self.subTest(words=words), self.assertRaises(ValueError):
                    control(args, words)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(load_preferences('work')['chart_type'], 'dots')

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

    def test_details_history_totals_survive_minimum_pane_width(self):
        model = {'provider': 'a-very-long-provider-name', 'model': 'model-a',
                 'total': 170000, 'input': 170000, 'output': 0,
                 'cache_read': 0, 'cache_write': 0}
        session = {'id': 'session', 'updated': 0, 'providers': [model],
                   'models': [model], 'quota': []}
        history = {'chart': [], 'current': session, 'previous': session,
                   'history': [model], 'total_history': [model]}
        rows = session_lines(history, 0, 20, compact=False)
        for prefix in ('a-very', 'Tokens'):
            totals = [text for text, _style in rows if text.startswith(prefix)]
            self.assertTrue(totals)
            for text in totals:
                self.assertTrue(text.endswith('170k'), text)
                self.assertLessEqual(len(text), 20)

    def test_history_headings_fit_standard_content_width(self):
        model = {'provider': 'openai-codex', 'total': 1}
        history = {'chart': [], 'current': None, 'previous': None,
                   'history': [model], 'total_history': [model]}
        texts = [text for text, _style in session_lines(history, 0, 32)]
        self.assertTrue(any(text.startswith('HISTORY OTHER SESSIONS') for text in texts))
        self.assertTrue(any(text.startswith('HISTORY TOTAL') for text in texts))

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
                     if text.startswith('HISTORY OTHER SESSIONS'))
        other_model = next(index for index, text in enumerate(texts[other + 1:], other + 1)
                           if text.startswith('CODEX / model-a'))
        total = next(index for index, text in enumerate(texts)
                     if text.startswith('HISTORY TOTAL'))
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
            'chart': [0, 1, 2], 'history': [{'provider': 'openai-codex', 'total': 8}],
            'total_history': [{'provider': 'openai-codex', 'total': 20}],
            'current': session, 'previous': session,
        }
        texts = [text for text, _style in session_lines(history, 0, 60, compact=True)]
        headings = [
            next(index for index, text in enumerate(texts) if text.startswith(prefix))
            for prefix in ('TOKEN RATE', 'CURRENT SESSION', 'PREVIOUS SESSION',
                           'HISTORY OTHER SESSIONS', 'HISTORY TOTAL')
        ]
        self.assertEqual(texts[1], '')
        self.assertEqual([texts[index - 1] for index in headings[1:]], ['', '', '', ''])
        self.assertNotIn('Other sessions', texts)
        self.assertNotIn('Project total', texts)
        self.assertEqual(texts[-1], '')

    def test_named_themes_preserve_status_semantics(self):
        for theme in THEME_NAMES:
            with self.subTest(theme=theme):
                config = deepcopy(DEFAULTS)
                change_config(config, ['theme', theme])
                tokens = resolve_tokens(config)
                self.assertEqual(tokens['good'], 'green')
                self.assertEqual(tokens['warn'], 'orange')
                self.assertEqual(tokens['error'], 'red')

    def test_blue_palette_uses_bright_cyan_blue_and_keeps_overrides(self):
        config = deepcopy(DEFAULTS)
        change_config(config, ['theme', 'blue'])
        tokens = resolve_tokens(config)
        self.assertEqual(tokens['accent'], '#00b4ff')
        self.assertEqual(tokens['chart'], '#00b4ff')
        self.assertEqual(tokens['secondary'], '#007fae')
        self.assertEqual((tokens['good'], tokens['warn'], tokens['error']),
                         ('green', 'orange', 'red'))

        change_config(config, ['theme', 'custom', 'accent', '#58A66A'])
        tokens = resolve_tokens(config)
        self.assertEqual(tokens['accent'], '#58a66a')
        self.assertEqual(tokens['chart'], '#00b4ff')

    def test_claude_palette_respects_custom_overrides_and_reset(self):
        config = deepcopy(DEFAULTS)
        self.assertEqual(config['theme'], 'green')
        change_config(config, ['theme', 'claude'])

        tokens = resolve_tokens(config)
        self.assertEqual(tokens['text'], '#faf9f5')
        self.assertEqual(tokens['muted'], '#b0aea5')
        self.assertEqual(tokens['secondary'], '#b0aea5')
        self.assertEqual(tokens['accent'], '#d97757')
        self.assertEqual(tokens['chart'], '#d97757')

        change_config(config, ['theme', 'custom', 'text', '#58A66A'])
        change_config(config, ['theme', 'blue'])
        change_config(config, ['theme', 'claude'])
        self.assertEqual(resolve_tokens(config)['text'], '#58a66a')

        change_config(config, ['theme', 'reset'])
        self.assertEqual(config['theme'], 'green')
        self.assertEqual(config['tokens'], {})
        self.assertEqual(resolve_tokens(config)['accent'], 'green')


class SyntheticHostIntegrationTests(unittest.TestCase):
    def test_registered_host_uses_real_preferences_source_ledger_and_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'synthetic_usage.py'
            source.write_text(
                'import json, sys, time\n'
                'print(json.dumps({"reports": [{"provider": sys.argv[1], '
                '"fetchedAt": int(time.time() * 1000), '
                '"limits": [{"id": "weekly", "label": "Weekly", '
                '"scope": {"accountId": "test-account"}, '
                '"amount": {"usedFraction": 0.4}}]}]}))\n')
            adapter = HostAdapter(
                host_id='synthetic', name='SYNTHETIC', default_providers=('toy',),
                allowed_providers=frozenset({'toy'}), aliases={'sample': 'toy'},
                empty_title='Allowance pending', empty_note='No sample yet',
                empty_compact_note='No sample yet', capture_status=False,
                keep_prior_on_empty=False, quota_history=True,
                launch_policy='exit-status', profile_resolver=lambda profile: profile or 'default',
                root_resolver=lambda profile: root / (profile or 'default'),
                fetch_builder=lambda provider, profile, owner: [sys.executable, str(source), provider],
                launch_builder=lambda argv, profile, native, extension, binary, status_path: [
                    'sh', '-c',
                    'status=$1; shift; "$@"; code=$?; printf "%s\\n" "$code" > "$status"; exit "$code"',
                    'synthetic-exit', str(status_path), sys.executable, '-c', 'raise SystemExit(0)', *argv])
            args = Namespace(host='synthetic', owner='owner-a', profile=None, providers='sample',
                             side=None, interval=None)
            with patch.object(host_adapters, '_HOSTS',
                              {**host_adapters._HOSTS, 'synthetic': adapter}), \
                 patch.dict(os.environ, {'TMUX': '', 'PI_CODING_AGENT_DIR': str(root / 'omp')}):
                from preferences import load_preferences
                config = defaults(args)
                self.assertEqual(config['providers'], ['toy'])
                self.assertEqual(config['profile'], 'default')
                self.assertEqual(load_preferences('default', host='synthetic')['providers'], ['toy'])
                self.assertEqual(defaults(Namespace(host='omp', profile=None, providers=None,
                                                    side=None, interval=None))['providers'], [])
                now = time.time()

                def entry(identity, total):
                    return {'id': identity, 'at': now, 'provider': 'toy', 'model': 'toy-model',
                            'input': total - 2, 'output': 2, 'cacheRead': 0,
                            'cacheWrite': 0, 'total': total}

                first = {'session': 'one', 'activation': 'first', 'owner': 'owner-a',
                         'socket': '', 'action': 'start', 'entries': [entry('event-a', 42)]}
                second = {'session': 'two', 'activation': 'second', 'owner': 'owner-b',
                          'socket': '', 'action': 'start', 'entries': [entry('event-b', 9)]}
                ingest(first, 'default', now=now, host='synthetic')
                ingest(first, 'default', now=now + 1, host='synthetic')
                ingest(second, 'default', now=now + 2, host='synthetic')
                history = summary('default', 'owner-a', now=now + 3, host='synthetic')
                other = summary('default', 'owner-b', now=now + 3, host='synthetic')
                self.assertEqual(history['current']['providers'][0]['total'], 42)
                self.assertEqual(other['current']['providers'][0]['total'], 9)
                self.assertEqual(history['history'][0]['total'], 9)
                self.assertEqual(history['total_history'][0]['total'], 51)
                rendered = '\n'.join(text for text, _ in session_lines(
                    history, now + 3, 42, host='synthetic'))
                self.assertIn('42', rendered)
                self.assertIn('9', rendered)

                job = FetchJob('toy', 'default', host='synthetic', owner='owner-a')
                try:
                    job.process.wait(timeout=5)
                    data, error = job.finish()
                finally:
                    job.close()
                self.assertIsNone(error)
                allowance = '\n'.join(text for text, _ in provider_lines(
                    data, 'toy', config, now, 42, host='synthetic'))
                self.assertIn('Weekly', allowance)
                self.assertIn('60% left', allowance)

                from dashboard import quota_samples
                samples = quota_samples(data)
                self.assertEqual(len(samples), 1)
                samples[0]['at'] = now + 3
                record_quota('default', 'owner-a', 'first', samples, host='synthetic')
                advanced = dict(samples[0], at=now + 4, used=.55)
                record_quota('default', 'owner-a', 'first', [advanced], host='synthetic')
                history = summary('default', 'owner-a', now=now + 5, host='synthetic')
                self.assertAlmostEqual(history['current']['quota'][0]['points'], 15)

                # The one-shot view uses the same registered source and stored ledger.
                from dashboard import main
                output = io.StringIO()
                with patch('dashboard.HOST_IDS', (*host_adapters.HOST_IDS, 'synthetic')), \
                     patch.object(sys, 'argv', ['dashboard.py', 'watch', '--host', 'synthetic',
                                                '--owner', 'owner-a', '--once']), \
                     redirect_stdout(output):
                    self.assertEqual(main(), 0)
                self.assertIn('60% left', output.getvalue())
                self.assertIn('42', output.getvalue())


    def test_once_failure_does_not_expose_source_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'failing_source.py'
            source.write_text(
                "import sys\nsys.stderr.write('private-canary\\n')\nraise SystemExit(7)\n")
            adapter = replace(host_adapters.get_host('omp'),
                              fetch_builder=lambda provider, profile, owner: [
                                  sys.executable, str(source)])
            errors = io.StringIO()
            with patch.object(host_adapters, '_HOSTS',
                              {**host_adapters._HOSTS, 'omp': adapter}), \
                 patch.dict(os.environ, {'TMUX': '', 'PI_CODING_AGENT_DIR': str(root / 'omp')}), \
                 patch.object(sys, 'argv', ['dashboard.py', 'watch', '--once', '--providers', 'toy']), \
                 redirect_stdout(io.StringIO()), redirect_stderr(errors):
                from dashboard import main
                self.assertEqual(main(), 1)
            self.assertIn('Refresh failed; check login/network', errors.getvalue())
            self.assertNotIn('private-canary', errors.getvalue())


class ClaudeDashboardTests(unittest.TestCase):
    def setUp(self):
        self.args = Namespace(host='claude', owner=None, profile=None, providers=None,
                              side=None, interval=None)
        self.history = {
            'current': {'id': 'claude-session', 'updated': 10,
                        'providers': [{'provider': 'anthropic', 'total': 125}],
                        'models': [{'provider': 'anthropic', 'model': 'sonnet', 'total': 125,
                                    'input': 100, 'output': 25, 'cache_read': 0,
                                    'cache_write': 0}], 'quota': []},
            'previous': {'id': 'prior-session', 'updated': 9,
                         'providers': [{'provider': 'anthropic', 'total': 80}],
                         'models': [], 'quota': []},
            'chart': [0, 60, 125],
            'history': [{'provider': 'anthropic', 'model': 'sonnet', 'total': 80,
                         'input': 80, 'output': 0, 'cache_read': 0, 'cache_write': 0}],
            'total_history': [{'provider': 'anthropic', 'model': 'sonnet', 'total': 205,
                               'input': 180, 'output': 25, 'cache_read': 0,
                               'cache_write': 0}],
        }

    def test_claude_uses_separate_preferences_and_only_anthropic(self):
        with patch('dashboard.load_preferences',
                   return_value=dict(DEFAULTS, providers=['anthropic'])) as load:
            config = defaults(self.args)
        load.assert_called_once_with(None, host='claude')
        self.assertEqual(config['profile'], None)
        self.assertEqual(config['providers'], ['anthropic'])
        config['compact'] = False
        change_config(config, ['view', 'compact'], host='claude')
        self.assertTrue(config['compact'])
        with self.assertRaisesRegex(ValueError, 'Anthropic'):
            change_config(config, ['providers', 'add', 'codex'], host='claude')

    def test_claude_limits_render_remaining_in_compact_and_details(self):
        data = {'reports': [{'provider': 'anthropic', 'fetchedAt': 10_000,
                             'limits': [
                                 {'id': 'claude:five_hour', 'label': '5 hour',
                                  'amount': {'usedFraction': .2},
                                  'window': {'resetsAt': 3_610_000, 'resetLabel': 'reset'}},
                                 {'id': 'claude:seven_day', 'label': '7 day',
                                  'amount': {'usedFraction': .75}},
                             ]}]}
        for compact in (True, False):
            with self.subTest(compact=compact):
                rows = [text for text, _ in provider_lines(
                    data, 'anthropic', {'interval': 60, 'compact': compact, 'windows': {}},
                    10, 40)]
                text = '\n'.join(rows)
                self.assertIn('5h', text)
                self.assertIn('7d', text)
                self.assertIn('80% left', text)
                self.assertIn('25% left', text)
                self.assertNotIn('20% used', text)
                self.assertEqual('ID: claude:five_hour' in rows, not compact)

    def test_same_anthropic_provider_keeps_host_specific_unknown_allowance(self):
        data = {'reports': [], 'dashboardNote': 'No captured rate-limit windows yet'}
        for compact in (True, False):
            config = {'compact': compact}
            claude = provider_lines(data, 'anthropic', config, 0, 32, host='claude')
            omp = provider_lines(data, 'anthropic', config, 0, 32)
            text = '\n'.join(row for row, _ in claude)
            self.assertIn('Allowance unknown', text)
            self.assertIn('No captured rate-limit windows yet', text)
            self.assertNotIn('0%', text)
            self.assertEqual(omp[0], ('Usage unavailable', 'warn'))
            self.assertEqual(omp[1][0], 'Check login / usage support' if compact
                             else 'No captured rate-limit windows yet')

    def test_claude_allowances_do_not_hide_missing_or_rejected_token_capture(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': directory, 'TMUX': '', 'TMUX_PANE': ''}):
            project = Path(directory) / 'project'
            project.mkdir()
            receiver = start_receiver(owner='pane-ui', cwd=str(project))

            def post(path, payload):
                request = urllib.request.Request(
                    receiver.address + path, data=json.dumps(payload).encode(), method='POST',
                    headers={'Authorization': 'Bearer ' + receiver.auth,
                             'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)

            try:
                now = time.time()
                post('/hook', {'hook_event_name': 'SessionStart', 'session_id': 'session-ui'})
                post('/statusline', {'session_id': 'session-ui', 'rate_limits': {
                    'five_hour': {'used_percentage': 25, 'resets_at': now + 3600}}})

                def verify(reason_fragment):
                    data = fetch_claude_usage(owner='pane-ui', cwd=str(project), now=now + 1)
                    status = data['capture']['status']
                    self.assertEqual(status, 'incomplete')
                    self.assertIn(reason_fragment, data['capture']['reason'])
                    for compact in (True, False):
                        config = {'interval': 60, 'compact': compact, 'windows': {}}
                        claude = provider_lines(data, 'anthropic', config, now + 1, 40,
                                                host='claude')
                        rendered = '\n'.join(row for row, _ in claude)
                        self.assertIn('75% left', rendered)
                        self.assertIn('Token capture ' + status, rendered)
                        self.assertIn(data['capture']['reason'], rendered)
                        self.assertNotIn('0 tokens', rendered)
                        omp = provider_lines(data, 'anthropic', config, now + 1, 40, host='omp')
                        self.assertNotIn('Token capture', '\n'.join(row for row, _ in omp))

                verify('No complete API request')
                attributes = [{'key': name, 'value': {'stringValue': value}}
                              for name, value in (('session.id', 'session-ui'),
                                                  ('event.name', 'api_request'),
                                                  ('event.timestamp', '2026-09-25T12:00:00Z'),
                                                  ('model', 'claude-test'))]
                attributes += [{'key': name, 'value': {'intValue': value}}
                               for name, value in (('input_tokens', 12), ('output_tokens', 7),
                                                   ('cache_creation_tokens', 2))]
                post('/v1/logs', {'resourceLogs': [{'scopeLogs': [{'logRecords': [{
                    'body': {'stringValue': 'claude_code.api_request'},
                    'attributes': attributes}]}]}]})
                verify('rejected or incomplete')
                unavailable = dict(fetch_claude_usage(owner='pane-ui', cwd=str(project), now=now + 1),
                                   capture={'status': 'unavailable',
                                            'reason': 'No active Claude capture.'})
                for compact in (True, False):
                    rendered = '\n'.join(text for text, _ in provider_lines(
                        unavailable, 'anthropic',
                        {'interval': 60, 'compact': compact, 'windows': {}},
                        now + 1, 40, host='claude'))
                    self.assertIn('75% left', rendered)
                    self.assertIn('Token capture unavailable', rendered)
                    self.assertIn('No active Claude capture.', rendered)
            finally:
                receiver.close()

    def test_claude_session_chart_and_visibility_use_existing_ledger(self):
        for compact in (True, False):
            rows = session_lines(self.history, 10, 48, compact=compact,
                                 previous_visible=False, history_other_visible=True,
                                 history_total_visible=False, host='claude')
            text = '\n'.join(row for row, _ in rows)
            self.assertIn('TOKEN RATE', text)
            self.assertIn('CURRENT SESSION', text)
            self.assertIn('HISTORY OTHER SESSIONS', text)
            self.assertIn('125', text)
            self.assertNotIn('PREVIOUS SESSION', text)
            self.assertNotIn('HISTORY TOTAL', text)
            self.assertTrue(any('▁' in row or '█' in row for row, _ in rows))
        empty = dict(self.history, current=None, previous=None)
        self.assertIn('Waiting for CLAUDE session', '\n'.join(
            row for row, _ in session_lines(empty, 10, 48, host='claude')))

    def test_claude_poll_replaces_stale_allowance_without_quota_history(self):
        screen = MagicMock()
        screen.getmaxyx.return_value = (70, 80)
        screen.getch.side_effect = [-1, ord('r'), -1, SystemExit]
        frames = []
        screen.refresh.side_effect = lambda: frames.append(
            '\n'.join(call.args[2] for call in screen.addnstr.call_args_list))
        screen.erase.side_effect = screen.addnstr.reset_mock
        first = MagicMock(started=0)
        first.finish.return_value = (
            {'reports': [{'provider': 'anthropic', 'fetchedAt': 1_000,
                          'limits': [{'id': 'five_hour', 'label': '5 hour',
                                      'amount': {'usedFraction': .25}}]}]}, None)
        second = MagicMock(started=2)
        second.finish.return_value = (
            {'reports': [], 'dashboardNote': 'No captured rate-limit windows yet'}, None)
        config = dict(DEFAULTS, profile=None, providers=['anthropic'], refresh=0)
        with ExitStack() as stack:
            for name in ('curs_set', 'mousemask', 'mouseinterval'):
                stack.enter_context(patch('dashboard.curses.' + name))
            stack.enter_context(patch('dashboard.defaults', return_value=config))
            stack.enter_context(patch('dashboard.initialize_colors', return_value={}))
            stack.enter_context(patch('dashboard.session_summary', return_value=self.history))
            stack.enter_context(patch('dashboard.time.monotonic', side_effect=[0, 1, 2, 3]))
            stack.enter_context(patch('dashboard.FetchJob', side_effect=[first, second]))
            quota = stack.enter_context(patch('dashboard.record_quota'))
            active = stack.enter_context(patch('dashboard.active_session'))
            with self.assertRaises(SystemExit):
                watch(screen, self.args)
        quota.assert_not_called()
        active.assert_not_called()
        self.assertTrue(any('75% left' in frame for frame in frames))
        self.assertIn('Allowance unknown', frames[-1])
        self.assertIn('No captured rate-limit windows yet', frames[-1])
        self.assertNotIn('75% left', frames[-1])

    def test_claude_keyboard_and_mouse_hide_save_only_claude_settings(self):
        config = dict(DEFAULTS, profile=None, providers=[], refresh=0)
        for key in (ord('q'), curses.KEY_MOUSE):
            with self.subTest(key=key), ExitStack() as stack:
                screen = MagicMock()
                screen.getmaxyx.return_value = (20, 80)
                screen.getch.return_value = key
                self.args.owner = '%7'
                for name in ('curs_set', 'mousemask', 'mouseinterval'):
                    stack.enter_context(patch('dashboard.curses.' + name))
                stack.enter_context(patch('dashboard.curses.getmouse',
                                          return_value=(0, 14, 16, 0, curses.BUTTON1_CLICKED)))
                stack.enter_context(patch('dashboard.defaults', return_value=config.copy()))
                stack.enter_context(patch('dashboard.load_config', return_value=config.copy()))
                stack.enter_context(patch('dashboard.initialize_colors', return_value={}))
                stack.enter_context(patch('dashboard.session_summary', return_value=self.history))
                mux = stack.enter_context(patch('dashboard.mux', return_value='0'))
                save = stack.enter_context(patch('dashboard.update_preferences'))
                watch(screen, self.args)
                save.assert_called_once_with(None, {'enabled': False}, host='claude')
                self.assertTrue(any('@claude_usage_config' in call.args
                                    for call in mux.call_args_list))
                self.assertFalse(any('@omp_usage_config' in call.args
                                     for call in mux.call_args_list))
        self.args.owner = None

    @patch('dashboard.subprocess.Popen')
    def test_claude_fetch_job_never_invokes_omp_usage_source(self, popen):
        popen.return_value.poll.return_value = 0
        job = FetchJob('anthropic', None, host='claude', owner='%7')
        try:
            command = popen.call_args.args[0]
            self.assertEqual(command[1:3], [str(ROOT / 'claude_usage_source.py'), '--owner'])
            self.assertEqual(command[-1], '%7')
            self.assertNotIn(str(ROOT / 'usage_source.py'), command)
            self.assertNotIn('omp', command)
        finally:
            job.close()

    @patch('dashboard.mux')
    def test_tmux_owner_and_config_options_are_host_isolated(self, mux):
        mux.return_value = '%2\t%1\n%3\t%9'
        self.assertEqual(owned_panes('%1', host='claude'), ['%2'])
        self.assertIn('@claude_usage_owner', mux.call_args.args[-1])
        mux.return_value = ''
        load_config('%1', dict(DEFAULTS), host='claude')
        self.assertEqual(mux.call_args.args[-1], '@claude_usage_config')

    def test_launch_tmux_failures_never_print_forwarded_prompt(self):
        from dashboard import main

        canary = 'private-launch-prompt-canary'
        synthetic = replace(host_adapters.get_host('claude'), host_id='synthetic')
        hosts = {**host_adapters._HOSTS, 'synthetic': synthetic}
        for host in ('claude', 'synthetic'):
            for failure in ('no stderr', 'stderr', 'os error', 'timeout'):
                with self.subTest(host=host, failure=failure):
                    def fail_new_session(*args):
                        self.assertEqual(args[0], 'new-session')
                        command = ['/tmux', '-L', host + '-usage', *args]
                        if failure == 'no stderr':
                            raise subprocess.CalledProcessError(1, command, stderr='')
                        if failure == 'stderr':
                            raise subprocess.CalledProcessError(1, command, stderr=canary)
                        if failure == 'os error':
                            raise OSError(f'tmux failed: {args[-1]}')
                        raise subprocess.TimeoutExpired(command, 10)

                    output, errors = io.StringIO(), io.StringIO()
                    with patch.object(host_adapters, '_HOSTS', hosts), \
                         patch('dashboard.HOST_IDS', (*host_adapters.HOST_IDS, 'synthetic')), \
                         patch.dict(os.environ, {'TMUX': ''}), \
                         patch.object(sys, 'argv', ['dashboard.py', 'launch', '--host', host,
                                                    '--', canary]), \
                         patch('dashboard.defaults', return_value={'enabled': True}), \
                         patch('dashboard.tmux_binary', return_value='/tmux'), \
                         patch('host_adapters.shutil.which', return_value='/claude'), \
                         patch('dashboard.shutil.get_terminal_size',
                               return_value=Namespace(columns=120, lines=40)), \
                         patch('dashboard.mux', side_effect=fail_new_session), \
                         redirect_stdout(output), redirect_stderr(errors):
                        self.assertEqual(main(), 1)
                    self.assertNotIn(canary, output.getvalue() + errors.getvalue())

    @patch('dashboard.subprocess.run', return_value=Namespace(returncode=0))
    @patch('dashboard.shutil.get_terminal_size', return_value=Namespace(columns=120, lines=40))
    @patch('dashboard.tmux_binary', return_value='/tmux')
    @patch('dashboard.mux')
    def test_launch_keeps_receiver_alive_and_never_adds_omp_extension(
            self, mux, _tmux, _size, attach):
        pane_states = iter(('0', '1'))
        status_path = None

        def fake_mux(*args):
            nonlocal status_path
            if args[0] == 'new-session':
                status_path = Path(shlex.split(args[-1])[4])
                return '%4'
            if args[0] == 'display-message':
                state = next(pane_states)
                if state == '1':
                    status_path.write_text('0\n')
                return state
            return ''

        mux.side_effect = fake_mux
        self.args.claude_binary = '/claude'
        self.args.on_owner_ready = MagicMock()
        with patch.dict('dashboard.os.environ', {'TMUX': '', 'CLAUDE_RECEIVER_NONCE': 'private'}), \
             patch('dashboard.control') as control, \
             patch('dashboard.time.sleep') as wait:
            self.assertEqual(launch(self.args, ['--settings', '/tmp/claude-settings.json']), 0)
        wait.assert_called_once_with(1)
        self.args.on_owner_ready.assert_called_once_with('%4')
        control.assert_called_once_with(self.args, ['init'])
        command = next(call.args[-1] for call in mux.call_args_list
                       if call.args[0] == 'new-session')
        self.assertIn('/claude', command)
        self.assertIn('/tmp/claude-settings.json', command)
        self.assertNotIn('OMP_USAGE_LAUNCHER', command)
        self.assertNotIn('extension.js', command)
        self.assertNotIn('private', command)
        self.assertTrue(any('@claude_usage_config' in call.args for call in mux.call_args_list))
        self.assertIn('-L', attach.call_args.args[0])
        self.assertIn('claude-usage-', attach.call_args.args[0][2])
        self.assertTrue(any(call.args[0] == 'display-message' and '#{pane_dead}' in call.args
                            for call in mux.call_args_list))
        self.assertTrue(any(call.args[0] == 'kill-session'
                            for call in mux.call_args_list))

    @patch('dashboard.subprocess.run', side_effect=OSError('attach failed'))
    @patch('dashboard.shutil.get_terminal_size', return_value=Namespace(columns=120, lines=40))
    @patch('dashboard.tmux_binary', return_value='/tmux')
    @patch('dashboard.mux')
    def test_failed_claude_attach_cleans_up_pane_before_receiver_closes(
            self, mux, _tmux, _size, _attach):
        mux.side_effect = lambda *args: '%8' if args[0] == 'new-session' else '0'
        self.args.claude_binary = '/claude'
        with patch.dict('dashboard.os.environ', {'TMUX': ''}), \
             patch('dashboard.control'), \
             self.assertRaisesRegex(OSError, 'attach failed'):
            launch(self.args, [])
        self.assertTrue(any(call.args[0] == 'kill-session'
                            for call in mux.call_args_list))

    def test_real_tmux_preserves_claude_exit_and_removes_sidebar_after_detach(self):
        try:
            tmux = tmux_binary()
        except ValueError:
            self.skipTest('tmux 3.3+ not installed')
        original_run = subprocess.run

        def attach_in_pty(command, **kwargs):
            if kwargs.get('check') or 'stdout' in kwargs:
                return original_run(command, **kwargs)
            master, slave = pty.openpty()
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
                process = subprocess.Popen(
                    command, stdin=slave, stdout=slave, stderr=slave,
                    start_new_session=True,
                    preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
                time.sleep(.3)
                os.write(master, b'\x02d')
                detached = process.wait(timeout=8)
                self.assertEqual(detached, 0)
                return Namespace(returncode=detached)
            finally:
                os.close(slave)
                os.close(master)
        def start_sidebar(_args, _words):
            from dashboard import mux
            mux('split-window', '-h', '-d', '-l', '34', '-t', self.args.owner, 'sleep 30')

        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                fake_claude = Path(directory) / 'fake-claude'
                fake_claude.write_text(f'#!/bin/sh\nsleep 1\nexit {exit_code}\n')
                fake_claude.chmod(0o700)
                self.args.claude_binary = str(fake_claude)
                with patch.dict(os.environ, {'TMUX': '', 'TERM': 'xterm-256color'}), \
                     patch('dashboard.subprocess.run', side_effect=attach_in_pty), \
                     patch('dashboard.control', side_effect=start_sidebar), \
                     patch('dashboard.shutil.get_terminal_size',
                           return_value=Namespace(columns=120, lines=40)):
                    self.assertEqual(launch(self.args, []), exit_code)
                    import dashboard
                    socket = dashboard.SOCKET_NAME

                server = original_run([tmux, '-L', socket, 'has-session'],
                                      capture_output=True, timeout=5)
                self.assertNotEqual(server.returncode, 0)

if __name__ == '__main__':
    unittest.main()
