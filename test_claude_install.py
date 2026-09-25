"""Isolated Claude integration behavior; never writes to the real home."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import claude_install


ASSETS = ('claude_launcher.py', 'claude_bridge.py', 'claude_usage_source.py',
          'dashboard.py', 'updater.py', 'preferences.py', 'host_adapters.py',
          'session_usage.py', 'usage_source.py', 'package.json')


class ClaudeInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "checkout with 'quotes'"
        self.root.mkdir()
        for name in ASSETS:
            (self.root / name).write_text('{}\n' if name == 'package.json' else '\n', encoding='utf-8')
        (self.root / 'claude_launcher.py').write_text(
            'import json, sys\nprint("LAUNCH:" + json.dumps(sys.argv[1:]))\n'
            'sys.exit(23 if "--fail" in sys.argv else 0)\n', encoding='utf-8')
        self.commands = self.home / 'commands'
        self.commands.mkdir()
        self.executable('claude', '#!/usr/bin/env python3\nimport json, sys\n'
                        'print("NATIVE:" + json.dumps(sys.argv[1:]))\n'
                        'sys.exit(17 if "--fail" in sys.argv else 0)\n')
        self.executable('tmux', '#!/bin/sh\nprintf "tmux 3.4\\n"\n')
        self.bin_dir = self.home / 'local-bin'
        self.config = self.home / 'claude-config'
        self.xdg = self.home / 'xdg-config'
        self.fallback = self.home / 'missing-tmux'
        fixtures = (
            patch.dict(os.environ, {'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': str(self.config),
                                 'XDG_CONFIG_HOME': str(self.xdg), 'ZDOTDIR': str(self.home),
                                 'SHELL': '/bin/bash',
                                 'PATH': f'{self.commands}{os.pathsep}{os.environ.get("PATH", "")}'},
                       clear=False),
            patch.object(claude_install, 'ROOT', self.root),
            patch.object(claude_install, 'PRIVATE_TMUX', self.fallback),
        )
        for fixture in fixtures:
            fixture.start()
            self.addCleanup(fixture.stop)

    def executable(self, name, contents):
        path = self.commands / name
        path.write_text(contents, encoding='utf-8')
        path.chmod(0o755)
        return path

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = claude_install.main(['--bin-dir', str(self.bin_dir), *args])
        return status, out.getvalue(), err.getvalue()

    def test_installs_idempotently_and_uninstalls_without_touching_user_data(self):
        prefs = self.config / 'settings.json'
        prefs.parent.mkdir(parents=True)
        prefs.write_text('{"theme":"dark"}\n', encoding='utf-8')
        other = self.home / '.local/bin/omp'
        other.parent.mkdir(parents=True)
        other.write_text('OMP executable\n', encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'all')[0], 0)
        record = claude_install.validate_owned_installation(root=self.root)
        self.assertEqual(set(record['shells']), {'bash', 'zsh', 'fish'})
        self.assertTrue((self.config / 'skills/usage-dashboard/SKILL.md').is_file())
        self.assertEqual(self.invoke('--shell', 'all')[0], 0)
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertFalse(claude_install.record_path().exists())
        self.assertFalse(claude_install.skill_path().exists())
        self.assertFalse((self.home / '.bashrc').exists())
        self.assertFalse((self.home / '.zshrc').exists())
        self.assertFalse((self.xdg / 'fish/functions/claude.fish').exists())
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertEqual(prefs.read_text(encoding='utf-8'), '{"theme":"dark"}\n')
        self.assertEqual(other.read_text(encoding='utf-8'), 'OMP executable\n')

    def test_none_installs_only_skill_and_rejects_other_provider(self):
        self.assertEqual(self.invoke('--provider', 'openai', '--shell', 'none')[0], 1)
        self.assertFalse(claude_install.skill_path().exists())
        self.assertEqual(self.invoke('--shell', 'none')[0], 0)
        self.assertEqual(claude_install.validate_owned_installation(root=self.root)['shells'], {})
        self.assertFalse((self.home / '.bashrc').exists())
        self.assertEqual(self.invoke('--uninstall')[0], 0)

    def test_personal_command_collision_prevents_install_and_refresh_but_not_uninstall(self):
        command = self.config / 'commands/usage-dashboard.md'
        command.parent.mkdir(parents=True)
        command.write_text('my existing command\n', encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'all')[0], 1)
        self.assertFalse(claude_install.skill_path().exists())
        self.assertFalse((self.home / '.bashrc').exists())
        command.unlink()
        self.assertEqual(self.invoke('--shell', 'none')[0], 0)
        before = claude_install.record_path().read_text(encoding='utf-8')
        command.write_text('my existing command\n', encoding='utf-8')
        self.assertEqual(self.invoke('--refresh')[0], 1)
        self.assertEqual(claude_install.record_path().read_text(encoding='utf-8'), before)
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertEqual(command.read_text(encoding='utf-8'), 'my existing command\n')
        command.unlink()
        command.symlink_to(self.home / 'missing-command')
        self.assertEqual(self.invoke('--shell', 'none')[0], 1)
        self.assertTrue(command.is_symlink())

    def test_existing_rc_bytes_survive_install_refresh_and_uninstall(self):
        rc = self.home / '.bashrc'
        rc.write_text('export CUSTOM=1', encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'bash')[0], 0)
        self.assertEqual(self.invoke('--shell', 'bash')[0], 0)
        self.assertEqual(self.invoke('--shell', 'none')[0], 0)
        self.assertEqual(rc.read_text(encoding='utf-8'), 'export CUSTOM=1')
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertEqual(rc.read_text(encoding='utf-8'), 'export CUSTOM=1')

    def test_modified_block_and_foreign_rc_marker_refused_without_changes(self):
        rc = self.home / '.bashrc'
        rc.write_text(claude_install.BEGIN + '\n# Checkout: \"elsewhere\"\n'
                      'echo foreign\n' + claude_install.END + '\n', encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'bash')[0], 1)
        self.assertFalse(claude_install.skill_path().exists())
        rc.unlink()
        self.assertEqual(self.invoke('--shell', 'bash')[0], 0)
        original = rc.read_text(encoding='utf-8')
        rc.write_text(original.replace('# <<< claude-usage-dashboard <<<',
                                       '# <<< changed <<<'), encoding='utf-8')
        self.assertEqual(self.invoke('--uninstall')[0], 1)
        rc.write_text(original + '# personal\n', encoding='utf-8')
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertEqual(rc.read_text(encoding='utf-8'), '# personal\n')

    def test_skill_symlink_and_ancestor_symlink_are_refused(self):
        skill = claude_install.skill_path()
        skill.parent.mkdir(parents=True)
        private = self.home / 'private'
        private.write_text('my skill\n', encoding='utf-8')
        skill.symlink_to(private)
        self.assertEqual(self.invoke('--shell', 'none')[0], 1)
        self.assertEqual(private.read_text(encoding='utf-8'), 'my skill\n')
        skill.unlink()
        skill.parent.rmdir()
        skill.parent.symlink_to(self.home)
        self.assertEqual(self.invoke('--shell', 'none')[0], 1)
        self.assertFalse(claude_install.record_path().exists())

    def test_foreign_skill_hook_rc_and_symlink_refused_before_mutations(self):
        skill = claude_install.skill_path()
        skill.parent.mkdir(parents=True)
        skill.write_text('my skill\n', encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'all')[0], 1)
        self.assertFalse((self.home / '.bashrc').exists())
        skill.unlink()
        self.assertEqual(self.invoke('--shell', 'all')[0], 0)
        skill.write_text('personal edit\n', encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'all')[0], 1)
        self.assertEqual(self.invoke('--uninstall')[0], 1)
        self.assertEqual(skill.read_text(encoding='utf-8'), 'personal edit\n')
        skill.write_text(claude_install.skill_content(self.root), encoding='utf-8')
        self.assertEqual(self.invoke('--uninstall')[0], 0)

        rc = self.home / '.bashrc'
        rc.write_text('alias claude="custom"\n', encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'bash')[0], 1)
        self.assertFalse(claude_install.record_path().exists())
        rc.unlink()
        managed, _ = claude_install.shell_paths('bash')
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_text('personal function\n', encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'bash')[0], 1)
        self.assertEqual(managed.read_text(encoding='utf-8'), 'personal function\n')
        managed.unlink()
        target = self.home / 'personal-data'
        target.write_text('private\n', encoding='utf-8')
        managed.symlink_to(target)
        self.assertEqual(self.invoke('--shell', 'bash')[0], 1)
        self.assertEqual(target.read_text(encoding='utf-8'), 'private\n')
        self.assertTrue(managed.is_symlink())

    def test_transaction_rolls_back_on_late_write_failure(self):
        original = claude_install.replace_regular
        count = 0

        def fail_second(path, old, new):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('simulated disk failure')
            return original(path, old, new)

        with patch.object(claude_install, 'replace_regular', side_effect=fail_second):
            status, _, error = self.invoke('--shell', 'all')
        self.assertEqual(status, 1)
        self.assertIn('simulated disk failure', error)
        self.assertFalse(claude_install.shell_paths('bash')[0].exists())
        self.assertFalse(claude_install.record_path().exists())
        self.assertFalse(claude_install.skill_path().exists())

    def test_migrates_only_exact_owned_legacy_wrapper(self):
        legacy = self.bin_dir / 'claude-usage'
        legacy.parent.mkdir()
        legacy.write_text(claude_install.legacy_content(self.root), encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'none')[0], 0)
        self.assertFalse(legacy.exists())
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        legacy.write_text(claude_install.legacy_content(self.root) + '# changed\n', encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'none')[0], 0)
        self.assertTrue(legacy.exists())
        self.assertEqual(self.invoke('--uninstall')[0], 0)
        self.assertTrue(legacy.exists())

    def test_missing_assets_and_prerequisites_do_not_install_and_uninstall_needs_none(self):
        (self.root / 'dashboard.py').unlink()
        self.assertEqual(self.invoke('--shell', 'none')[0], 1)
        self.assertFalse(claude_install.skill_path().exists())
        (self.root / 'dashboard.py').write_text('\n', encoding='utf-8')
        (self.commands / 'claude').unlink()
        with patch.dict(os.environ, {'PATH': str(self.commands)}):
            self.assertEqual(self.invoke('--shell', 'none')[0], 1)
        self.executable('claude', '#!/bin/sh\nexit 0\n')
        self.assertEqual(self.invoke('--shell', 'none')[0], 0)
        (self.root / 'claude_launcher.py').unlink()
        (self.commands / 'claude').unlink()
        (self.commands / 'tmux').unlink()
        self.assertEqual(self.invoke('--uninstall')[0], 0)

    def test_refresh_keeps_original_targets_and_refuses_modified_ownership(self):
        self.assertEqual(self.invoke('--shell', 'bash')[0], 0)
        old = claude_install.validate_owned_installation(root=self.root)
        initial_record = claude_install.record_path().read_text(encoding='utf-8')
        initial_skill = claude_install.skill_path().read_text(encoding='utf-8')
        managed, rc = claude_install.shell_paths('bash')
        initial_hook = managed.read_text(encoding='utf-8')
        replacement = self.home / 'other checkout'
        replacement.mkdir()
        for name in ASSETS:
            shutil.copyfile(self.root / name, replacement / name)
        with self.assertRaisesRegex(RuntimeError, 'another checkout'):
            claude_install.refresh_owned_installation(root=replacement)
        self.assertEqual(claude_install.record_path().read_text(encoding='utf-8'), initial_record)
        self.assertEqual(claude_install.skill_path().read_text(encoding='utf-8'), initial_skill)
        self.assertEqual(managed.read_text(encoding='utf-8'), initial_hook)
        refreshed = claude_install.refresh_owned_installation(root=self.root)
        self.assertEqual(refreshed, old)
        rc.write_text(rc.read_text(encoding='utf-8').replace('claude-usage-dashboard <<<',
                                                            'changed <<<'), encoding='utf-8')
        with self.assertRaises(RuntimeError):
            claude_install.refresh_owned_installation(root=self.root)
        self.assertEqual(claude_install.record_path().read_text(encoding='utf-8'), initial_record)

    def test_refresh_across_changed_templates_uses_recorded_digest(self):
        self.assertEqual(self.invoke('--shell', 'bash')[0], 0)
        shell_content = claude_install.shell_content
        skill_content = claude_install.skill_content

        def next_shell(shell, root):
            return shell_content(shell, root).replace(
                claude_install.FILE_END, '# New release instruction\n' + claude_install.FILE_END)

        with patch.object(claude_install, 'shell_content', side_effect=next_shell), \
                patch.object(claude_install, 'skill_content',
                             side_effect=lambda root: skill_content(root) + '\nNew release instructions.\n'):
            self.assertEqual(claude_install.validate_owned_installation(root=self.root)['root'],
                             str(self.root))
            updated = claude_install.refresh_owned_installation(root=self.root)
            self.assertEqual(claude_install.validate_owned_installation(root=self.root), updated)
            self.assertEqual(self.invoke('--refresh')[0], 0)
            self.assertEqual(self.invoke('--uninstall')[0], 0)

    def test_interactive_shells_forward_arguments_and_status_noninteractive_bypasses(self):
        self.assertEqual(self.invoke('--shell', 'all')[0], 0)
        args = ['--model', 'some model', "it's quoted", '', '--fail']
        quoted = ' '.join(claude_install.shlex.quote(value) for value in args)
        expected = 'LAUNCH:' + json.dumps(args)
        native = 'NATIVE:' + json.dumps(args)
        for shell in ('bash', 'zsh', 'fish'):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                run = f'claude {quoted}'
                bypass = f'command claude {quoted}'
                wrapped = subprocess.run([shell, '-ic', run], capture_output=True, text=True)
                self.assertEqual((wrapped.returncode, wrapped.stdout.strip()), (23, expected))
                direct = subprocess.run([shell, '-ic', bypass], capture_output=True, text=True)
                self.assertEqual((direct.returncode, direct.stdout.strip()), (17, native))
                plain = subprocess.run([shell, '-c', run], capture_output=True, text=True)
                self.assertEqual((plain.returncode, plain.stdout.strip()), (17, native))


if __name__ == '__main__':
    unittest.main()
