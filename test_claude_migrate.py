"""Ownership and transactional legacy migration, isolated from the real home."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import claude_migrate as migration


class ClaudeMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / 'home'
        self.home.mkdir()
        self.config = self.home / '.claude'
        self.bin_dir = self.home / '.local/bin'
        self.bin_dir.mkdir(parents=True)
        # This obsolete checkout is deliberately absent and unrelated to the plugin cache.
        self.root = Path(temporary.name) / 'retired checkout'
        environment = patch.dict(os.environ, {
            'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': str(self.config),
            'XDG_CONFIG_HOME': str(self.home / '.config'), 'ZDOTDIR': str(self.home),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))

    def install_fixture(self, shells=('bash', 'zsh', 'fish'), prefix='export CUSTOM=1', suffix=''):
        owner = '# Checkout: ' + json.dumps(str(self.root)) + '\n'
        entries = {}
        for shell in shells:
            managed, rc = migration.shell_paths(shell)
            hook = migration.FILE_BEGIN + '\n' + owner + '# Old release template\n' + migration.FILE_END + '\n'
            self.write(managed, hook)
            block = migration.BEGIN + '\n' + owner + '. old-hook\n' + migration.END + '\n'
            if rc is not None:
                self.write(rc, prefix + ('\n' if prefix and not prefix.endswith('\n') else '') + block + suffix)
            entries[shell] = {
                'managed': str(managed), 'rc': str(rc) if rc else None,
                'rc_created': rc is not None and not prefix,
                'rc_separator': rc is not None and bool(prefix) and not prefix.endswith('\n'),
                'digest': migration.digest(hook),
                'block_digest': migration.digest(block) if rc else None,
            }
        self.write(migration.skill_path(), 'Old release personal skill\n')
        record = {'version': 1, 'root': str(self.root), 'shells': entries,
                  'skill': str(migration.skill_path()),
                  'skill_digest': migration.digest('Old release personal skill\n')}
        self.write(migration.record_path(), json.dumps(record, indent=2) + '\n')
        return record

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = migration.main(['--bin-dir', str(self.bin_dir), *args])
        return status, out.getvalue(), err.getvalue()

    def snapshot(self):
        return {str(path.relative_to(self.home)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in self.home.rglob('*') if path.is_file() and not path.is_symlink()}

    def test_dry_run_is_default_and_apply_preserves_surrounding_bytes_and_user_data(self):
        self.install_fixture(prefix='export CUSTOM=1', suffix='# after\r\n')
        protected = {
            self.config / 'usage-dashboard/usage-dashboard.sqlite3': 'ledger bytes',
            self.config / 'usage-dashboard/preferences.json': '{"theme":"custom"}',
            self.config / 'settings.json': '{"user":"settings"}',
            self.config / 'commands/usage-dashboard.md': 'personal command',
            self.bin_dir / 'omp-usage': 'unrelated wrapper',
        }
        for path, content in protected.items():
            self.write(path, content)
        self.root.mkdir()
        (self.root / 'user.txt').write_text('keep checkout', encoding='utf-8')
        before = self.snapshot()
        status, output, error = self.invoke()
        self.assertEqual((status, error), (0, ''))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.invoke('--apply')[0], 0)
        for shell in migration.SHELLS:
            managed, rc = migration.shell_paths(shell)
            self.assertFalse(managed.exists())
            if rc is not None:
                self.assertEqual(rc.read_bytes(), b'export CUSTOM=1# after\r\n')
        self.assertFalse(migration.skill_path().exists())
        self.assertFalse(migration.record_path().exists())
        for path, content in protected.items():
            self.assertEqual(path.read_text(encoding='utf-8'), content)
        self.assertEqual((self.root / 'user.txt').read_text(encoding='utf-8'), 'keep checkout')
        remaining = self.snapshot()
        self.assertEqual(self.invoke('--apply')[0], 0)
        self.assertEqual(self.snapshot(), remaining)

    def test_created_rc_is_removed_only_if_empty_after_block_removal(self):
        self.install_fixture(shells=('bash', 'zsh'), prefix='', suffix='# later user addition\n')
        self.assertEqual(self.invoke('--apply')[0], 0)
        for shell in ('bash', 'zsh'):
            self.assertEqual(migration.shell_paths(shell)[1].read_text(), '# later user addition\n')
        self.install_fixture(shells=('bash',), prefix='')
        self.assertEqual(self.invoke('--apply')[0], 0)
        self.assertFalse((self.home / '.bashrc').exists())

    def test_no_record_grants_no_ownership_even_for_exact_looking_artifacts(self):
        self.install_fixture()
        migration.record_path().unlink()
        self.write(self.bin_dir / 'claude-usage', migration.legacy_content(self.root))
        before = self.snapshot()
        self.assertEqual(self.invoke('--apply')[0], 0)
        self.assertEqual(self.snapshot(), before)

    def test_modified_or_missing_required_targets_refuse_without_partial_removal(self):
        for target in ('skill', 'hook', 'block'):
            for missing in (False, True):
                with self.subTest(target=target, missing=missing):
                    self.install_fixture()
                    managed, rc = migration.shell_paths('bash')
                    path = {'skill': migration.skill_path(), 'hook': managed, 'block': rc}[target]
                    if missing:
                        path.unlink()
                    else:
                        self.write(path, path.read_text() + '# modified\n' if target != 'block'
                                   else path.read_text().replace('. old-hook', '. foreign-hook'))
                    before = self.snapshot()
                    self.assertEqual(self.invoke('--apply')[0], 1)
                    self.assertEqual(self.snapshot(), before)

    def test_allowlist_and_ownership_markers_cannot_be_replaced_by_matching_digests(self):
        for mutation in ('path', 'hook-marker', 'block-marker', 'shell', 'version'):
            with self.subTest(mutation=mutation):
                record = self.install_fixture(shells=('bash',))
                managed, rc = migration.shell_paths('bash')
                if mutation == 'path':
                    record['shells']['bash']['managed'] = str(self.home / 'foreign')
                elif mutation == 'hook-marker':
                    self.write(managed, 'foreign\n')
                    record['shells']['bash']['digest'] = migration.digest('foreign\n')
                elif mutation == 'block-marker':
                    text = rc.read_text().replace('# Checkout: ' + json.dumps(str(self.root)), '# Checkout: "foreign"')
                    self.write(rc, text)
                    record['shells']['bash']['block_digest'] = migration.digest(migration.split_block(text, rc)[1])
                elif mutation == 'shell':
                    record['shells']['foreign'] = record['shells'].pop('bash')
                else:
                    record['version'] = True
                self.write(migration.record_path(), json.dumps(record))
                before = self.snapshot()
                self.assertEqual(self.invoke('--apply')[0], 1)
                self.assertEqual(self.snapshot(), before)

    def test_duplicate_or_malformed_record_and_markers_refuse_without_changes(self):
        for mutation in ('json', 'duplicate-key', 'duplicate-block', 'missing-end', 'separator'):
            with self.subTest(mutation=mutation):
                self.install_fixture(shells=('bash',))
                record = migration.record_path()
                rc = migration.shell_paths('bash')[1]
                if mutation == 'json':
                    self.write(record, '{')
                elif mutation == 'duplicate-key':
                    self.write(record, record.read_text().replace('"version": 1', '"version": 1, "version": 1'))
                elif mutation == 'duplicate-block':
                    self.write(rc, rc.read_text() + migration.split_block(rc.read_text(), rc)[1])
                elif mutation == 'missing-end':
                    self.write(rc, rc.read_text().replace(migration.END, '# missing'))
                else:
                    self.write(rc, rc.read_text().replace('export CUSTOM=1\n', 'export CUSTOM=1'))
                before = self.snapshot()
                self.assertEqual(self.invoke('--apply')[0], 1)
                self.assertEqual(self.snapshot(), before)

    def test_symlinked_targets_and_ancestors_are_refused(self):
        for target in ('skill', 'hook', 'rc', 'record', 'wrapper', 'ancestor'):
            with self.subTest(target=target):
                self.install_fixture(shells=('bash',))
                managed, rc = migration.shell_paths('bash')
                foreign = self.home / 'foreign'
                self.write(foreign, 'foreign data')
                path = {'skill': migration.skill_path(), 'hook': managed, 'rc': rc,
                        'record': migration.record_path(), 'wrapper': self.bin_dir / 'claude-usage',
                        'ancestor': managed.parent}[target]
                if target == 'ancestor':
                    saved = self.home / 'saved-hooks'
                    path.rename(saved)
                    path.symlink_to(saved, target_is_directory=True)
                else:
                    path.unlink(missing_ok=True)
                    path.symlink_to(foreign)
                before = self.snapshot()
                self.assertEqual(self.invoke('--apply')[0], 1)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(path.is_symlink())
                path.unlink()
                if target == 'ancestor':
                    saved.rename(path)

    def test_only_exact_owned_legacy_wrapper_is_removed_and_record_is_last(self):
        self.install_fixture()
        wrapper = self.bin_dir / 'claude-usage'
        self.write(wrapper, migration.legacy_content(self.root))
        changes = migration.plan_migration(self.bin_dir)
        self.assertEqual(changes[-1][0], migration.record_path())
        self.assertEqual(self.invoke('--apply')[0], 0)
        self.assertFalse(wrapper.exists())
        for foreign in ('#!/bin/sh\necho foreign\n', migration.legacy_content(self.root) + '# edited\n'):
            self.install_fixture()
            self.write(wrapper, foreign)
            before = self.snapshot()
            self.assertEqual(self.invoke('--apply')[0], 1)
            self.assertEqual(self.snapshot(), before)

    def test_compare_before_mutation_refuses_changed_target(self):
        self.install_fixture()
        changes = migration.plan_migration(self.bin_dir)
        self.write(migration.skill_path(), 'concurrent change')
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'changed during migration'):
            migration.apply_migration(changes)
        self.assertEqual(self.snapshot(), before)

    def test_late_failure_rolls_back_all_content_and_original_modes(self):
        self.install_fixture()
        managed, rc = migration.shell_paths('bash')
        managed.chmod(0o755)
        rc.chmod(0o640)
        before = self.snapshot()
        replace = migration.replace_regular

        def fail_record(path, old, new, mode=None):
            if path == migration.record_path() and new is None:
                raise OSError('injected late removal failure')
            return replace(path, old, new, mode=mode)

        with patch.object(migration, 'replace_regular', side_effect=fail_record):
            self.assertEqual(self.invoke('--apply')[0], 1)
        self.assertEqual(self.snapshot(), before)

    def test_mid_apply_concurrent_edit_is_preserved_while_prior_removals_roll_back(self):
        self.install_fixture()
        replace = migration.replace_regular
        rc = migration.shell_paths('bash')[1]

        def concurrent_edit(path, old, new, mode=None):
            if path == rc:
                self.write(rc, 'concurrent user edit\n')
            return replace(path, old, new, mode=mode)

        with patch.object(migration, 'replace_regular', side_effect=concurrent_edit):
            self.assertEqual(self.invoke('--apply')[0], 1)
        self.assertEqual(rc.read_text(), 'concurrent user edit\n')
        self.assertTrue(migration.shell_paths('bash')[0].exists())
        self.assertTrue(migration.record_path().exists())
        self.assertTrue(migration.skill_path().exists())

    def test_root_owned_sticky_immediate_parent_is_not_a_safe_migration_target(self):
        self.install_fixture(shells=('bash',))
        self.write(self.bin_dir / 'claude-usage', migration.legacy_content(self.root))
        before = self.snapshot()
        lstat = os.lstat

        def sticky_parent(path, *args, **kwargs):
            details = lstat(path, *args, **kwargs)
            if Path(path) == self.bin_dir:
                fields = list(details)
                fields[0] = stat.S_IFDIR | 0o1777
                fields[4] = 0
                return os.stat_result(fields)
            return details

        with patch.object(migration.os, 'lstat', side_effect=sticky_parent):
            for args in ((), ('--apply',)):
                self.assertEqual(self.invoke(*args)[0], 1)
        self.assertEqual(self.snapshot(), before)

    def test_writable_ancestry_refuses_preview_and_apply_without_changing_artifacts(self):
        self.install_fixture()
        self.write(self.bin_dir / 'claude-usage', migration.legacy_content(self.root))
        self.write(self.config / 'usage-dashboard/usage-dashboard.sqlite3', 'ledger bytes')
        self.write(self.config / 'usage-dashboard/preferences.json', '{"theme":"custom"}')
        directories = (migration.record_path().parent, migration.skill_path().parent,
                       self.home / '.config', self.home, self.bin_dir.parent, self.bin_dir)
        for directory in directories:
            original_mode = stat.S_IMODE(directory.stat().st_mode)
            for mode in (0o775, 0o777, 0o1777):
                # Only root-owned sticky *ancestors* may be shared writable.
                if (os.geteuid() == 0 and mode == 0o1777
                        and directory not in (migration.record_path().parent,
                                              migration.skill_path().parent, self.home, self.bin_dir)):
                    continue
                with self.subTest(directory=directory, mode=oct(mode)):
                    try:
                        directory.chmod(mode)
                        before = self.snapshot()
                        for args in ((), ('--apply',)):
                            self.assertEqual(self.invoke(*args)[0], 1)
                            self.assertEqual(self.snapshot(), before)
                            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), mode)
                    finally:
                        directory.chmod(original_mode)

    def test_foreign_owned_ancestry_and_leaves_refuse_preview_and_apply(self):
        self.install_fixture()
        wrapper = self.bin_dir / 'claude-usage'
        self.write(wrapper, migration.legacy_content(self.root))
        managed, rc = migration.shell_paths('bash')
        targets = (migration.record_path().parent, self.home / '.config', self.bin_dir.parent,
                   migration.record_path(), migration.skill_path(), managed, rc, wrapper)
        lstat = os.lstat
        for target in targets:
            with self.subTest(target=target):
                before = self.snapshot()

                def foreign_owner(path, *args, **kwargs):
                    details = lstat(path, *args, **kwargs)
                    if Path(path) == target:
                        fields = list(details)
                        fields[4] = os.geteuid() + 1
                        return os.stat_result(fields)
                    return details

                with patch.object(migration.os, 'lstat', side_effect=foreign_owner):
                    for args in ((), ('--apply',)):
                        self.assertEqual(self.invoke(*args)[0], 1)
                self.assertEqual(self.snapshot(), before)

    def test_non_directory_ancestor_refuses_preview_and_apply(self):
        self.install_fixture(shells=('bash',))
        directory = migration.shell_paths('bash')[0].parent
        saved = self.home / 'saved-hooks'
        directory.rename(saved)
        self.write(directory, 'foreign obstruction\n')
        before = self.snapshot()
        for args in ((), ('--apply',)):
            self.assertEqual(self.invoke(*args)[0], 1)
            self.assertEqual(self.snapshot(), before)

    def test_shared_writable_leaves_refuse_preview_and_apply(self):
        self.install_fixture()
        wrapper = self.bin_dir / 'claude-usage'
        self.write(wrapper, migration.legacy_content(self.root))
        managed, rc = migration.shell_paths('bash')
        for target in (migration.record_path(), migration.skill_path(), managed, rc, wrapper):
            original_mode = stat.S_IMODE(target.stat().st_mode)
            for mode in (0o664, 0o666):
                with self.subTest(target=target, mode=oct(mode)):
                    try:
                        target.chmod(mode)
                        before = self.snapshot()
                        for args in ((), ('--apply',)):
                            self.assertEqual(self.invoke(*args)[0], 1)
                            self.assertEqual(self.snapshot(), before)
                    finally:
                        target.chmod(original_mode)

    def test_permissions_changed_after_planning_refuse_initial_apply_preflight(self):
        self.install_fixture()
        wrapper = self.bin_dir / 'claude-usage'
        self.write(wrapper, migration.legacy_content(self.root))
        plan = migration.plan_migration
        for target in (self.bin_dir.parent, wrapper):
            original_mode = stat.S_IMODE(target.stat().st_mode)
            before_apply = None

            def unsafe_after_plan(bin_dir):
                nonlocal before_apply
                changes = plan(bin_dir)
                target.chmod(0o777 if target.is_dir() else 0o666)
                before_apply = self.snapshot()
                return changes

            with self.subTest(target=target):
                try:
                    with patch.object(migration, 'plan_migration', side_effect=unsafe_after_plan):
                        self.assertEqual(self.invoke('--apply')[0], 1)
                    self.assertEqual(self.snapshot(), before_apply)
                finally:
                    target.chmod(original_mode)

    def test_unsafe_ancestor_refuses_before_final_comparison_can_redirect_wrapper_delete(self):
        self.install_fixture(shells=('bash',))
        wrapper = self.bin_dir / 'claude-usage'
        self.write(wrapper, migration.legacy_content(self.root))
        wrapper.chmod(0o755)
        victim_directory = self.home / 'foreign-victim'
        victim = victim_directory / 'claude-usage'
        lock = victim_directory / 'usage-dashboard.lock'
        self.write(victim, 'FOREIGN VICTIM FILE\n')
        self.write(lock, 'foreign lock bytes\n')
        victim.chmod(0o640)
        lock.chmod(0o600)
        self.write(self.config / 'usage-dashboard/usage-dashboard.sqlite3', 'ledger bytes')
        self.bin_dir.parent.chmod(0o777)
        before = self.snapshot()
        regular = migration.regular
        for args in ((), ('--apply',)):
            comparisons = 0
            injected = False

            def redirect_after_comparison(path):
                nonlocal comparisons, injected
                content = regular(path)
                if path == wrapper:
                    comparisons += 1
                    if comparisons == 3:  # Plan, full preflight, final unlink comparison.
                        self.bin_dir.rename(self.bin_dir.parent / 'saved-bin')
                        self.bin_dir.symlink_to(victim_directory, target_is_directory=True)
                        injected = True
                return content

            with self.subTest(args=args):
                with patch.object(migration, 'regular', side_effect=redirect_after_comparison):
                    self.assertEqual(self.invoke(*args)[0], 1)
                self.assertFalse(injected)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual((victim.read_bytes(), stat.S_IMODE(victim.stat().st_mode)),
                                 (b'FOREIGN VICTIM FILE\n', 0o640))
                self.assertEqual((lock.read_bytes(), stat.S_IMODE(lock.stat().st_mode)),
                                 (b'foreign lock bytes\n', 0o600))

    def test_private_home_below_root_sticky_ancestor_accepts_read_only_and_executable_files(self):
        ancestor = self.home.parent.parent
        details = ancestor.stat()
        if details.st_uid != 0 or not details.st_mode & stat.S_ISVTX:
            self.skipTest('Disposable home is not below a root-owned sticky directory.')
        self.home.chmod(0o700)
        self.install_fixture(prefix='export CUSTOM=1\r\n', suffix='# after\r\n')
        wrapper = self.bin_dir / 'claude-usage'
        self.write(wrapper, migration.legacy_content(self.root))
        wrapper.chmod(0o755)
        migration.skill_path().chmod(0o400)
        migration.record_path().chmod(0o400)
        for shell in migration.SHELLS:
            managed, rc = migration.shell_paths(shell)
            managed.chmod(0o755)
            if rc is not None:
                rc.chmod(0o444)
        before = self.snapshot()
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.invoke('--apply')[0], 0)
        self.assertFalse(wrapper.exists())
        self.assertFalse(migration.record_path().exists())
        self.assertFalse(migration.skill_path().exists())
        for shell in migration.SHELLS:
            managed, rc = migration.shell_paths(shell)
            self.assertFalse(managed.exists())
            if rc is not None:
                self.assertEqual((rc.read_bytes(), stat.S_IMODE(rc.stat().st_mode)),
                                 (b'export CUSTOM=1\r\n# after\r\n', 0o444))


if __name__ == '__main__':
    unittest.main()
