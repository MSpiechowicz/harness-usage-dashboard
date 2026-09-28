#!/usr/bin/env python3
"""Internal shell hook for interactive `omp`; the real OMP executable is unchanged."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ATTACH_WAIT_SECONDS = 8
ATTACH_POLL_SECONDS = 0.05


def _pane_target(environment):
    pane = environment.get('TMUX_PANE', '')
    if pane.startswith('%') and pane[1:].isascii() and pane[1:].isdecimal():
        return pane
    return None


def _tmux_query(environment, *arguments):
    """Use the pane's own socket, never the caller's default tmux server."""
    socket = environment.get('TMUX', '').rsplit(',', 2)[0]
    if not socket or _pane_target(environment) is None:
        return None
    try:
        from dashboard import tmux_binary
        return subprocess.check_output(
            [tmux_binary(), '-S', socket, *arguments], text=True,
            stderr=subprocess.DEVNULL, timeout=1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _attached_features(environment):
    pane = _pane_target(environment)
    if pane is None:
        return None
    output = _tmux_query(environment, 'list-clients', '-t', pane, '-F',
                         '#{client_session}|#{client_termfeatures}')
    if output is None:
        return None
    clients = []
    for row in output.splitlines():
        session, separator, features = row.partition('|')
        if not separator or not session:
            return None
        clients.append(set(features.split(',')))
    return clients


def select_image_environment(environment):
    """Default to SIXEL only when this tmux build and all attached clients affirm it."""
    force = environment.get('PI_FORCE_IMAGE_PROTOCOL', '')
    if 'PI_FORCE_IMAGE_PROTOCOL' in environment and force.strip().lower() != 'sixel':
        return environment

    if 'PI_FORCE_IMAGE_PROTOCOL' in environment and 'PI_ALLOW_SIXEL_PASSTHROUGH' in environment:
        return environment

    clients = _attached_features(environment)
    if not clients or not all('sixel' in features for features in clients):
        return environment
    support = _tmux_query(environment, 'display-message', '-p',
                          '#{sixel_support}')
    if support is None or support.strip() != '1':
        return environment

    environment.setdefault('PI_FORCE_IMAGE_PROTOCOL', 'sixel')
    environment.setdefault('PI_ALLOW_SIXEL_PASSTHROUGH', '1')
    return environment


def await_client_and_exec(command):
    """A new detached pane waits briefly for its first client before starting OMP."""
    environment = os.environ.copy()
    deadline = time.monotonic() + ATTACH_WAIT_SECONDS
    while True:
        clients = _attached_features(environment)
        if clients is None or clients or time.monotonic() >= deadline:
            break
        time.sleep(ATTACH_POLL_SECONDS)
    os.execvpe(command[0], command, select_image_environment(environment))

# These are OMP CLI subcommands, not chat prompts. Keep them outside the TUI wrapper.
SUBCOMMANDS = {
    'acp', 'agents', 'auth-broker', 'auth-gateway', 'bench', 'browser-relay', 'cleanse',
    'commit', 'completions', 'compress', 'config', 'dry-balance', 'gallery', 'gc', 'git',
    'grep', 'grievances', 'if-bench', 'images', 'install', 'join', 'models', 'plugin',
    'ps', 'read', 'render', 'say', 'search', 'setup', 'share', 'shell', 'ssh', 'stats',
    'tiny-models', 'token', 'ttsr', 'update', 'usage', 'worktree',
}
VALUE_FLAGS = {
    '--model', '--smol', '--slow', '--plan', '--prewalk-into', '--plan-yolo-into',
    '--provider', '--api-key', '--system-prompt', '--append-system-prompt', '--profile',
    '--cwd', '--config', '--add-dir', '--session-dir', '--models', '--tools', '--thinking',
    '--service-tier', '--hook', '--extension', '-e', '--skills', '--max-time',
    '--approval-mode', '--plugin-dir', '--mode',
}
BOOLEAN_FLAGS = {
    '--prewalk', '--no-prewalk', '--plan-yolo', '--allow-home', '--continue', '-c',
    '--from-claude', '--from-codex', '--no-session', '--no-tools', '--no-lsp', '--no-pty',
    '--hide-thinking', '--advisor', '--external-thinking', '--no-skills', '--no-rules',
    '--no-title', '--print-thoughts', '--auto-approve',
}
BYPASS_FLAGS = {'--help', '-h', '--version', '-v', '--print', '-p', '--export', '--alias', '--no-extensions'}


def should_wrap(argv, interactive):
    """Only wrap recognized interactive invocations, never pipes, RPC, or CLI tools."""
    if not interactive:
        return False
    index = 0
    seen_prompt = False
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            return True
        flag, equals, value = arg.partition('=')
        if flag in BYPASS_FLAGS:
            return False
        if flag in VALUE_FLAGS:
            if not equals:
                index += 1
                if index == len(argv):
                    return False  # Let OMP report its own missing-value error.
                value = argv[index]
            if flag == '--mode' and value != 'text':
                return False
        elif flag in ('--resume', '-r'):
            if not equals and index + 1 < len(argv) and not argv[index + 1].startswith('-'):
                index += 1
        elif flag in BOOLEAN_FLAGS:
            pass
        elif arg.startswith('-'):
            return False  # Unknown/new CLI modes must not accidentally enter tmux.
        else:
            if not seen_prompt and arg in SUBCOMMANDS:
                return False
            seen_prompt = True
        index += 1
    return True


def selected_profile(argv):
    profile = os.environ.get('OMP_PROFILE', os.environ.get('PI_PROFILE'))
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            break
        flag, equals, value = arg.partition('=')
        if flag in VALUE_FLAGS:
            if not equals:
                index += 1
                if index >= len(argv):
                    break
                value = argv[index]
            if flag == '--profile':
                profile = value
        index += 1
    return profile


def normalize_cwd(argv):
    """Keep OMP and usage subprocesses in the same explicitly selected directory."""
    argv = list(argv)
    selected = None
    index = 0
    while index < len(argv):
        flag, equals, value = argv[index].partition('=')
        if flag == '--':
            break
        if flag in VALUE_FLAGS:
            if not equals:
                index += 1
                if index >= len(argv):
                    break
                value = argv[index]
            if flag == '--cwd':
                selected = str(Path(value).expanduser().absolute())
                argv[index] = '--cwd=' + selected if equals else selected
        index += 1
    if selected is not None:
        os.chdir(selected)
    return argv


def main():
    argv = sys.argv[1:]
    if len(argv) > 2 and argv[:2] == ['--await-client', '--']:
        await_client_and_exec(argv[2:])
        return 0
    native = argv[:1] == ['--native']
    if native:
        argv = argv[1:]
    if argv[:1] == ['--']:
        argv = argv[1:]
    omp = shutil.which('omp') or str(Path.home() / '.local/bin/omp')
    if not should_wrap(argv, sys.stdin.isatty() and sys.stdout.isatty()):
        os.execv(omp, [omp, *argv])
    from argparse import Namespace
    from dashboard import extension_path, launch
    from preferences import resolve_profile
    try:
        profile = resolve_profile(selected_profile(argv))
        argv = normalize_cwd(argv)
        if os.environ.get('TMUX'):
            # The extension attaches to this existing pane on session_start.
            environment = os.environ.copy()
            environment['OMP_PROFILE'] = profile
            environment.pop('OMP_USAGE_LAUNCHER', None)
            select_image_environment(environment)
            extensions = [] if native else ['-e', str(extension_path(profile))]
            os.execve(omp, [omp, *extensions, *argv], environment)
        args = Namespace(providers=None, side=None, interval=None, profile=profile, owner=None, native=native)
        launch(args, argv)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'OMP dashboard: {error}', file=sys.stderr)
        print('Use `command omp` to bypass shell integration.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
