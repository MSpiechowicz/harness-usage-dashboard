"""Host-independent dashboard commands, charts and terminal-safe presentation."""
import math
import re
import sys
import time

from host_adapters import get_host
from usage_source import ALIASES
from preferences import CHART_TYPES, THEME_NAMES, TOKEN_NAMES, normalize_color

NAMES = {**{value: key.upper() for key, value in ALIASES.items()}, 'anthropic': 'CLAUDE'}
BAR_GLYPHS = '▁▂▃▄▅▆▇█│└─┌┐┘'
CHART_GLYPHS = frozenset(BAR_GLYPHS + '●')
TRACE_GLYPHS = '│└─' + ''.join(chr(code) for code in range(0x2800, 0x2900))
THEME_TOKENS = {
    'green': {'text': 'white', 'muted': 'default', 'secondary': '#008f4c',
              'accent': 'green', 'chart': 'green', 'good': 'green', 'warn': 'orange',
              'error': 'red'},
    'blue': {'text': 'white', 'muted': 'default', 'secondary': '#007fae',
             'accent': '#00b4ff', 'chart': '#00b4ff', 'good': 'green', 'warn': 'orange',
             'error': 'red'},
    'brown': {'text': 'white', 'muted': 'default', 'secondary': '#744c24',
              'accent': 'brown', 'chart': 'brown', 'good': 'green', 'warn': 'orange',
              'error': 'red'},
    'yellow': {'text': 'white', 'muted': 'default', 'secondary': '#8a7600',
               'accent': 'yellow', 'chart': 'yellow', 'good': 'green', 'warn': 'orange',
               'error': 'red'},
    'cyan': {'text': 'white', 'muted': 'default', 'secondary': '#007c83',
             'accent': 'cyan', 'chart': 'cyan', 'good': 'green', 'warn': 'orange',
             'error': 'red'},
    'magenta': {'text': 'white', 'muted': 'default', 'secondary': '#8a3f8f',
                'accent': 'magenta', 'chart': 'magenta', 'good': 'green', 'warn': 'orange',
                'error': 'red'},
    'orange': {'text': 'white', 'muted': 'default', 'secondary': '#a65300',
               'accent': 'orange', 'chart': 'orange', 'good': 'green', 'warn': 'orange',
               'error': 'red'},
    'red': {'text': 'white', 'muted': 'default', 'secondary': '#9e2f3f',
            'accent': 'red', 'chart': 'red', 'good': 'green', 'warn': 'orange',
            'error': 'red'},
    # Anthropic brand-inspired palette; not a claim about Claude Code's terminal defaults.
    'claude': {'text': '#faf9f5', 'muted': '#b0aea5', 'secondary': '#b0aea5',
               'accent': '#d97757', 'chart': '#d97757', 'good': 'green',
               'warn': 'orange', 'error': 'red'},
}
COMMANDS = {
    'view': ('list', 'compact', 'details'),
    'commands': ('hide', 'show'),
    'chart': CHART_TYPES,
    'previous': ('hide', 'show'),
    'history-other': ('hide', 'show'),
    'history-total': ('hide', 'show'),
    'position': ('left', 'right'),
    'providers': ('add', 'remove', 'hide', 'show'),
    'theme': (*THEME_NAMES, 'custom', 'reset'),
    'window': ('on', 'off', 'focus', 'refresh', 'interval', 'hide', 'show'),
}
THEME_OPTIONS = '|'.join(THEME_NAMES)
CHART_OPTIONS = '|'.join(CHART_TYPES)
HELP = (f'/usage-dashboard: view list|compact|details; chart {CHART_OPTIONS}; '
        f'position left|right; providers add|remove|hide|show PROVIDER; '
        f'theme {THEME_OPTIONS} (claude: Anthropic brand-inspired); '
        'theme custom TOKEN COLOR; theme reset; window on|off|focus|refresh; '
        'window interval SECONDS; window hide|show PROVIDER FILTER; commands hide|show; '
        'previous hide|show; history-other hide|show; history-total hide|show')
NATIVE_HELP = (f'/usage-dashboard: view list|compact|details; chart {CHART_OPTIONS}; '
               'providers add|remove|hide|show anthropic; '
               f'theme {THEME_OPTIONS}; theme custom TOKEN COLOR; theme reset; '
               'window on|off|focus|refresh; window hide|show anthropic FILTER; '
               'commands hide|show; previous hide|show; '
               'history-other hide|show; history-total hide|show')


def resolve_tokens(config):
    theme = config.get('theme', 'green')
    tokens = dict(THEME_TOKENS.get(theme, THEME_TOKENS['green']))
    tokens.update(config.get('tokens') or {})
    return tokens
def _token_name(value):
    value = value.strip().lower()
    if value not in TOKEN_NAMES:
        raise ValueError('Token must be one of: ' + ', '.join(TOKEN_NAMES))
    return value


def _theme_config(config, action, params):
    if action in THEME_NAMES:
        config['theme'] = action
    elif action == 'custom':
        token = _token_name(params[0])
        color = normalize_color(params[1])
        config.setdefault('tokens', {})[token] = color
    elif action == 'reset':
        config['theme'] = 'green'
        config['tokens'] = {}

def provider_id(value, host='omp'):
    return get_host(host).provider_id(value)
def change_config(config, words, host='omp'):
    if words == ['init']:
        return config
    if len(words) < 2 or words[0] not in COMMANDS or words[1] not in COMMANDS[words[0]]:
        raise ValueError(HELP)
    section, action, *params = words
    window_filter = section == 'window' and action in ('hide', 'show')
    count = (2 if window_filter else 2 if section == 'theme' and action == 'custom'
             else 1 if section == 'providers' or action == 'interval' else 0)
    if len(params) != count:
        raise ValueError(HELP)
    if section == 'providers':
        provider = provider_id(params[0], host)
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
        provider = provider_id(params[0], host)
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
    elif section in ('commands', 'previous', 'history-other', 'history-total'):
        config[f'{section.replace("-", "_")}_visible'] = action == 'show'
    elif section == 'theme':
        _theme_config(config, action, params)
    elif section == 'chart':
        config['chart_type'] = action
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


def describe(config, *, native=False):
    theme = config.get('theme', 'green')
    custom = config.get('tokens') or {}
    token_note = (' / custom ' + ', '.join(f'{name}={value}' for name, value in custom.items())
                  if custom else '')
    layout = '' if native else f" / {config['side']}"
    interval = '' if native else f" / {config['interval']}s"
    lines = [f"Dashboard {'on' if config['enabled'] else 'off'}{layout} / "
             f"{'compact' if config['compact'] else 'details'}{interval} / "
             f"theme {theme}{token_note} / chart {config['chart_type']} / "
             f"commands {'shown' if config['commands_visible'] else 'hidden'} / "
             f"previous {'shown' if config['previous_visible'] else 'hidden'} / "
             f"history other sessions {'shown' if config['history_other_visible'] else 'hidden'} / "
             f"history total {'shown' if config['history_total_visible'] else 'hidden'}"]
    if not config['providers']:
        lines.extend(['Add at least one provider.', '/usage-dashboard providers add PROVIDER'])
    for provider in config['providers']:
        lines.append(provider + (' [hidden]' if provider in config['hidden'] else ' [visible]'))
        for pattern in config['windows'].get(provider, []):
            lines.append('  hidden windows matching: ' + pattern)
    return '\n'.join(lines)
def clean(value):
    # Braille is a single-cell chart glyph; other non-ASCII stays restricted.
    return ''.join(char if ' ' <= char <= '~' or char in CHART_GLYPHS
                   or 0x2800 <= ord(char) <= 0x28ff else '?'
                   for char in str(value) if ord(char) >= 32 and ord(char) != 127)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def format_tokens(value):
    """Keep large token totals readable without hiding small exact totals."""
    value = max(0, int(value))
    if value < 1_000:
        return f'{value:,}'
    for factor, suffix in ((1_000_000_000_000, 'T'), (1_000_000_000, 'B'),
                           (1_000_000, 'M'), (1_000, 'k')):
        if value >= factor:
            scaled = value / factor
            if scaled >= 100:
                text = f'{scaled:.0f}'
            elif scaled >= 10:
                text = f'{scaled:.1f}'
            else:
                text = f'{scaled:.2f}'
            if '.' in text:
                text = text.rstrip('0').rstrip('.')
            if text == '1000' and suffix != 'T':
                continue
            return text + suffix
    return str(value)

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


def provider_lines(data, provider, config, now, width, host='omp'):
    adapter = get_host(host)
    lines = []
    reports = [r for r in data.get('reports', []) if r.get('provider') == provider]
    if not reports:
        note = data.get('dashboardNote')
        if not note:
            note = adapter.empty_compact_note if config['compact'] else adapter.empty_note
        elif config['compact'] and adapter.empty_compact_note != adapter.empty_note:
            note = adapter.empty_compact_note
        return [(adapter.empty_title, 'warn'), (clean(note), 'dim')]
    for index, report in enumerate(reports):
        label = f'Account {index + 1}' if len(reports) > 1 else ''
        fetched = report.get('fetchedAt')
        age = max(0, int(now - fetched / 1000)) if number(fetched) else None
        if label:
            lines.append((label, 'title'))
        if age is not None and age > config['interval'] + 5:
            lines.append((f'Source age: {age}s (cached)', 'dim'))
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
                lines.append(('ID: ' + clean(limit.get('id', '')), 'dim'))
        if not shown:
            lines.append(('All windows hidden' if report.get('limits') else 'No usage windows reported', 'dim'))
        credits = report.get('resetCredits', {}).get('availableCount')
        if number(credits):
            lines.append((f'Reset credits: {credits:g}', 'secondary'))
    if adapter.capture_status:
        capture = data.get('capture') or {}
        status = capture.get('status')
        if status in ('incomplete', 'unavailable'):
            lines.append((f'Token capture {status}', 'warn'))
            reason = capture.get('reason') or data.get('dashboardNote')
            if reason:
                lines.append((clean(reason), 'dim'))
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
    return text[:max(1, width)], ('secondary', 0, len(label), 'title')


def total_heading(label, total, width):
    tail = format_tokens(total)
    label = clean(label)
    room = max(1, width - len(tail) - 3)
    if len(label) > room:
        label = label[:max(0, room - 3)] + '.' * min(3, room)
    return section_heading(label, width, tail)

def command_box_rows(count, interval, position, width):
    outer = max(3, width - 2)
    inner = max(1, outer - 2)
    title = 'COMMANDS'
    title_prefix = '┌─ '
    title_suffix = ' '
    title_size = len(title_prefix) + len(title) + len(title_suffix) + 1
    if outer >= title_size:
        top = (title_prefix + title + title_suffix
               + '─' * (outer - title_size) + '┐')
        top_style = 'secondary'
    else:
        top = '┌' + '─' * (outer - 2) + '┐'
        top_style = 'secondary'

    content_width = max(1, inner - 2) if inner >= 2 else inner

    def inside(text, style):
        if inner >= 2:
            framed = '│ ' + text[:content_width].ljust(content_width) + ' │'
        else:
            framed = '│' + text[:inner].ljust(inner) + '│'
        return framed, ('secondary', 1, len(framed) - 1, style)

    summary = f'{count} provider{"s" if count != 1 else ""} | every {interval}s'
    status = next((candidate for candidate in (summary + position, summary)
                   if len(candidate) <= content_width), summary)
    keys = next((candidate for candidate in (
        'r Refresh | q Hide | Scroll',
        'r Refresh q Hide Scroll',
        'r/q Actions | Scroll',
        'r/q | Scroll',
    ) if len(candidate) <= content_width), 'r/q | Scroll')
    return [
        (top, top_style),
        (inside(status, 'dim')),
        (inside('[Refresh] [Hide]', 'normal')),
        (inside(keys, 'dim')),
        ('└' + '─' * (outer - 2) + '┘', 'secondary'),
    ]


def trace_plot(values, peak, columns, unicode):
    """Rasterize nonzero minute samples into a bounded 2x4-dot character grid."""
    grid = [[0] * columns for _ in range(6)]
    if peak:
        max_x = columns * 2 - 1
        # Row 23 touches the axis; draw zero endpoints only when they border
        # activity, leaving all-zero stretches unpainted.
        points = [(round(index * max_x / max(1, len(values) - 1)),
                   23 - round(value * 23 / peak))
                  for index, value in enumerate(values)]
        bits = ((1, 2, 4, 64), (8, 16, 32, 128))

        def mark(x, y):
            grid[y // 4][x // 2] |= bits[x % 2][y % 4]

        if values and values[0]:
            mark(*points[0])
        for ((x0, y0), value0), ((x1, y1), value1) in zip(
                zip(points, values), zip(points[1:], values[1:])):
            if not value0 and not value1:
                continue
            steps = max(abs(x1 - x0), abs(y1 - y0))
            if not steps:
                mark(x1, y1)
                continue
            for step in range(steps + 1):
                x = round(x0 + (x1 - x0) * step / steps)
                y = round(y0 + (y1 - y0) * step / steps)
                mark(x, y)
    if unicode:
        return [''.join(chr(0x2800 + mask) for mask in row) for row in grid]

    def ascii_cell(mask):
        if not mask:
            return ' '
        left = [y for y, bit in enumerate((1, 2, 4, 64)) if mask & bit]
        right = [y for y, bit in enumerate((8, 16, 32, 128)) if mask & bit]
        if left and right:
            delta = sum(right) / len(right) - sum(left) / len(left)
            return '/' if delta < -0.25 else '\\' if delta > 0.25 else '-'
        dots = left or right
        return '|' if len(dots) > 1 else '.'

    return [''.join(ascii_cell(mask) for mask in row) for row in grid]


def trace_chart(values, width):
    peak = max(values, default=0)
    scale = format_tokens(peak) if peak else '0'
    axis = max(4, len(scale))
    columns = max(1, width - axis - 2)
    try:
        TRACE_GLYPHS.encode(sys.stdout.encoding or 'ascii')
        unicode = True
        vertical, corner, horizontal = '│', '└', '─'
    except UnicodeEncodeError:
        unicode = False
        vertical, corner, horizontal = '|', '+', '-'

    plot = trace_plot(values, peak, columns, unicode)
    rows = [('', 'dim')]
    for row, graphic in enumerate(plot):
        label = scale if peak and row == 0 else ''
        text = label.rjust(axis) + ' ' + vertical + graphic
        rows.append((text, ('secondary', axis + 2, len(text) if peak else axis + 2, 'chart')))

    rows.append(('0'.rjust(axis) + ' ' + corner + horizontal * columns, 'secondary'))
    labels = f'-{len(values)}m'.ljust(max(0, columns - 3)) + 'now'
    rows.append((' ' * (axis + 2) + labels, 'secondary'))
    return rows


def token_chart(values, width, chart_type='bars'):
    if chart_type == 'trace':
        return trace_chart(values, width)

    peak = max(values, default=0)
    try:
        (BAR_GLYPHS if chart_type == 'bars' else '●│└─').encode(
            sys.stdout.encoding or 'ascii')
        unicode = True
        blocks, vertical, corner, horizontal = ' ▁▂▃▄▅▆▇█', '│', '└', '─'
    except UnicodeEncodeError:
        unicode = False
        blocks, vertical, corner, horizontal = ' .:-=+*O@', '|', '+', '-'
    scale = format_tokens(peak) if peak else '0'
    axis = max(4, len(scale))
    columns = max(1, width - axis - 2)
    # Stretch across the available width; max-pool only when narrower than the history.
    bins = [max(values[i * len(values) // columns:max(i * len(values) // columns + 1,
                (i + 1) * len(values) // columns)], default=0) for i in range(columns)]
    if peak and chart_type == 'dots':
        grid = [[' '] * columns for _ in range(6)]
        for x, value in enumerate(bins):
            if value:
                grid[6 - math.ceil(value * 6 / peak)][x] = '●' if unicode else 'o'
        plot = [''.join(row) for row in grid]
    else:
        heights = ([math.ceil(value * 48 / peak) if value else 0 for value in bins]
                   if peak else [0] * columns)
    rows = [('', 'dim')]
    for row in range(6):
        label = scale if peak and row == 0 else ''
        if peak and chart_type == 'dots':
            bars = plot[row]
        else:
            bars = ''.join(blocks[min(8, max(0, height - (5 - row) * 8))] for height in heights)
        text = label.rjust(axis) + ' ' + vertical + bars
        rows.append((text, ('secondary', axis + 2, len(text) if peak else axis + 2, 'chart')))
    rows.append(('0'.rjust(axis) + ' ' + corner + horizontal * columns, 'secondary'))
    labels = f'-{len(values)}m'.ljust(max(0, columns - 3)) + 'now'
    rows.append((' ' * (axis + 2) + labels, 'secondary'))
    return rows


def thinking_level_label(value):
    value = clean(value or '')
    if not value or value.lower() == 'unknown':
        return ''
    return {'minimal': 'Minimal', 'low': 'Low', 'medium': 'Medium', 'high': 'High',
            'max': 'Max', 'xhigh': 'xHigh'}.get(value.lower(), value)


def model_usage_rows(model, width, label=None):
    model_name = clean(model.get('model', 'unknown'))
    label = label or model_name
    thinking = thinking_level_label(model.get('thinking_level'))
    if thinking:
        label += ' - ' + thinking
    label += ' tokens'
    rows = [allowance_row(label, format_tokens(model['total']), '', width, 'normal')]
    rows.append((f'  In {format_tokens(model["input"])} / out {format_tokens(model["output"])}', 'secondary'))
    rows.append((f'  Cache r {format_tokens(model["cache_read"])} / w {format_tokens(model["cache_write"])}', 'secondary'))
    return rows


def detailed_model_rows(entries, width, include_provider=False):
    grouped = {}
    for entry in entries:
        grouped.setdefault((entry['provider'], entry['model']), []).append(entry)
    rows = []
    for key, variants in grouped.items():
        provider, model = key
        prefix = f'{NAMES.get(provider, provider)} / {clean(model)}' if include_provider else clean(model)
        for variant in variants:
            if rows:
                rows.append(('', 'dim'))
            rows.extend(model_usage_rows(variant, width, prefix))
    return rows

def session_lines(history, now, width, compact=True, previous_visible=True,
                  history_other_visible=True, history_total_visible=True, host='omp',
                  chart_type='bars'):
    rows = []
    current = history['current']
    if current:
        if chart_type == 'trace' and width == 20:
            rows.append(('TOKEN TRACE tok/min', ('secondary', 0, 11, 'title')))
        else:
            rows.append(section_heading('TOKEN TRACE' if chart_type == 'trace' else 'TOKEN RATE',
                                        width, 'tok/min'))
        if host == 'claude' and not current['providers']:
            rows.append(('Awaiting native usage', 'warn'))
        rows.extend(token_chart(history['chart'], width, chart_type))
        rows.append(('', 'dim'))
    elif not current and not history['previous']:
        rows.append((f'Waiting for {host.upper()} session', 'dim'))
        rows.append(('', 'dim'))
    for name, title in (('current', 'CURRENT SESSION'), ('previous', 'PREVIOUS SESSION')):
        session = history[name]
        if not session or (name == 'previous' and not previous_visible):
            continue
        rows.append(section_heading(title, width, session['id'][:8] if not compact else ''))
        providers = session['providers']
        total = sum(item['total'] for item in providers)
        single = compact and len(providers) == 1
        label = NAMES.get(providers[0]['provider'], providers[0]['provider']) if single else 'Tokens'
        if host == 'claude' and not providers:
            # The chart already flags the wait; only the value stays quiet.
            text = allowance_row(NAMES['anthropic'], 'unknown', '', width, 'normal')[0]
            rows.append((text, ('normal', len(text) - len('unknown'), len(text), 'dim')))
        else:
            rows.append(allowance_row(label, format_tokens(total), '', width, 'normal'))
        if not compact or name == 'previous':
            stamp = time.strftime('%b %d %H:%M', time.localtime(session['updated']))
            rows.append((f'Last recorded {stamp}', 'secondary'))
        for item in providers:
            if single:
                continue
            label = NAMES.get(item['provider'], item['provider'])
            if not compact:
                rows.append(('', 'dim'))
            if compact:
                rows.append(allowance_row(label, format_tokens(item['total']), '', width, 'normal'))
            else:
                rows.append(total_heading(label, item['total'], width))
                models = [model for model in session['models'] if model['provider'] == item['provider']]
                if models:
                    rows.append(('', 'dim'))
                rows.extend(detailed_model_rows(models, width))
        if compact:
            quotas = [quota for quota in session['quota']
                      if quota['intervals'] > 0 and round(quota['points'], 2) > 0]
            if quotas:
                rows.append(('', 'dim'))
                rows.append(('Quota change (observed)', 'dim'))
            for quota in quotas:
                label = NAMES.get(quota['provider'], quota['provider']) + ' ' + quota['label']
                duplicates = sum(item['provider'] == quota['provider'] and item['label'] == quota['label']
                                 for item in quotas)
                if duplicates > 1:
                    # Put the fingerprint first so width fitting cannot erase its identity.
                    label = '[' + quota['key'][:6] + '] ' + label
                rows.append(allowance_row(label, f'+{quota["points"]:.2f}%', '', width, 'normal'))
        rows.append(('', 'dim'))

    def history_rows(entries):
        total = sum(item['total'] for item in entries)
        if compact:
            return [allowance_row('Tokens', format_tokens(total), '', width, 'normal')]
        result = [total_heading('Tokens', total, width)]
        details = detailed_model_rows(entries, width, include_provider=True)
        if details:
            result.append(('', 'dim'))
            result.extend(details)
        return result

    for title, entries, visible in (
            ('HISTORY OTHER SESSIONS', history.get('history', []), history_other_visible),
            ('HISTORY TOTAL', history.get('total_history', []), history_total_visible)):
        if not visible or not entries:
            continue
        rows.append(section_heading(title, width))
        rows.extend(history_rows(entries))
        rows.append(('', 'dim'))
    return rows
