#!/usr/bin/env python3
"""Install or remove the opt-in claude-usage command without changing Claude or OMP."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).absolute().parent
PRIVATE_TMUX = Path.home() / '.local/share/omp-usage-dashboard/vendor/usr/bin/tmux'


def prerequisites():
    if sys.version_info < (3, 10):
        raise RuntimeError('Python 3.10 or newer is required.')
    try:
        import curses  # noqa: F401
    except ImportError:
        raise RuntimeError('This Python installation lacks curses support.') from None

    claude = shutil.which('claude')
    if not claude:
        raise RuntimeError('Claude CLI must be installed and available on PATH.')

    for candidate in dict.fromkeys(path for path in (shutil.which('tmux'), str(PRIVATE_TMUX)) if path):
        try:
            result = subprocess.run([candidate, '-V'], check=True, capture_output=True,
                                    text=True, timeout=10)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            continue
        version = re.search(r'\btmux\s+(\d+)\.(\d+)', result.stdout)
        if version and tuple(map(int, version.groups())) >= (3, 3):
            return claude, candidate
    raise RuntimeError('tmux 3.3 or newer is required on PATH or at the private tmux fallback.')


def wrapper_content():
    launcher = ROOT / 'claude_launcher.py'
    owner = json.dumps(str(ROOT), ensure_ascii=True)
    return (f'#!/bin/sh\n# Managed by claude-usage-dashboard; checkout: {owner}\n'
            f'exec python3 {shlex.quote(str(launcher))} "$@"\n')


def owned_wrapper(path, content):
    """Only an ordinary file with this checkout's complete wrapper is ours."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
        with path.open('rb') as stream:
            return stream.read(len(content.encode('utf-8')) + 1) == content.encode('utf-8')
    except FileNotFoundError:
        return False


def install(path, content):
    if os.path.lexists(path):
        if not owned_wrapper(path, content):
            raise RuntimeError(f'Refusing to overwrite {path}; it is not this checkout\'s owned wrapper.')
        if os.access(path, os.X_OK):
            return
        path.chmod(0o755)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=f'.{path.name}.', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o755)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            raise RuntimeError(f'Refusing to overwrite {path}; it appeared during installation.') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin',
                        help='directory for the owned claude-usage executable (default: ~/.local/bin)')
    parser.add_argument('--uninstall', action='store_true',
                        help='remove only this checkout\'s owned executable; preserve all user data')
    args = parser.parse_args(argv)
    bin_dir = args.bin_dir.expanduser().absolute()
    path = bin_dir / 'claude-usage'
    content = wrapper_content()

    try:
        if args.uninstall:
            if owned_wrapper(path, content):
                path.unlink()
                print(f'Removed owned executable: {path}')
            else:
                print(f'No owned executable at {path}; unrelated files were preserved.')
            return 0

        if not (ROOT / 'claude_launcher.py').is_file():
            raise RuntimeError(f'Incomplete checkout: missing {ROOT / "claude_launcher.py"}.')
        claude, tmux = prerequisites()
        install(path, content)
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(f'{"Uninstall" if args.uninstall else "Installation"} failed: {exc}', file=sys.stderr)
        return 1

    print(f'Installed executable: {path}')
    print(f'Prerequisites: Claude CLI {claude}; tmux {tmux}')
    print('Keep this checkout in place. Run claude-usage with ordinary Claude CLI arguments.')
    if str(bin_dir) not in os.environ.get('PATH', '').split(os.pathsep):
        print(f'Add {shlex.quote(str(bin_dir))} to PATH to run claude-usage by name.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
