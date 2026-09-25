"""Offline installation boundaries; never install into the real home."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import claude_install


class ClaudeInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "checkout with 'quotes'"
        self.root.mkdir()
        (self.root / 'claude_launcher.py').write_text(
            'import json, sys\nprint(json.dumps(sys.argv[1:]))\n', encoding='utf-8')
        self.bin_dir = self.home / 'local bin'
        self.commands = self.home / 'commands'
        self.commands.mkdir()
        self.executable('claude', '#!/bin/sh\nexit 0\n')
        self.executable('tmux', '#!/bin/sh\nprintf "tmux 3.4\\n"\n')
        self.fallback = self.home / 'missing-tmux'

        environment = patch.dict(os.environ, {'PATH': f'{self.commands}{os.pathsep}{os.environ.get("PATH", "")}'})
        for fixture in (environment, patch.object(claude_install, 'ROOT', self.root),
                        patch.object(claude_install, 'PRIVATE_TMUX', self.fallback)):
            fixture.start()
            self.addCleanup(fixture.stop)

    def executable(self, name, contents):
        path = self.commands / name
        path.write_text(contents, encoding='utf-8')
        path.chmod(0o755)
        return path

    def invoke(self, *args):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = claude_install.main(['--bin-dir', str(self.bin_dir), *args])
        return status, out.getvalue(), err.getvalue()

    def test_installs_executable_forwarding_arguments_and_removes_only_wrapper(self):
        prefs = self.home / '.claude/settings.json'
        prefs.parent.mkdir(parents=True)
        prefs.write_text('{"theme":"dark"}\n', encoding='utf-8')
        other = self.home / '.local/bin/omp'
        other.parent.mkdir(parents=True)
        other.write_text('OMP executable\n', encoding='utf-8')
        wrapper = self.bin_dir / 'claude-usage'

        status, output, error = self.invoke()
        self.assertEqual((status, error), (0, ''))
        self.assertIn(str(wrapper), output)
        self.assertIn('PATH', output)
        self.assertTrue(os.access(wrapper, os.X_OK))
        result = subprocess.run([str(wrapper), '--model', 'some model', "it's quoted", ''],
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), ['--model', 'some model', "it's quoted", ''])
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertFalse(os.path.lexists(wrapper))
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertEqual(prefs.read_text(encoding='utf-8'), '{"theme":"dark"}\n')
        self.assertEqual(other.read_text(encoding='utf-8'), 'OMP executable\n')
        self.assertTrue((self.root / 'claude_launcher.py').exists())

    def test_install_rejects_foreign_regular_file_and_symlink_without_damage(self):
        self.bin_dir.mkdir()
        wrapper = self.bin_dir / 'claude-usage'
        wrapper.write_text('user executable\n', encoding='utf-8')
        status, _, error = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn('Refusing to overwrite', error)
        self.assertEqual(wrapper.read_text(encoding='utf-8'), 'user executable\n')
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertEqual(wrapper.read_text(encoding='utf-8'), 'user executable\n')

        wrapper.unlink()
        target = self.home / 'personal-script'
        target.write_text('personal data\n', encoding='utf-8')
        wrapper.symlink_to(target)
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertTrue(wrapper.is_symlink())
        self.assertEqual(target.read_text(encoding='utf-8'), 'personal data\n')

    def test_symlink_to_owned_contents_is_not_owned(self):
        self.assertEqual(self.invoke()[0], 0)
        wrapper = self.bin_dir / 'claude-usage'
        relocated = self.home / 'relocated-owned-wrapper'
        wrapper.rename(relocated)
        wrapper.symlink_to(relocated)

        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertTrue(wrapper.is_symlink())
        self.assertTrue(relocated.exists())

    def test_modified_owned_file_is_no_longer_owned(self):
        self.assertEqual(self.invoke()[0], 0)
        wrapper = self.bin_dir / 'claude-usage'
        wrapper.write_text(wrapper.read_text(encoding='utf-8') + '# local edit\n', encoding='utf-8')
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertTrue(wrapper.exists())
        self.assertIn('# local edit', wrapper.read_text(encoding='utf-8'))

    def test_missing_claude_or_suitable_tmux_fails_without_installing(self):
        wrapper = self.bin_dir / 'claude-usage'
        (self.commands / 'claude').unlink()
        with patch.dict(os.environ, {'PATH': str(self.commands)}):
            status, _, error = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn('Claude CLI', error)
        self.assertFalse(os.path.lexists(wrapper))

        self.executable('claude', '#!/bin/sh\nexit 0\n')
        self.executable('tmux', '#!/bin/sh\nprintf "tmux 3.2\\n"\n')
        status, _, error = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn('tmux 3.3', error)
        self.assertFalse(os.path.lexists(wrapper))

        self.fallback.write_text('#!/bin/sh\nprintf "tmux 3.3\\n"\n', encoding='utf-8')
        self.fallback.chmod(0o755)
        status, output, error = self.invoke()
        self.assertEqual((status, error), (0, ''))
        self.assertIn(str(self.fallback), output)
        self.assertTrue(wrapper.exists())

    def test_uninstall_never_requires_launcher_or_prerequisites(self):
        self.assertEqual(self.invoke()[0], 0)
        (self.root / 'claude_launcher.py').unlink()
        (self.commands / 'claude').unlink()
        (self.commands / 'tmux').unlink()
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertFalse(os.path.lexists(self.bin_dir / 'claude-usage'))


if __name__ == '__main__':
    unittest.main()
