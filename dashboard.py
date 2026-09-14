#!/usr/bin/env python3
"""Persistent, configurable OMP account-usage sidebar, hosted in a tmux split."""
import argparse
import curses
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sqlite3
import sys
import tempfile
import textwrap
import time
import uuid

from usage_source import ALIASES, UsageSourceError, fetch_usage
from preferences import DEFAULTS, agent_dir, load_preferences, resolve_profile, update_preferences
from session_usage import active_session, record_quota, summary as session_summary

ROOT = Path(__file__).resolve().parent
TMUX = None
OMP = shutil.which('omp') or str(Path.home() / '.local/bin/omp')
SOCKET_NAME = 'omp-usage'
NAMES = {value: key.upper() for key, value in ALIASES.items()}
CHART_GLYPHS = frozenset('▁▂▃▄▅▆▇█│└─')
COMMANDS = {
    'view': ('list', 'compact', 'details'),
    'position': ('left', 'right'),
    'providers': ('add', 'remove', 'hide', 'show'),
    'window': ('on', 'off', 'focus', 'refresh', 'interval', 'hide', 'show'),
}
HELP = ('/usage-dashboard: view list|compact|details; position left|right; '
        'providers add|remove|hide|show PROVIDER; window on|off|focus|refresh; '
        'window interval SECONDS; window hide|show PROVIDER FILTER')


def provider_id(value):
    value = ALIASES.get(value.lower(), value.lower())
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]*', value):
        raise ValueError('Use a provider ID such as codex, claude, copilot, grok, or deepseek')
    return value


def tmux_binary():
    global TMUX
    if TMUX is None:
        candidates = [shutil.which('tmux'), str(Path.home() / '.local/share/omp-usage-dashboard/vendor/usr/bin/tmux')]
        for candidate in dict.fromkeys(path for path in candidates if path):
            try:
                output = subprocess.check_output([candidate, '-V'], text=True, stderr=subprocess.DEVNULL, timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            version = re.search(r'\btmux\s+(\d+)\.(\d+)', output)
            if version and tuple(map(int, version.groups())) >= (3, 3):
                TMUX = candidate
                break
        if TMUX is None:
            raise ValueError('Install tmux 3.3 or newer before opening the sidebar.')
    return TMUX


def mux(*args):
    socket = os.environ.get('TMUX', '').rsplit(',', 2)[0]
    prefix = [tmux_binary(), '-S', socket] if socket else [tmux_binary(), '-L', SOCKET_NAME]
    return subprocess.check_output(prefix + list(args), text=True, stderr=subprocess.PIPE, timeout=10).strip()


def defaults(args):
    profile = resolve_profile(args.profile)
    config = load_preferences(profile)
    if args.providers is not None:
        config['providers'] = list(dict.fromkeys(provider_id(p.strip()) for p in args.providers.split(',') if p.strip()))
    if args.side is not None:
        config['side'] = args.side
    if args.interval is not None:
        config['interval'] = args.interval
    return {**config, 'profile': profile, 'refresh': 0}


def load_config(owner, fallback):
    raw = mux('show-options', '-p', '-v', '-q', '-t', owner, '@omp_usage_config')
    return json.loads(raw) if raw else fallback


def owned_panes(owner):
    rows = mux('list-panes', '-a', '-F', '#{pane_id}\t#{@omp_usage_owner}').splitlines()
    return [row.split('\t')[0] for row in rows if row.endswith('\t' + owner)]


def change_config(config, words):
    if words == ['init']:
        return config
    if len(words) < 2 or words[0] not in COMMANDS or words[1] not in COMMANDS[words[0]]:
        raise ValueError(HELP)
    section, action, *params = words
    window_filter = section == 'window' and action in ('hide', 'show')
    count = 2 if window_filter else 1 if section == 'providers' or action == 'interval' else 0
    if len(params) != count:
        raise ValueError(HELP)
    if section == 'providers':
        provider = provider_id(params[0])
        if action in ('add', 'show'):
            if provider not in config['providers']:
                config['providers'].append(provider)
            config['hidden'] = [p for p in config['hidden'] if p != provider]
            config['enabled'] = True
        elif action == 'hide':
            if provider not in config['providers']:
                raise ValueError('Provider is not in the dashboard: ' + provider)
            if provider not in config['hidden']:
                config['hidden'].append(provider)
        else:
            config['providers'] = [p for p in config['providers'] if p != provider]
            config['hidden'] = [p for p in config['hidden'] if p != provider]
            config['windows'].pop(provider, None)
    elif window_filter:
        provider = provider_id(params[0])
        if provider not in config['providers']:
            raise ValueError('Provider is not in the dashboard: ' + provider)
        pattern = params[1].strip().lower()
        if not pattern:
            raise ValueError('Window filter cannot be empty')
        patterns = config['windows'].setdefault(provider, [])
        if action == 'hide' and pattern not in patterns:
            patterns.append(pattern)
        elif action == 'show':
            config['windows'][provider] = [p for p in patterns if p != pattern]
    elif action in ('left', 'right'):
        config['side'] = action
    elif action in ('compact', 'details'):
        config['compact'] = action == 'compact'
    elif action in ('on', 'off'):
        config['enabled'] = action == 'on'
    elif action == 'interval':
        interval = int(params[0])
        if interval < 15:
            raise ValueError('Polling interval must be at least 15 seconds')
        config['interval'] = interval
    elif action == 'refresh':
        config['refresh'] = time.time_ns()
    elif action not in ('list', 'focus'):
        raise ValueError(HELP)
    return config


def describe(config):
    lines = [f"Dashboard {'on' if config['enabled'] else 'off'} / {config['side']} / "
             f"{'compact' if config['compact'] else 'details'} / {config['interval']}s"]
    if not config['providers']:
        lines.extend(['Add at least one provider.', '/usage-dashboard providers add PROVIDER'])
    for provider in config['providers']:
        lines.append(provider + (' [hidden]' if provider in config['hidden'] else ' [visible]'))
        for pattern in config['windows'].get(provider, []):
            lines.append('  hidden windows matching: ' + pattern)
    return '\n'.join(lines)


def control(args, words):
    owner = args.owner or os.environ.get('TMUX_PANE')
    if owner and not re.fullmatch(r'%\d+', owner):
        raise ValueError('Invalid OMP pane identity.')
    if words == ['detach']:
        if owner:
            for pane in owned_panes(owner):
                mux('kill-pane', '-t', pane)
            mux('set-option', '-p', '-u', '-t', owner, '@omp_usage_config')
        return
    if not owner:
        old = defaults(args)
        config = change_config(json.loads(json.dumps(old)), words)
        changes = {key: config[key] for key in DEFAULTS if config[key] != old[key]}
        if words in (['window', 'on'], ['window', 'off']):
            changes['enabled'] = config['enabled']
        if changes:
            update_preferences(config['profile'], changes)
        print(describe(config))
        if config['enabled'] and words != ['view', 'list']:
            print('Saved. Run omp through the installed shell integration to attach the sidebar.')
        return
    old = load_config(owner, defaults(args))
    config = change_config(json.loads(json.dumps(old)), words)
    if words == ['view', 'list']:
        print(describe(config))
        return
    if words == ['window', 'focus'] and not config['enabled']:
        raise ValueError('Dashboard is off; use /usage-dashboard window on first.')
    panes = owned_panes(owner)
    recreate = config['enabled'] and (not panes or old['side'] != config['side'])
    if recreate:
        width = int(mux('display-message', '-p', '-t', owner, '#{pane_width}'))
        width += sum(int(mux('display-message', '-p', '-t', p, '#{pane_width}')) + 1 for p in panes)
        if width < 96:
            raise ValueError('Sidebar needs at least 96 columns. Widen the terminal and try again.')
    changes = {key: config[key] for key in DEFAULTS if config[key] != old[key]}
    if words in (['window', 'on'], ['window', 'off']):
        changes['enabled'] = config['enabled']
    if changes:
        update_preferences(config['profile'], changes)
    mux('set-option', '-p', '-t', owner, '@omp_usage_config', json.dumps(config))
    if not config['enabled'] or recreate:
        for pane in panes:
            mux('kill-pane', '-t', pane)
    if recreate:
        mux('set-option', '-t', owner, 'mouse', 'on')
        # Equal window-local styles remove tmux's half-colored focus indicator.
        for option in ('pane-border-style', 'pane-active-border-style'):
            mux('set-option', '-w', '-t', owner, option, 'fg=colour240,bg=default')
        mux('set-option', '-w', '-t', owner, 'pane-border-indicators', 'off')
        command = [sys.executable, str(ROOT / 'dashboard.py'), 'watch', '--owner', owner]
        options = ['split-window', '-h', '-d', '-l', '34', '-t', owner, '-P', '-F', '#{pane_id}']
        if config['side'] == 'left':
            options.append('-b')
        pane = mux(*options, shlex.join(command))
        mux('set-option', '-p', '-t', pane, '@omp_usage_owner', owner)
        mux('select-pane', '-t', pane, '-T', 'OMP usage')
        panes = [pane]
    if words == ['window', 'focus'] and panes:
        mux('select-pane', '-t', panes[0])
    print(describe(config))


def clean(value):
    # Keep only single-cell chart glyphs in addition to printable ASCII.
    return ''.join(char if ' ' <= char <= '~' or char in CHART_GLYPHS else '?'
                   for char in str(value) if ord(char) >= 32 and ord(char) != 127)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fraction(amount):
    value = amount.get('usedFraction')
    if number(value):
        return value
    used, total = amount.get('used'), amount.get('limit')
    if number(used) and number(total) and total > 0:
        return used / total
    if amount.get('unit') == 'percent' and number(used):
        return used / 100
    if number(amount.get('remainingFraction')):
        return max(0, 1 - amount['remainingFraction'])
    return None


def remaining_fraction(amount):
    remaining = amount.get('remainingFraction')
    if not number(remaining):
        balance, total = amount.get('remaining'), amount.get('limit')
        if number(balance) and number(total) and total > 0:
            remaining = balance / total
        elif amount.get('unit') == 'percent' and number(balance):
            remaining = balance / 100
        else:
            used = fraction(amount)
            remaining = 1 - used if used is not None else None
    return min(1, max(0, remaining)) if number(remaining) else None


def countdown(window, now):
    reset = window.get('resetsAt')
    if not number(reset):
        return ''
    seconds = max(0, int(reset / 1000 - now))
    if not seconds:
        return 'reset due; awaiting provider'
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    duration = f'{days}d {hours}h' if days else f'{hours}h {minutes}m' if hours else f'{minutes}m {seconds}s'
    return clean(window.get('resetLabel', 'reset')) + ' ' + duration


def allowance_color(remaining, status=None):
    if status == 'exhausted' or (remaining is not None and remaining <= .20):
        return 'error'
    if status in ('warning', 'unknown') or (remaining is not None and remaining <= .40):
        return 'warn'
    return 'good' if remaining is not None else 'normal'


def allowance_row(label, value_text, reset, width, color):
    units = {'hour': 'h', 'day': 'd', 'minute': 'm', 'week': 'w'}
    label = re.sub(r'\b(\d+)\s+(hour|day|minute|week)s?\b',
                   lambda match: match[1] + units[match[2].lower()], label, flags=re.IGNORECASE)
    right = value_text + (' | ' + reset if reset else '')
    room = max(1, width - len(right) - 1)
    if len(label) > room:
        label = label[:max(0, room - 3)] + '.' * min(3, room)
    left = label.ljust(room)
    text = left + ' ' + right
    percentage = re.search(r'\d+%', value_text)
    if percentage:
        start = len(left) + 1 + percentage.start()
        return text, ('normal', start, start + len(percentage[0]), color)
    return text, 'normal'


def provider_lines(data, provider, config, now, width):
    lines = []
    reports = [r for r in data.get('reports', []) if r.get('provider') == provider]
    if not reports:
        note = data.get('dashboardNote') or 'Check OMP login/support'
        if config['compact']:
            note = 'Check login / usage support'
        return [('Usage unavailable', 'warn'), (clean(note), 'dim')]
    for index, report in enumerate(reports):
        label = f'Account {index + 1}' if len(reports) > 1 else ''
        fetched = report.get('fetchedAt')
        age = max(0, int(now - fetched / 1000)) if number(fetched) else None
        if label:
            lines.append((label, 'title'))
        if age is not None and (age > config['interval'] + 5 or not config['compact']):
            cached = ' (cached)' if age > config['interval'] + 5 else ''
            lines.append((f'Source age: {age}s{cached}', 'dim'))
        if report.get('metadata', {}).get('limitReached'):
            lines.append(('ACCOUNT LIMIT REACHED', 'error'))
        if report.get('metadata', {}).get('isAvailable') is False:
            lines.append(('INSUFFICIENT BALANCE', 'error'))
        for note in report.get('notes', []):
            lines.append((clean(note), 'warn'))
        shown = 0
        for limit in report.get('limits', []):
            haystack = str(limit.get('id', '')) + ' ' + str(limit.get('label', ''))
            if any(pattern in haystack.lower() for pattern in config['windows'].get(provider, [])):
                continue
            shown += 1
            amount = limit.get('amount') or {}
            value = remaining_fraction(amount)
            label = clean(limit.get('label', limit.get('id', 'Usage')))
            status = limit.get('status')
            color = allowance_color(value, status)
            reset = countdown(limit.get('window') or {}, now)
            compact_reset = reset.removeprefix('reset ')
            inline_reset = compact_reset if len(compact_reset) <= 12 and width >= 26 else ''
            if value is not None:
                lines.append(allowance_row(label, f'{value:>4.0%} left', inline_reset, width, color))
                if inline_reset:
                    reset = ''
            else:
                remaining = amount.get('remaining')
                used = amount.get('used')
                unit = clean(amount.get('unit', ''))
                if unit == 'unknown':
                    unit = ''
                detail = f'{remaining:g} {unit} left' if number(remaining) else f'{used:g} {unit} used' if number(used) else 'Amount unavailable'
                lines.append(allowance_row(label, detail, '', width, color))
            if reset:
                lines.append((reset, 'dim'))
            if status and status != 'ok':
                lines.append((clean(status), 'warn' if status != 'exhausted' else 'error'))
            for note in limit.get('notes', []):
                lines.append((clean(note), 'dim'))
            if not config['compact']:
                used = fraction(amount)
                if used is not None:
                    lines.append((f'{used:.0%} used', 'dim'))
                lines.append(('ID: ' + clean(limit.get('id', '')), 'dim'))
                lines.append(('', ''))
        if not shown:
            lines.append(('All windows hidden' if report.get('limits') else 'No usage windows reported', 'dim'))
        credits = report.get('resetCredits', {}).get('availableCount')
        if number(credits):
            lines.append((f'Reset credits: {credits:g}', 'dim'))
    return lines


def quota_samples(data):
    samples = []
    reports = data.get('reports', [])
    for report in reports:
        provider = report.get('provider')
        fetched = report.get('fetchedAt')
        if not number(fetched):
            continue
        for limit in report.get('limits', []):
            used = fraction(limit.get('amount') or {})
            scope = limit.get('scope') or {}
            # Unidentified reports cannot safely be paired across account reordering.
            if used is None or not any(scope.get(key) for key in ('accountId', 'projectId', 'orgId')):
                continue
            window = limit.get('window') or {}
            identity_scope = dict(scope)
            if scope.get('shared'):
                # A shared bucket is not a new allowance for each model using it.
                identity_scope.pop('modelId', None)
            samples.append({
                'identity': [provider, identity_scope, limit.get('id')],
                'provider': provider, 'label': clean(limit.get('label', limit.get('id', 'Quota'))),
                'at': fetched / 1000, 'used': used,
                'reset': [window.get('id'), window.get('resetsAt'), (limit.get('amount') or {}).get('limit')],
            })
    return samples


def section_heading(label, width, tail=''):
    label, tail = clean(label), clean(tail)
    rule = '-' * max(1, width - len(label) - len(tail) - (2 if tail else 1))
    text = label + ' ' + rule + (' ' + tail if tail else '')
    return text[:max(1, width)], ('dim', 0, len(label), 'title')


def token_chart(values, width):
    peak = max(values, default=0)
    if not peak:
        return [(f'No activity in the last {len(values)}m', 'dim')]
    # Four rows with eighth-cell precision, rather than three coarse '#' levels.
    try:
        ''.join(CHART_GLYPHS).encode(sys.stdout.encoding or 'ascii')
        blocks, vertical, corner, horizontal = ' ▁▂▃▄▅▆▇█', '│', '└', '─'
    except UnicodeEncodeError:
        blocks, vertical, corner, horizontal = ' .:-=+*O@', '|', '+', '-'
    scale = f'{peak / 1_000_000:.1f}m' if peak >= 1_000_000 else f'{peak / 1_000:.0f}k' if peak >= 1_000 else str(peak)
    axis = max(4, len(scale))
    columns = max(1, width - axis - 2)
    # Stretch across the available width; max-pool only when narrower than the history.
    bins = [max(values[i * len(values) // columns:max(i * len(values) // columns + 1,
                (i + 1) * len(values) // columns)], default=0) for i in range(columns)]
    heights = [math.ceil(value * 32 / peak) if value else 0 for value in bins]
    rows = []
    for row in range(4):
        label = scale if row == 0 else ''
        bars = ''.join(blocks[min(8, max(0, height - (3 - row) * 8))] for height in heights)
        text = label.rjust(axis) + ' ' + vertical + bars
        rows.append((text, ('dim', axis + 2, len(text), 'title')))
    rows.append(('0'.rjust(axis) + ' ' + corner + horizontal * columns, ('dim', 0, 0, 'dim')))
    labels = f'-{len(values)}m'.ljust(max(0, columns - 3)) + 'now'
    rows.append((' ' * (axis + 2) + labels, ('dim', 0, 0, 'dim')))
    return rows


def session_lines(history, now, width, compact=True):
    rows = []
    current = history['current']
    if current:
        rows.append(section_heading('TOKEN RATE', width, 'tok/min'))
        rows.extend(token_chart(history['chart'], width))
        if not compact:
            rows.append(('All models; reported usage', 'dim'))
        rows.append(('', 'dim'))
    elif not history['previous']:
        rows.extend([('Waiting for OMP session', 'dim'), ('', 'dim')])
    for name, title in (('current', 'CURRENT SESSION'), ('previous', 'PREVIOUS SESSION')):
        session = history[name]
        if not session:
            continue
        rows.append(section_heading(title, width, session['id'][:8] if not compact else ''))
        providers = session['providers']
        total = sum(item['total'] for item in providers)
        single = compact and len(providers) == 1
        label = NAMES.get(providers[0]['provider'], providers[0]['provider']) + ' tokens' if single else 'Tokens'
        rows.append(allowance_row(label, f'{total:,}', '', width, 'normal'))
        if not compact or name == 'previous':
            stamp = time.strftime('%b %d %H:%M', time.localtime(session['updated']))
            rows.append((f'Last recorded {stamp}', 'dim'))
        for item in providers:
            if single:
                continue
            label = NAMES.get(item['provider'], item['provider'])
            if not compact:
                rows.append(('', 'dim'))
            rows.append(allowance_row(label, f'{item["total"]:,}', '', width, 'normal'))
            if not compact:
                for model in session['models']:
                    if model['provider'] == item['provider']:
                        rows.append((f'{model["model"]}: {model["total"]:,}', 'normal'))
                        rows.append((f'  In {model["input"]:,} / out {model["output"]:,}', 'dim'))
                        rows.append((f'  Cache r {model["cache_read"]:,} / w {model["cache_write"]:,}', 'dim'))
        quotas = [quota for quota in session['quota']
                  if quota['intervals'] > 0 and round(quota['points'], 2) > 0]
        if quotas:
            rows.extend([('', 'dim'), ('Quota change (observed)', 'dim')])
        for quota in quotas:
            label = NAMES.get(quota['provider'], quota['provider']) + ' ' + quota['label']
            duplicates = sum(item['provider'] == quota['provider'] and item['label'] == quota['label']
                             for item in quotas)
            if duplicates > 1 or not compact:
                # Put the fingerprint first so width fitting cannot erase its identity.
                label = '[' + quota['key'][:6] + '] ' + label
            rows.append(allowance_row(label, f'+{quota["points"]:.2f} pp', '', width, 'normal'))
            if not compact:
                if quota['segments'] > 1:
                    rows.append((f'{quota["segments"]} observation segments', 'dim'))
                rows.append((f'Last sample {max(0, int(now - quota["last"]))}s ago', 'dim'))
        if quotas and not compact:
            rows.extend([('pp = percentage points', 'dim'), ('Account-wide; not exact billing', 'dim')])
        rows.append(('', 'dim'))
    recorded = sum(item['total'] for item in history['history'])
    current_total = sum(item['total'] for item in current['providers']) if current else 0
    if history['history'] and (not compact or not current or recorded != current_total):
        rows.append(section_heading('HISTORY', width))
        rows.append(allowance_row('All sessions', f'{recorded:,}', '', width, 'normal'))
        if not compact:
            rows.extend((f'{NAMES.get(item["provider"], item["provider"])} / {item["model"]}: {item["total"]:,}', 'dim')
                        for item in history['history'])
        rows.append(('', 'dim'))
    return rows


class FetchJob:
    def __init__(self, provider, profile):
        self.output = tempfile.TemporaryFile(mode='w+t')
        self.error = tempfile.TemporaryFile(mode='w+t')
        command = [sys.executable, str(ROOT / 'usage_source.py'), '--provider', provider]
        if profile is not None:
            command += ['--profile', profile]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=self.output,
                                            stderr=self.error, start_new_session=True)
        except BaseException:
            self.output.close()
            self.error.close()
            raise
        self.started = time.monotonic()

    def signal_group(self, signum):
        try:
            os.killpg(self.process.pid, signum)
        except ProcessLookupError:
            pass

    def finish(self):
        timed_out = self.process.poll() is None and time.monotonic() - self.started >= 65
        if timed_out:
            self.signal_group(signal.SIGKILL)
            self.process.wait()
        if self.process.poll() is None:
            return None
        if timed_out or self.process.returncode:
            return (None, 'Refresh timed out' if timed_out else 'Refresh failed; check login/network')
        try:
            self.output.seek(0)
            data = json.load(self.output)
            if not isinstance(data, dict) or not isinstance(data.get('reports'), list):
                raise ValueError('Unexpected usage response')
            return (data, None)
        except (ValueError, TypeError):
            return (None, 'Invalid usage response')

    def close(self):
        if self.process.poll() is None:
            self.signal_group(signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.signal_group(signal.SIGKILL)
                self.process.wait()
        self.output.close()
        self.error.close()


def stop_watch(_signum, _frame):
    raise SystemExit(0)


def watch(screen, args):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(200)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    colors = {'dim': curses.A_DIM, 'normal': curses.A_NORMAL}
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for i, (name, color) in enumerate([('title', curses.COLOR_CYAN), ('good', curses.COLOR_GREEN),
                                          ('warn', curses.COLOR_YELLOW), ('error', curses.COLOR_RED)], 1):
            curses.init_pair(i, color, -1)
            colors[name] = curses.color_pair(i)
    config = defaults(args)
    states, jobs = {}, {}
    history_error = None
    offset, next_config, next_frame = 0, 0, 0
    try:
        while True:
            now, tick = time.time(), time.monotonic()
            if args.owner and tick >= next_config:
                try:
                    if mux('display-message', '-p', '-t', args.owner, '#{pane_dead}') != '0':
                        return
                    updated = load_config(args.owner, config)
                except subprocess.CalledProcessError:
                    return
                if updated['refresh'] != config['refresh']:
                    for state in states.values():
                        state['next'] = 0
                if updated['interval'] != config['interval']:
                    for state in states.values():
                        state['next'] = min(state['next'], tick + updated['interval'])
                config = updated
                next_config = tick + 1
            visible = [p for p in config['providers'] if p not in config['hidden']]
            for provider in list(jobs):
                job = jobs[provider]
                result = job.finish() if provider in visible else (None, None)
                if result is not None:
                    data, error = result
                    state = states[provider]
                    if data is not None:
                        if not data['reports'] and state['data'] and state['data']['reports']:
                            error = 'Provider returned no usage'
                        else:
                            state['data'] = data
                            try:
                                record_quota(job.history_profile, args.owner, job.activation, quota_samples(data))
                                history_error = None
                            except (OSError, ValueError, sqlite3.Error):
                                history_error = 'Could not save quota history'
                    state['error'] = error
                    state['checked'] = tick
                    state['next'] = tick + config['interval'] if error else max(tick, job.started + config['interval'])
                    job.close()
                    del jobs[provider]
            for provider in visible:
                state = states.setdefault(provider, {'data': None, 'error': None, 'next': 0, 'checked': None})
                if len(jobs) < 2 and provider not in jobs and tick >= state['next']:
                    try:
                        active = active_session(config['profile'], args.owner)
                    except (OSError, sqlite3.Error):
                        active = None
                        history_error = 'Session history unavailable'
                    jobs[provider] = FetchJob(provider, config['profile'])
                    jobs[provider].history_profile = config['profile']
                    jobs[provider].activation = active['activation'] if active else None
            if tick >= next_frame:
                height, width = screen.getmaxyx()
                screen.erase()
                try:
                    rows = session_lines(session_summary(config['profile'], args.owner, now),
                                         now, width - 2, config['compact'])
                except (OSError, sqlite3.Error):
                    rows = [('Session history unavailable', 'warn')]
                if history_error:
                    rows.append((history_error, 'warn'))
                for provider in visible:
                    state = states[provider]
                    name = NAMES.get(provider, provider.upper())
                    tail = ''
                    if config['compact'] and state['checked'] is not None:
                        tail = 'checking' if provider in jobs else f'checked {max(0, int(tick - state["checked"]))}s'
                    rows.append(section_heading(name, width - 2, tail))
                    if state['checked'] is not None and not config['compact']:
                        age = max(0, int(tick - state['checked']))
                        next_check = max(0, int(state['next'] - tick))
                        progress = 'checking...' if provider in jobs else f'next {next_check}s'
                        rows.append((f'Checked {age}s ago | {progress}', 'dim'))
                    if state['error']:
                        rows.append(('STALE DATA' if state['data'] else 'USAGE UNAVAILABLE', 'error'))
                        rows.append((state['error'], 'warn'))
                    if state['data']:
                        rows.extend(provider_lines(state['data'], provider, config, now, width - 2))
                    elif not state['error']:
                        rows.append(('Fetching account usage...', 'dim'))
                    rows.append(('', 'dim'))
                if not visible:
                    rows += [('No visible providers' if config['providers'] else 'Add at least one provider.', 'dim'),
                            ('/usage-dashboard providers', 'dim'),
                            ('Choose Add to select a provider.', 'dim')]
                if width < 22:
                    rows = [('Widen terminal', 'warn')]
                # Wrap long labels/notes; never let curses write outside this pane.
                wrapped = []
                for text, style in rows:
                    if isinstance(style, tuple):
                        # Dense rows are width-fitted so highlight offsets stay exact.
                        wrapped.append((clean(text)[:max(1, width - 2)], style))
                    else:
                        wrapped.extend((part, style) for part in (textwrap.wrap(clean(text), max(1, width - 2)) or ['']))
                body_height = max(0, height - 4)
                offset = min(offset, max(0, len(wrapped) - body_height))
                draw = wrapped[offset:offset + body_height]
                draw += [('', '')] * max(0, body_height - len(draw))
                count = len(visible)
                position = f' | {offset + 1}-{min(len(wrapped), offset + body_height)}/{len(wrapped)}' if len(wrapped) > body_height else ''
                draw += [(f'{count} provider{"s" if count != 1 else ""} | every {config["interval"]}s{position}', 'dim'),
                         ('[Refresh] [Hide]', 'normal'),
                         ('Click | r/q | wheel/arrows', 'dim')]
                for row, (text, style) in enumerate(draw[:max(0, height - 1)]):
                    if width > 2:
                        base = style[0] if isinstance(style, tuple) else style
                        screen.addnstr(row, 1, clean(text), width - 2, colors.get(base, 0))
                        if isinstance(style, tuple):
                            _base, start, end, color = style
                            end = min(end, width - 2, len(text))
                            if start < end:
                                screen.addnstr(row, 1 + start, clean(text[start:end]), end - start, colors.get(color, 0))
                screen.refresh()
                next_frame = tick + 1
            key = screen.getch()
            if key == curses.KEY_MOUSE:
                try:
                    _id, mouse_x, mouse_y, _z, buttons = curses.getmouse()
                except curses.error:
                    continue
                if buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED) and mouse_y == screen.getmaxyx()[0] - 3:
                    if 1 <= mouse_x < 10:
                        key = ord('r')
                    elif 11 <= mouse_x < 17:
                        key = ord('q')
                elif buttons & curses.BUTTON4_PRESSED:
                    offset = max(0, offset - 3)
                elif buttons & getattr(curses, 'BUTTON5_PRESSED', 0):
                    offset += 3
            if key == ord('q'):
                update_preferences(config['profile'], {'enabled': False})
                if args.owner:
                    config['enabled'] = False
                    mux('set-option', '-p', '-t', args.owner, '@omp_usage_config', json.dumps(config))
                return
            if key == ord('r'):
                for state in states.values():
                    state['next'] = 0
            if key in (curses.KEY_DOWN, ord('j')):
                offset += 1
            if key in (curses.KEY_UP, ord('k')):
                offset = max(0, offset - 1)
            if key == curses.KEY_NPAGE:
                offset += max(1, screen.getmaxyx()[0] - 3)
            if key == curses.KEY_PPAGE:
                offset = max(0, offset - max(1, screen.getmaxyx()[0] - 3))
            if key != -1:
                next_frame = 0
    finally:
        for job in jobs.values():
            job.close()


def extension_path(profile):
    installed = agent_dir(profile) / 'extensions/usage-dashboard.js'
    return installed if installed.is_symlink() and installed.resolve() == ROOT / 'extension.js' else ROOT / 'extension.js'


def launch(args, omp_args):
    if os.environ.get('TMUX'):
        raise ValueError('Run omp directly in this tmux pane; its extension attaches the dashboard.')
    size = shutil.get_terminal_size()
    session = 'usage-' + uuid.uuid4().hex[:8]
    global SOCKET_NAME
    SOCKET_NAME = 'omp-' + session
    native = getattr(args, 'native', False)
    config = None if native else defaults(args)
    extensions = [] if native else ['-e', str(extension_path(args.profile))]
    command = ['env', f'OMP_USAGE_LAUNCHER={0 if native else 1}', OMP, *extensions, *omp_args]
    if args.profile is not None:
        command += ['--profile', args.profile]
    # A fresh server inherits credentials without embedding them in pane command strings.
    owner = mux('new-session', '-d', '-s', session, '-x', str(size.columns), '-y', str(size.lines),
                '-c', os.getcwd(), '-P', '-F', '#{pane_id}', shlex.join(command))
    try:
        mux('set-option', '-t', session, 'status', 'off')
        mux('set-option', '-t', session, 'allow-passthrough', 'on')
        args.owner = owner
        # Native discovery owns scope selection and initialization, including project overrides.
        if not native:
            # Keep tmux available when off or narrow so window on can work later.
            if config['enabled'] and size.columns < 96:
                config['enabled'] = False
            mux('set-option', '-p', '-t', owner, '@omp_usage_config', json.dumps(config))
            control(args, ['init'])
        mux('select-pane', '-t', owner)
    except BaseException:
        mux('kill-session', '-t', session)
        raise
    binary = tmux_binary()
    os.execv(binary, [binary, '-L', SOCKET_NAME, 'attach-session', '-t', session])


def main():
    parser = argparse.ArgumentParser(description='Internal dashboard runtime; start OMP through the installed shell integration.')
    parser.add_argument('action', nargs='?', default='launch', choices=['launch', 'watch', 'control'])
    parser.add_argument('--providers', help='Comma-separated provider IDs or aliases')
    parser.add_argument('--side', choices=['left', 'right'])
    parser.add_argument('--interval', type=int, help='Polling interval, minimum 15 seconds')
    parser.add_argument('--profile', default=os.environ.get('OMP_PROFILE', os.environ.get('PI_PROFILE')), help='OMP profile; put before --')
    parser.add_argument('--owner', help=argparse.SUPPRESS)
    parser.add_argument('--once', action='store_true', help='Print live usage without curses (watch only)')
    argv = sys.argv[1:]
    split = argv.index('--') if '--' in argv else len(argv)
    args = parser.parse_args(argv[:split])
    extra = argv[split + 1:]
    if args.interval is not None and args.interval < 15:
        parser.error('--interval must be at least 15 seconds')
    try:
        config = defaults(args)
        if args.action == 'launch':
            if any(arg == '--profile' or arg.startswith('--profile=') for arg in extra):
                parser.error('Put --profile before -- so usage polling uses the same profile')
            launch(args, extra)
        elif args.action == 'control':
            control(args, extra)
        elif extra:
            parser.error('Unexpected arguments after --')
        elif args.once:
            print('\n'.join(text for text, _ in session_lines(
                session_summary(config['profile'], args.owner), time.time(), 32, config['compact'])))
            if not config['providers']:
                print(describe(config))
            for provider in config['providers']:
                print(NAMES.get(provider, provider.upper()))
                print('\n'.join(text for text, _ in provider_lines(fetch_usage(provider, args.profile), provider, config, time.time(), 32)))
                print()
        else:
            # Exported COLUMNS/LINES can be the parent terminal's size, not this split's size.
            os.environ.pop('COLUMNS', None)
            os.environ.pop('LINES', None)
            signal.signal(signal.SIGHUP, stop_watch)
            signal.signal(signal.SIGTERM, stop_watch)
            curses.wrapper(watch, args)
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError, curses.error, UsageSourceError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and isinstance(exc.stderr, str) else str(exc)
        print('omp-dashboard: ' + clean(detail), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
