"""Offline ownership boundaries for shell installation; never use the real home."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install


class InstallerOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "checkout with 'quotes'"
        self.root.mkdir()
        for name in ('extension.js', 'dashboard.py', 'usage_source.py', 'session_usage.py', 'launcher.py', 'preferences.py', 'updater.py', 'package.json'):
            (self.root / name).write_text('', encoding='utf-8')
        self.agent = self.home / '.omp/agent'
        self.config = self.home / '.config'
        patches = (
            patch.object(install, 'ROOT', self.root),
            patch.object(install, 'OWNER', '# Checkout: ' + json.dumps(str(self.root), ensure_ascii=True)),
            patch.object(Path, 'home', return_value=self.home),
            patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config), 'SHELL': '/bin/bash',
                                    'ZDOTDIR': str(self.home), 'PI_CODING_AGENT_DIR': str(self.agent)}),
            patch.object(install, 'prerequisites', return_value=('/real/omp', '/real/tmux')),
        )
        for fixture in patches:
            fixture.start()
            self.addCleanup(fixture.stop)

    def invoke(self, *args):
        with patch('sys.argv', ['install.py', *args]), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return install.main()

    def test_reinstall_and_uninstall_preserve_user_data(self):
        rc = self.home / '.bashrc'
        original = '# my settings\nexport EDITOR=vim\n'
        rc.write_text(original, encoding='utf-8')
        self.agent.mkdir(parents=True)
        preferences = self.agent / 'usage-dashboard.json'
        preferences.write_text('{"enabled": false}\n', encoding='utf-8')
        binary = self.home / '.local/bin/omp'
        binary.parent.mkdir(parents=True)
        binary.write_text('real OMP binary\n', encoding='utf-8')
        old_link = binary.with_name('omp-dashboard')
        old_link.symlink_to(self.root / 'dashboard.py')

        self.assertEqual(self.invoke('--shell', 'all'), 0)
        installed_rc = rc.read_text(encoding='utf-8')
        self.assertFalse(os.path.lexists(old_link))
        self.assertEqual(self.invoke('--shell', 'all'), 0)
        self.assertEqual(rc.read_text(encoding='utf-8'), installed_rc)
        self.assertEqual(self.invoke('--uninstall'), 0)
        self.assertEqual(rc.read_text(encoding='utf-8'), original)
        self.assertEqual(preferences.read_text(encoding='utf-8'), '{"enabled": false}\n')
        self.assertEqual(binary.read_text(encoding='utf-8'), 'real OMP binary\n')
        self.assertFalse(os.path.lexists(self.agent / 'extensions/usage-dashboard.js'))
        for shell in install.SHELLS:
            managed, _ = install.shell_paths(shell)
            self.assertFalse(os.path.lexists(managed))

    def test_fish_migration_preserves_unowned_autoload(self):
        legacy = self.config / 'fish/conf.d/omp-usage-dashboard.fish'
        legacy.parent.mkdir(parents=True)
        original = f'{install.FILE_BEGIN}\n{install.OWNER}\nfunction omp\nend\n{install.FILE_END}\n'
        legacy.write_text(original, encoding='utf-8')
        autoload = self.config / 'fish/functions/omp.fish'
        autoload.parent.mkdir(parents=True)
        custom = 'function omp\n    command my-omp $argv\nend\n'
        autoload.write_text(custom, encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'fish'), 1)
        self.assertEqual(legacy.read_text(encoding='utf-8'), original)
        self.assertEqual(autoload.read_text(encoding='utf-8'), custom)

    def test_fish_migration_removes_owned_startup_hook(self):
        legacy = self.config / 'fish/conf.d/omp-usage-dashboard.fish'
        legacy.parent.mkdir(parents=True)
        legacy.write_text(f'{install.FILE_BEGIN}\n{install.OWNER}\nfunction omp\nend\n{install.FILE_END}\n',
                          encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'fish'), 0)
        self.assertFalse(legacy.exists())
        self.assertEqual(self.invoke('--shell', 'fish'), 0)
        self.assertEqual(self.invoke('--uninstall', '--shell', 'fish'), 0)
        self.assertFalse((self.config / 'fish/functions/omp.fish').exists())

    def test_native_setup_requires_new_stable_package_path(self):
        self.assertEqual(self.invoke('--native', '--shell', 'bash'), 1)
        stable = self.home / '.omp/plugins/node_modules/harness-usage-dashboard'
        stable.parent.mkdir(parents=True)
        stable.symlink_to(self.root, target_is_directory=True)
        with patch.object(install, 'ROOT', stable), \
                patch.object(install, 'OWNER', '# Checkout: ' + json.dumps(str(stable), ensure_ascii=True)):
            self.assertEqual(self.invoke('--native', '--shell', 'bash'), 0)
            managed, _ = install.shell_paths('bash')
            self.assertIn(str(stable / 'launcher.py'), managed.read_text(encoding='utf-8'))
            self.assertEqual(self.invoke('--native', '--uninstall', '--shell', 'bash'), 0)
            self.assertFalse(managed.exists())

    def test_alias_conflict_does_not_partially_install(self):
        rc = self.home / '.bashrc'
        original = "alias omp='custom-omp --flag'\n"
        rc.write_text(original, encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'all'), 1)
        self.assertEqual(rc.read_text(encoding='utf-8'), original)
        self.assertFalse(os.path.lexists(self.agent / 'extensions/usage-dashboard.js'))
        self.assertFalse(self.config.exists())

    def test_symlinked_rc_is_never_rewritten(self):
        original = self.home / 'dotfiles-bashrc'
        original.write_text('# externally managed\n', encoding='utf-8')
        rc = self.home / '.bashrc'
        rc.symlink_to(original)
        self.assertEqual(self.invoke('--shell', 'bash'), 1)
        self.assertEqual(self.invoke('--uninstall'), 0)
        self.assertTrue(rc.is_symlink())
        self.assertEqual(original.read_text(encoding='utf-8'), '# externally managed\n')

    def test_foreign_checkout_files_and_links_survive_uninstall(self):
        managed, rc = install.shell_paths('bash')
        managed.parent.mkdir(parents=True)
        foreign = '# Managed by somebody else\n'
        managed.write_text(foreign, encoding='utf-8')
        block = f'{install.BEGIN}\n# Checkout: "/other/checkout"\n. /other/hook\n{install.END}\n'
        rc.write_text(block, encoding='utf-8')
        extension = self.agent / 'extensions/usage-dashboard.js'
        extension.parent.mkdir(parents=True)
        extension.symlink_to('/other/extension.js')
        legacy = self.home / '.local/bin/omp-dashboard'
        legacy.parent.mkdir(parents=True)
        legacy.symlink_to('/other/dashboard.py')
        self.assertEqual(self.invoke('--shell', 'bash'), 1)
        self.assertEqual(self.invoke('--uninstall'), 0)
        self.assertEqual(managed.read_text(encoding='utf-8'), foreign)
        self.assertEqual(rc.read_text(encoding='utf-8'), block)
        self.assertEqual(os.readlink(extension), '/other/extension.js')
        self.assertEqual(os.readlink(legacy), '/other/dashboard.py')

    def test_duplicate_owned_markers_are_not_deleted(self):
        rc = self.home / '.bashrc'
        managed, _ = install.shell_paths('bash')
        malformed = install.integration_block(managed) * 2
        rc.write_text(malformed, encoding='utf-8')
        self.assertEqual(self.invoke('--shell', 'bash'), 1)
        self.assertEqual(self.invoke('--uninstall'), 1)
        self.assertEqual(rc.read_text(encoding='utf-8'), malformed)


if __name__ == '__main__':
    unittest.main()
