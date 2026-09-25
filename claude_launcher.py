#!/usr/bin/env python3
"""Opt-in interactive Claude dashboard; never changes Claude's global settings."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from argparse import Namespace


COMMANDS = {
    'agents', 'attach', 'auth', 'auto-mode', 'doctor', 'gateway', 'import',
    'install', 'logs', 'mcp', 'plugin', 'plugins', 'project', 'respawn', 'rm',
    'setup-token', 'stop', 'kill', 'ultrareview', 'update', 'upgrade',
}
VALUE_FLAGS = {
    '--agent', '--agents', '--append-system-prompt', '--autocompact',
    '--debug-file', '--effort', '--environment', '--fallback-model',
    '--input-format', '--json-schema', '--max-budget-usd', '--mcp-config',
    '--model', '-n', '--name', '--output-format', '--permission-mode',
    '--permission-prompts', '--plugin-dir', '--plugin-url',
    '--remote-control-session-name-prefix', '--session-id', '--setting-sources',
    '--settings', '--system-prompt', '--system-prompt-snapshot',
}
OPTIONAL_VALUE_FLAGS = {
    '--cloud', '--debug', '--from-pr', '-r', '--resume', '--remote-control',
    '--teleport', '-w', '--worktree', '--prompt-suggestions',
}
BOOLEAN_FLAGS = {
    '-c', '--continue', '--allow-dangerously-skip-permissions',
    '--ax-screen-reader', '--brief', '--chrome', '--dangerously-skip-permissions',
    '--disable-slash-commands', '--exclude-dynamic-system-prompt-sections',
    '--fork-session', '--ide', '--strict-mcp-config', '--verbose',
}
BYPASS_FLAGS = {
    '-h', '--help', '-v', '--version', '-p', '--print', '--bg', '--background',
    '--bare', '--safe-mode', '--restricted', '--tmux', '--no-session-persistence',
    '--include-hook-events', '--include-partial-messages',
    '--forward-subagent-text', '--replay-user-messages',
}
# Variadic CLI options make positional parsing ambiguous. Defer to Claude itself.
BYPASS_VALUE_FLAGS = {'--add-dir', '--allowedTools', '--allowed-tools', '--betas',
                      '--disallowedTools', '--disallowed-tools', '--file', '--tools'}
TELEMETRY_KEYS = {'OTEL_LOGS_EXPORTER', 'CLAUDE_CODE_ENABLE_TELEMETRY'}


def should_wrap(argv, interactive):
    """Wrap only recognized interactive CLI syntax, without interpreting prompts."""
    if not interactive:
        return False

    index = 0
    seen_prompt = False
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            return True
        flag, equals, _ = arg.partition('=')
        if flag in BYPASS_FLAGS or flag in BYPASS_VALUE_FLAGS:
            return False
        if flag in VALUE_FLAGS:
            if not equals:
                index += 1
                if index == len(argv):
                    return False
        elif flag in OPTIONAL_VALUE_FLAGS:
            # An optional argument may also be a prompt; pass bytes to Claude unchanged.
            if not equals and index + 1 < len(argv) and not argv[index + 1].startswith('-'):
                index += 1
        elif flag in BOOLEAN_FLAGS:
            pass
        elif arg.startswith('-'):
            return False
        else:
            if not seen_prompt and arg in COMMANDS:
                return False
            seen_prompt = True
        index += 1
    return True


def _settings_argument(argv):
    """Find the one explicitly supplied settings value, or reject ambiguous syntax."""
    found = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            break
        flag, equals, value = arg.partition('=')
        if flag in VALUE_FLAGS:
            if not equals:
                index += 1
                if index == len(argv):
                    raise ValueError(f'{flag} needs a value')
                value = argv[index]
            if flag == '--settings':
                if found is not None:
                    raise ValueError('multiple --settings options cannot be merged safely')
                found = (index, bool(equals), value)
        elif flag in OPTIONAL_VALUE_FLAGS and not equals:
            if index + 1 < len(argv) and not argv[index + 1].startswith('-'):
                index += 1
        index += 1
    return found


def _read_settings(value):
    if value.lstrip().startswith('{'):
        result = json.loads(value)
    else:
        result = json.loads(Path(value).expanduser().read_text(encoding='utf-8'))
    if not isinstance(result, dict):
        raise ValueError('settings must contain a JSON object')
    return result


def _telemetry_conflict(configuration):
    environment = configuration.get('env', {})
    if not isinstance(environment, dict):
        return True
    return any(key in TELEMETRY_KEYS or key.startswith('OTEL_EXPORTER_OTLP')
               for key in environment)


def _existing_settings():
    """Observe active user/project/managed settings without editing any of them."""
    config_root = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser()
    locations = [Path('/etc/claude-code/managed-settings.json'),
                 config_root / 'managed-settings.json',
                 config_root / 'settings.json']
    for folder in (Path.cwd(), *Path.cwd().parents):
        locations.extend((folder / '.claude/settings.json',
                          folder / '.claude/settings.local.json'))

    existing_status = False
    conflict = False
    for path in dict.fromkeys(locations):
        try:
            settings = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError):
            # Unknown policy is not permission to replace its telemetry destination.
            conflict = True
            continue
        if not isinstance(settings, dict):
            conflict = True
            continue
        existing_status |= 'statusLine' in settings
        conflict |= _telemetry_conflict(settings)
    return existing_status, conflict


def _merge_settings(base, additions):
    """Append hook commands; never replace user hooks or statusLine."""
    result = dict(base)
    hooks = base.get('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('existing hooks cannot be merged safely')
    merged_hooks = dict(hooks)
    for event, new_entries in additions['hooks'].items():
        previous = hooks.get(event, [])
        if not isinstance(previous, list):
            raise ValueError(f'existing {event} hooks cannot be merged safely')
        merged_hooks[event] = [*previous, *new_entries]
    result['hooks'] = merged_hooks
    if 'statusLine' in additions and 'statusLine' not in base:
        result['statusLine'] = additions['statusLine']
    return result


def _run_in_current_pane(command, environment):
    process = subprocess.Popen(command, env=environment)
    try:
        return process.wait()
    except BaseException:
        # Do not close the receiver while a child launched in this pane survives.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise


def _run_wrapped(claude, argv, selected, original):
    from claude_bridge import start_receiver
    from dashboard import control, launch

    existing_status, existing_conflict = _existing_settings()
    conflict = existing_conflict or _telemetry_conflict(original) or any(
        key in TELEMETRY_KEYS or key.startswith('OTEL_EXPORTER_OTLP') for key in os.environ)

    owner = os.environ.get('TMUX_PANE') if os.environ.get('TMUX') else None
    args = Namespace(host='claude', profile=None, owner=owner, providers=None,
                     side=None, interval=None, claude_binary=claude)
    with start_receiver(owner=owner, cwd=os.getcwd(), profile=None) as receiver:
        additions = receiver.settings(include_status_line=not existing_status and 'statusLine' not in original)
        try:
            merged = _merge_settings(original, additions)
        except ValueError as exc:
            print(f'claude-usage: settings cannot be merged ({exc}); starting Claude unchanged.',
                  file=sys.stderr)
            return subprocess.run([claude, *argv], check=False).returncode
        with tempfile.TemporaryDirectory(prefix='claude-usage-') as directory:
            settings_file = Path(directory) / 'settings.json'
            descriptor = os.open(settings_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(merged, output)
            if selected:
                position, equals, _ = selected
                injected = list(argv)
                injected[position] = ('--settings=' if equals else '') + str(settings_file)
            else:
                injected = [*argv, '--settings', str(settings_file)]

            environment = os.environ.copy()
            additions_env = receiver.environment()
            if conflict:
                additions_env = {key: value for key, value in additions_env.items()
                                 if key.startswith('CLAUDE_USAGE_BRIDGE_')}
                print('claude-usage: existing telemetry configuration retained; token capture unavailable.',
                      file=sys.stderr)
            environment.update(additions_env)

            if os.environ.get('TMUX'):
                try:
                    control(args, ['init'])
                except (OSError, subprocess.SubprocessError):
                    print('claude-usage: sidebar unavailable (tmux operation failed).',
                          file=sys.stderr)
                except ValueError as exc:
                    print(f'claude-usage: sidebar unavailable: {exc}', file=sys.stderr)
                return _run_in_current_pane([claude, *injected], environment)

            # A new tmux server inherits the receiver environment without exposing
            # its auth token in the pane command or in subprocess argv.
            saved = os.environ.copy()
            try:
                for key, value in additions_env.items():
                    os.environ[key] = value
                return launch(args, injected)
            finally:
                for key in additions_env:
                    if key in saved:
                        os.environ[key] = saved[key]
                    else:
                        os.environ.pop(key, None)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    claude = shutil.which('claude')
    if claude is None:
        print('claude-usage: Claude CLI is not available on PATH.', file=sys.stderr)
        return 1
    if not should_wrap(argv, sys.stdin.isatty() and sys.stdout.isatty()):
        os.execv(claude, [claude, *argv])

    try:
        selected = _settings_argument(argv)
        original = _read_settings(selected[2]) if selected else {}
    except (OSError, UnicodeError):
        print('claude-usage: settings unavailable; starting Claude unchanged.',
              file=sys.stderr)
        return subprocess.run([claude, *argv], check=False).returncode
    except ValueError as exc:
        print(f'claude-usage: settings unavailable ({exc}); starting Claude unchanged.',
              file=sys.stderr)
        return subprocess.run([claude, *argv], check=False).returncode

    try:
        return _run_wrapped(claude, argv, selected, original)
    except (OSError, subprocess.SubprocessError):
        print('claude-usage: dashboard unavailable (launch or local operation failed).',
              file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f'claude-usage: dashboard unavailable: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
