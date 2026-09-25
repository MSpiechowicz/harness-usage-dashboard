"""Disposable Git source updates without touching the active checkout or GitHub."""
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import claude_install
import updater


class ClaudeUpdaterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.origin = self.home / 'upstream.git'
        self.seed = self.home / 'seed'
        self.root = self.home / 'checkout'
        self.run_git(None, 'init', '--bare', str(self.origin))
        self.run_git(None, 'init', '-b', 'main', str(self.seed))
        self.run_git(self.seed, 'config', 'user.name', 'Fixture')
        self.run_git(self.seed, 'config', 'user.email', 'fixture@example.test')
        self.files(self.seed, '1.0.0')
        self.run_git(self.seed, 'add', '.')
        self.run_git(self.seed, 'commit', '-m', 'first')
        self.run_git(self.seed, 'remote', 'add', 'origin', str(self.origin))
        self.run_git(self.seed, 'push', 'origin', 'main')
        self.run_git(None, '--git-dir', str(self.origin), 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.run_git(None, 'clone', str(self.origin), str(self.root))
        self.run_git(self.root, 'remote', 'set-url', 'origin', 'https://github.com/MSpiechowicz/harness-usage-dashboard.git')
        self.before = self.run_git(self.root, 'rev-parse', 'HEAD')
        self.files(self.seed, '1.0.1')
        self.run_git(self.seed, 'add', '.')
        self.run_git(self.seed, 'commit', '-m', 'release')
        self.target = self.run_git(self.seed, 'rev-parse', 'HEAD')
        self.run_git(self.seed, 'tag', 'v1.0.1')
        self.run_git(self.seed, 'push', 'origin', 'main', 'v1.0.1')
        self.release = {'version': '1.0.1', 'tag': 'v1.0.1', 'url': updater.RELEASE_BASE + 'v1.0.1'}
        self.record = {'root': str(self.root), 'shells': {'bash': 'owned'}}
        self.calls = []
        self.real_refresh = updater.refresh_claude_installer
        self.real_attestation = updater._attested_tag
        # Replace only GitHub network transport; all Git inspection and merge remain real.
        def local_fetch(root, tag):
            updater.git(root, 'fetch', '--no-tags', '--no-recurse-submodules',
                        str(self.origin), f'refs/tags/{tag}', timeout=updater.FETCH_TIMEOUT)
        def local_attestation(tag):
            sha = self.run_git(None, '--git-dir', str(self.origin), 'rev-parse', f'refs/tags/{tag}')
            kind = self.run_git(None, '--git-dir', str(self.origin), 'cat-file', '-t', sha)
            return {'sha': sha, 'type': kind}

        for fixture in (
            patch.object(updater, '_fetch_claude_release', side_effect=local_fetch),
            patch.object(updater, '_attested_tag', side_effect=local_attestation),
            patch.object(updater, 'latest_release', return_value=self.release),
            patch.object(claude_install, 'validate_owned_installation', side_effect=self.validate),
            patch.object(updater, 'refresh_claude_installer', side_effect=self.refresh),
            patch.object(updater, 'agent_dir', side_effect=lambda profile=None, host='omp': self.home / host),
        ):
            fixture.start()
            self.addCleanup(fixture.stop)

    @staticmethod
    def run_git(root, *args):
        command = ['git'] + (['-C', str(root)] if root is not None else []) + list(args)
        return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()

    def files(self, root, version, *, package=None, catalog=None):
        (root / '.omp-plugin').mkdir(exist_ok=True)
        (root / 'package.json').write_text(json.dumps({
            'name': 'harness-usage-dashboard', 'version': package or version,
        }), encoding='utf-8')
        (root / '.omp-plugin/marketplace.json').write_text(json.dumps({
            'name': 'harness-usage-dashboard', 'plugins': [{
                'name': 'harness-usage-dashboard', 'version': catalog or version,
                'source': {'source': 'github', 'repo': updater.REPOSITORY, 'ref': 'v' + version},
            }],
        }), encoding='utf-8')
        for name in sorted(set(updater.REQUIRED_CLAUDE_FILES) | {'usage_source.py'}):
            (root / name).write_text('fixture\n', encoding='utf-8')

    def validate(self, *, root):
        if str(root) != self.record['root']:
            raise RuntimeError('Foreign installation')
        self.calls.append('validate')
        return deepcopy(self.record)

    def refresh(self, root):
        self.calls.append('refresh')
        return deepcopy(self.record)

    def assert_unmodified(self):
        self.assertEqual(self.run_git(self.root, 'rev-parse', 'HEAD'), self.before)
        self.assertEqual(updater.current_version(self.root), '1.0.0')
        self.assertNotIn('refresh', self.calls)

    def test_real_fast_forward_and_same_version_refresh(self):
        with patch.object(updater, 'native', side_effect=AssertionError('OMP invoked')):
            response = updater.install_claude_update(root=self.root)
            self.assertEqual(response['currentVersion'], '1.0.1')
            self.assertTrue(response['updated'])
            self.assertEqual(self.run_git(self.root, 'rev-parse', 'HEAD'), self.target)
            second = updater.install_claude_update(root=self.root)
        self.assertFalse(second['updated'])
        self.assertEqual(self.calls.count('refresh'), 2)

    def test_equal_version_local_commit_is_not_the_published_release(self):
        updater.install_claude_update(root=self.root)
        self.run_git(self.root, 'config', 'user.name', 'Fixture')
        self.run_git(self.root, 'config', 'user.email', 'fixture@example.test')
        (self.root / 'local.txt').write_text('not a published release', encoding='utf-8')
        self.run_git(self.root, 'add', 'local.txt')
        self.run_git(self.root, 'commit', '-m', 'local version-preserving change')
        local_head = self.run_git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(updater.UpdateError, 'published release'):
            updater.install_claude_update(root=self.root)
        self.assertEqual(self.run_git(self.root, 'rev-parse', 'HEAD'), local_head)
        self.assertEqual(self.calls.count('refresh'), 1)

    def test_refuses_dirty_tracked_staged_and_untracked(self):
        for name, action in (
            ('tracked', lambda: (self.root / 'package.json').write_text('changed')),
            ('staged', lambda: (self.root / 'package.json').write_text('changed')),
            ('untracked', lambda: (self.root / 'unknown').write_text('changed')),
        ):
            with self.subTest(name=name):
                action()
                if name == 'staged':
                    self.run_git(self.root, 'add', 'package.json')
                with self.assertRaises(updater.UpdateError):
                    updater.install_claude_update(root=self.root)
                self.assertEqual(self.run_git(self.root, 'rev-parse', 'HEAD'), self.before)
                self.run_git(self.root, 'reset', '--hard', 'HEAD')  # Disposable fixture only.
                (self.root / 'unknown').unlink(missing_ok=True)
        self.assertNotIn('refresh', self.calls)

    def test_wrong_origin_rewrite_branch_and_ownership(self):
        self.run_git(self.root, 'remote', 'set-url', 'origin', 'https://example.invalid/foreign.git')
        with self.assertRaisesRegex(updater.UpdateError, 'Origin'):
            updater.install_claude_update(root=self.root)
        self.run_git(self.root, 'remote', 'set-url', 'origin', 'https://github.com/MSpiechowicz/harness-usage-dashboard.git')
        self.run_git(self.root, 'config', '--local', 'url.https://example.invalid/.insteadOf', 'https://github.com/')
        with self.assertRaisesRegex(updater.UpdateError, 'Origin'):
            updater.install_claude_update(root=self.root)
        self.run_git(self.root, 'config', '--local', '--unset', 'url.https://example.invalid/.insteadOf')
        self.run_git(self.root, 'checkout', '-b', 'feature')
        with self.assertRaisesRegex(updater.UpdateError, 'main branch'):
            updater.install_claude_update(root=self.root)
        self.run_git(self.root, 'checkout', 'main')
        self.record['root'] = '/foreign'
        with self.assertRaisesRegex(RuntimeError, 'Foreign'):
            updater.install_claude_update(root=self.root)
        self.assert_unmodified()

    def test_api_attestation_rejects_forged_fast_forward_tag_before_merge(self):
        (self.seed / 'claude_install.py').write_text('forged installer\n', encoding='utf-8')
        self.run_git(self.seed, 'add', 'claude_install.py')
        self.run_git(self.seed, 'commit', '-m', 'forge tag while preserving release metadata')
        forged = self.run_git(self.seed, 'rev-parse', 'HEAD')
        self.run_git(self.seed, 'push', 'origin', 'main')
        self.run_git(None, '--git-dir', str(self.origin), 'update-ref', 'refs/tags/v1.0.1', forged)
        with patch.object(updater, '_attested_tag',
                          return_value={'sha': self.target, 'type': 'commit'}):
            with self.assertRaisesRegex(updater.UpdateError, 'attested'):
                updater.install_claude_update(root=self.root)
        self.assert_unmodified()

    def test_attestation_api_errors_refuse_without_advancing_checkout(self):
        failures = (
            (urllib.error.HTTPError('https://api.github.com/', 404, '', {}, None), 'missing'),
            (urllib.error.HTTPError('https://api.github.com/', 403, '', {}, None), 'rate limit'),
            (urllib.error.HTTPError('https://api.github.com/', 429, '', {}, None), 'rate limit'),
            (urllib.error.URLError('private network failure'), 'Cannot verify'),
        )
        for error, message in failures:
            with self.subTest(message=message), patch.object(
                    updater, '_attested_tag', self.real_attestation), patch.object(
                    updater.urllib.request, 'build_opener') as build:
                build.return_value.open.side_effect = error
                with self.assertRaisesRegex(updater.UpdateError, message):
                    updater.install_claude_update(root=self.root)
                self.assert_unmodified()

    def test_attestation_requires_exact_tag_ref_and_object_identity(self):
        objects = (
            {'ref': 'refs/tags/v9.9.9', 'object': {'sha': self.target, 'type': 'commit'}},
            {'ref': 'refs/tags/v1.0.1', 'object': {'sha': self.target, 'type': 'blob'}},
            {'ref': 'refs/tags/v1.0.1', 'object': {'sha': 'bad', 'type': 'commit'}},
        )
        for payload in objects:
            with self.subTest(payload=payload), patch.object(
                    updater, '_attested_tag', self.real_attestation), patch.object(
                    updater.urllib.request, 'build_opener') as build:
                build.return_value.open.return_value = io.BytesIO(json.dumps(payload).encode('utf-8'))
                with self.assertRaises(updater.UpdateError):
                    updater.install_claude_update(root=self.root)
                self.assert_unmodified()

    def test_bad_release_metadata_and_local_tag_conflict(self):
        self.run_git(self.root, 'tag', 'v1.0.1', self.before)
        with self.assertRaisesRegex(updater.UpdateError, 'tag conflicts'):
            updater.install_claude_update(root=self.root)
        self.run_git(self.root, 'tag', '-d', 'v1.0.1')
        for metadata in ({'version': '1.0.1', 'tag': 'v1.0.2', 'url': 'unused'},
                         {'version': '1.0.1-beta', 'tag': 'v1.0.1-beta', 'url': 'unused'}):
            with self.subTest(metadata=metadata), patch.object(updater, 'latest_release', return_value=metadata):
                with self.assertRaises(updater.UpdateError):
                    updater.install_claude_update(root=self.root)
        self.assert_unmodified()

    def test_catalog_mismatch_and_non_fast_forward(self):
        self.files(self.seed, '1.0.2', catalog='9.0.0')
        self.run_git(self.seed, 'add', '.')
        self.run_git(self.seed, 'commit', '-m', 'bad catalog')
        self.run_git(self.seed, 'tag', 'v1.0.2')
        self.run_git(self.seed, 'push', 'origin', 'v1.0.2')
        release = {'version': '1.0.2', 'tag': 'v1.0.2', 'url': updater.RELEASE_BASE + 'v1.0.2'}
        with patch.object(updater, 'latest_release', return_value=release):
            with self.assertRaisesRegex(updater.UpdateError, 'catalog'):
                updater.install_claude_update(root=self.root)
        self.run_git(self.root, 'config', 'user.name', 'Fixture')
        self.run_git(self.root, 'config', 'user.email', 'fixture@example.test')
        (self.root / 'local.txt').write_text('local', encoding='utf-8')
        self.run_git(self.root, 'add', '.')
        self.run_git(self.root, 'commit', '-m', 'local commit')
        local_head = self.run_git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(updater.UpdateError, 'fast-forward'):
            updater.install_claude_update(root=self.root)
        self.assertEqual(self.run_git(self.root, 'rev-parse', 'HEAD'), local_head)

    def test_annotated_release_tag_fast_forwards_to_verified_commit(self):
        self.files(self.seed, '1.0.2')
        self.run_git(self.seed, 'add', '.')
        self.run_git(self.seed, 'commit', '-m', 'annotated release')
        self.run_git(self.seed, '-c', 'user.name=Fixture', '-c',
                     'user.email=fixture@example.test', 'tag', '-a', 'v1.0.2', '-m', 'release')
        self.run_git(self.seed, 'push', 'origin', 'v1.0.2')
        release = {'version': '1.0.2', 'tag': 'v1.0.2', 'url': updater.RELEASE_BASE + 'v1.0.2'}
        with patch.object(updater, 'latest_release', return_value=release):
            response = updater.install_claude_update(root=self.root)
        self.assertTrue(response['updated'])
        self.assertEqual(self.run_git(self.root, 'rev-parse', 'HEAD'),
                         self.run_git(self.seed, 'rev-parse', 'HEAD'))

    def test_new_checkout_installer_runs_in_fresh_process(self):
        self.files(self.seed, '1.0.2')
        (self.seed / 'claude_install.py').write_text(chr(10).join((
            'import os', 'from pathlib import Path',
            "Path(os.environ['CLAUDE_CONFIG_DIR']).mkdir(parents=True, exist_ok=True)",
            "(Path(os.environ['CLAUDE_CONFIG_DIR']) / 'new-installer-ran').write_text('yes')",
        )) + chr(10), encoding='utf-8')
        self.run_git(self.seed, 'add', '.')
        self.run_git(self.seed, 'commit', '-m', 'updated installer')
        self.run_git(self.seed, 'tag', 'v1.0.2')
        self.run_git(self.seed, 'push', 'origin', 'v1.0.2')
        release = {'version': '1.0.2', 'tag': 'v1.0.2', 'url': updater.RELEASE_BASE + 'v1.0.2'}
        config = self.home / 'claude-config'
        output = io.StringIO()
        with patch.object(updater, 'latest_release', return_value=release), (
                patch.object(updater, 'refresh_claude_installer', self.real_refresh)), (
                patch.object(updater, 'ROOT', self.root)), (
                patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(config))), (
                contextlib.redirect_stdout(output)):
            self.assertEqual(updater.main(['install', '--host', 'claude']), 0)
        self.assertTrue(json.loads(output.getvalue())['updated'])
        self.assertEqual((config / 'new-installer-ran').read_text(encoding='utf-8'), 'yes')

    def test_package_mismatch_refuses_before_merge(self):
        self.files(self.seed, '1.0.2', package='9.0.0')
        self.run_git(self.seed, 'add', '.')
        self.run_git(self.seed, 'commit', '-m', 'bad package')
        self.run_git(self.seed, 'tag', 'v1.0.2')
        self.run_git(self.seed, 'push', 'origin', 'v1.0.2')
        release = {'version': '1.0.2', 'tag': 'v1.0.2', 'url': updater.RELEASE_BASE + 'v1.0.2'}
        with patch.object(updater, 'latest_release', return_value=release):
            with self.assertRaisesRegex(updater.UpdateError, 'catalog'):
                updater.install_claude_update(root=self.root)
        self.assert_unmodified()

    def test_missing_release_runtime_file_refuses_before_merge(self):
        self.files(self.seed, '1.0.2')
        (self.seed / 'claude_bridge.py').unlink()
        self.run_git(self.seed, 'add', '-A')
        self.run_git(self.seed, 'commit', '-m', 'missing Claude runtime')
        self.run_git(self.seed, 'tag', 'v1.0.2')
        self.run_git(self.seed, 'push', 'origin', 'v1.0.2')
        release = {'version': '1.0.2', 'tag': 'v1.0.2', 'url': updater.RELEASE_BASE + 'v1.0.2'}
        with patch.object(updater, 'latest_release', return_value=release):
            with self.assertRaisesRegex(updater.UpdateError, 'missing regular'):
                updater.install_claude_update(root=self.root)
        self.assert_unmodified()

    def test_missing_shared_usage_source_refuses_before_checkout_advances(self):
        self.files(self.seed, '1.0.2')
        (self.seed / 'usage_source.py').unlink()
        self.run_git(self.seed, 'add', '-A')
        self.run_git(self.seed, 'commit', '-m', 'release missing shared usage source')
        self.run_git(self.seed, 'tag', 'v1.0.2')
        self.run_git(self.seed, 'push', 'origin', 'v1.0.2')
        release = {'version': '1.0.2', 'tag': 'v1.0.2', 'url': updater.RELEASE_BASE + 'v1.0.2'}
        with patch.object(updater, 'latest_release', return_value=release):
            with self.assertRaisesRegex(updater.UpdateError, 'missing regular'):
                updater.install_claude_update(root=self.root)
        self.assert_unmodified()
        self.assertTrue((self.root / 'usage_source.py').is_file())

    def test_git_failure_after_advancement_reports_partial_state(self):
        original = updater.git

        def interrupted(root, *args, **kwargs):
            response = original(root, *args, **kwargs)
            if args[0] == 'merge':
                raise updater.UpdateError('merge command interrupted after checkout')
            return response

        with patch.object(updater, 'git', side_effect=interrupted):
            with self.assertRaises(updater.PartialClaudeUpdate) as caught:
                updater.install_claude_update(root=self.root)
        self.assertFalse(caught.exception.response['updated'])
        self.assertEqual(caught.exception.response['currentVersion'], '1.0.1')
        self.assertEqual(self.run_git(self.root, 'rev-parse', 'HEAD'), self.target)
        self.assertNotIn('refresh', self.calls)

    def test_fetch_failure_and_concurrent_changes_refuse_before_merge(self):
        with patch.object(updater, '_fetch_claude_release', side_effect=updater.UpdateError('fetch failed')):
            with self.assertRaisesRegex(updater.UpdateError, 'fetch failed'):
                updater.install_claude_update(root=self.root)
        self.assert_unmodified()

        original = updater._release_commit

        def mutate_owner(root, release):
            commit = original(root, release)
            self.record['shells']['bash'] = 'changed'
            return commit

        with patch.object(updater, '_release_commit', side_effect=mutate_owner):
            with self.assertRaisesRegex(updater.UpdateError, 'integration changed'):
                updater.install_claude_update(root=self.root)
        self.assert_unmodified()

        self.record['shells']['bash'] = 'owned'
        def advance_head(root, release):
            commit = original(root, release)
            self.run_git(self.root, 'config', 'user.name', 'Fixture')
            self.run_git(self.root, 'config', 'user.email', 'fixture@example.test')
            self.run_git(self.root, 'merge', '--ff-only', commit)
            return commit

        with patch.object(updater, '_release_commit', side_effect=advance_head):
            with self.assertRaises(updater.PartialClaudeUpdate) as caught:
                updater.install_claude_update(root=self.root)
        self.assertFalse(caught.exception.response['updated'])
        self.assertEqual(self.run_git(self.root, 'rev-parse', 'HEAD'), self.target)

    def test_checkout_lock_is_shared_across_config_roots(self):
        with updater.claude_update_lock(self.root):
            with self.assertRaisesRegex(updater.UpdateError, 'Another Claude checkout update'):
                updater.install_claude_update(root=self.root)
        self.assert_unmodified()

    def test_refresh_failure_reports_actual_state_and_retries_same_version(self):
        with patch.object(updater, 'refresh_claude_installer', side_effect=RuntimeError('failed')):
            with self.assertRaises(updater.PartialClaudeUpdate) as caught:
                updater.install_claude_update(root=self.root)
        response = caught.exception.response
        self.assertFalse(response['updated'])
        self.assertEqual(response['currentVersion'], '1.0.1')
        self.assertIn('--refresh', response['message'])
        self.assertEqual(self.run_git(self.root, 'rev-parse', 'HEAD'), self.target)
        with patch.object(updater, 'refresh_claude_installer', side_effect=RuntimeError('failed')):
            with self.assertRaises(updater.PartialClaudeUpdate):
                updater.install_claude_update(root=self.root)
        self.assertFalse(updater.install_claude_update(root=self.root)['updated'])

    def test_cli_install_success_and_partial_failure_json(self):
        output = io.StringIO()
        with patch.object(updater, 'ROOT', self.root), contextlib.redirect_stdout(output):
            with patch.object(updater, 'refresh_claude_installer', side_effect=RuntimeError('failed')):
                self.assertEqual(updater.main(['install', '--host', 'claude']), 1)
        partial = json.loads(output.getvalue())
        self.assertFalse(partial['updated'])
        self.assertEqual(partial['currentVersion'], '1.0.1')
        self.assertEqual(self.run_git(self.root, 'rev-parse', 'HEAD'), self.target)

        output = io.StringIO()
        with patch.object(updater, 'ROOT', self.root), contextlib.redirect_stdout(output):
            self.assertEqual(updater.main(['install', '--host', 'claude']), 0)
        repaired = json.loads(output.getvalue())
        self.assertFalse(repaired['updated'])
        self.assertEqual(repaired['currentVersion'], '1.0.1')

    def test_check_rejects_foreign_ownership_before_network_or_cache(self):
        self.record['root'] = '/foreign'
        with patch.object(updater, 'latest_release') as release:
            with self.assertRaisesRegex(RuntimeError, 'Foreign'):
                updater.check_claude_update(root=self.root)
            release.assert_not_called()
        self.assertFalse((self.home / 'claude/usage-dashboard-update.json').exists())

    def test_no_release_and_network_failure_do_not_mutate(self):
        with patch.object(updater, 'latest_release', return_value=None):
            response = updater.install_claude_update(root=self.root)
        self.assertFalse(response['updated'])
        with patch.object(updater, 'latest_release', side_effect=updater.UpdateError('offline')):
            with self.assertRaisesRegex(updater.UpdateError, 'offline'):
                updater.install_claude_update(root=self.root)
        self.assert_unmodified()

    def test_claude_check_host_cache_isolation_and_profile_refusal(self):
        with patch.object(updater, 'native', side_effect=AssertionError('OMP invoked')):
            response = updater.check_claude_update(root=self.root)
        self.assertTrue(response['updateAvailable'])
        self.assertTrue((self.home / 'claude/usage-dashboard-update.json').exists())
        self.assertFalse((self.home / 'omp/usage-dashboard-update.json').exists())
        output = io.StringIO()
        with patch.object(updater, 'ROOT', self.root), contextlib.redirect_stdout(output):
            self.assertEqual(updater.main(['check', '--host', 'claude']), 0)
        self.assertEqual(json.loads(output.getvalue())['currentVersion'], '1.0.0')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            updater.main(['check', '--host', 'claude', '--profile', 'work'])


if __name__ == '__main__':
    unittest.main()
