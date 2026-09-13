"""Offline release-cache and native-upgrade ownership boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import updater


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / 'installed-1.0.0'
        self.package(self.root, '1.0.0')
        self.release = {'version': '1.0.1', 'tag': 'v1.0.1', 'url': updater.RELEASE_BASE + 'v1.0.1'}
        self.clock = 100000
        for fixture in (
            patch.object(updater, 'agent_dir', side_effect=lambda profile: self.home / (profile or 'default')),
            patch.object(updater.time, 'time', side_effect=lambda: self.clock),
        ):
            fixture.start()
            self.addCleanup(fixture.stop)

    def package(self, root, version):
        root.mkdir(parents=True, exist_ok=True)
        (root / 'package.json').write_text(json.dumps({'name': 'oh-my-pi-usage-dashboard', 'version': version}), encoding='utf-8')

    def summary(self, root=None, scope='user', version='1.0.0'):
        return {'id': updater.PLUGIN_ID, 'scope': scope, 'entries': [
            {'scope': scope, 'installPath': str(root or self.root), 'version': version},
        ]}

    def test_cached_release_recompares_installed_version_and_expires_at_24_hours(self):
        with patch.object(updater, 'latest_release', return_value=self.release) as fetch:
            self.assertTrue(updater.check('work', cached=True, root=self.root)['updateAvailable'])
            self.package(self.root, '1.0.1')
            self.clock += updater.CACHE_SECONDS - 1
            self.assertFalse(updater.check('work', cached=True, root=self.root)['updateAvailable'])
            self.assertEqual(fetch.call_count, 1)
            self.clock += 1
            updater.check('work', cached=True, root=self.root)
            self.assertEqual(fetch.call_count, 2)
            updater.check('other', cached=True, root=self.root)
            self.assertEqual(fetch.call_count, 3)

    def test_failed_refresh_never_hides_update_for_24_hours(self):
        with patch.object(updater, 'latest_release', side_effect=[updater.UpdateError('offline'), self.release]):
            with self.assertRaises(updater.UpdateError):
                updater.check('work', cached=True, root=self.root)
            self.assertFalse(updater.cache_path('work').exists())
            self.assertTrue(updater.check('work', cached=True, root=self.root)['updateAvailable'])

    def test_cache_cannot_supply_untrusted_release_links(self):
        malicious = dict(self.release, url='https://untrusted.invalid/download')
        updater.write_cache('work', malicious)
        with patch.object(updater, 'latest_release', return_value=None):
            response = updater.check('work', cached=True, root=self.root)
        self.assertIsNone(response['latestVersion'])
        self.assertIsNone(response['releaseUrl'])
        self.assertFalse(response['updateAvailable'])

    def test_no_releases_is_cached_but_explicit_check_refreshes(self):
        with patch.object(updater, 'latest_release', side_effect=[None, self.release]) as fetch:
            response = updater.check('work', cached=True, root=self.root)
            self.assertIsNone(response['latestVersion'])
            self.assertFalse(response['updateAvailable'])
            updater.check('work', cached=True, root=self.root)
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue(updater.check('work', root=self.root)['updateAvailable'])

    def test_semantic_comparison_and_prerelease_rejection(self):
        self.assertTrue(updater.result('1.9.9', dict(self.release, version='1.10.0'))['updateAvailable'])
        self.assertFalse(updater.result('2.0.0', self.release)['updateAvailable'])
        with self.assertRaises(updater.UpdateError):
            updater.version_tuple('1.0.1-beta.1')

    def test_public_404_is_no_release_but_rate_limit_is_error(self):
        with patch.object(updater.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = urllib.error.HTTPError(updater.RELEASE_API, 404, '', {}, None)
            self.assertIsNone(updater.latest_release())
            build.return_value.open.side_effect = urllib.error.HTTPError(updater.RELEASE_API, 403, '', {}, None)
            with self.assertRaises(updater.UpdateError):
                updater.latest_release()

    def test_legacy_checkout_and_wrong_registered_path_cannot_trigger_native_writes(self):
        other = self.home / 'other-checkout'
        self.package(other, '1.0.0')
        for listing in ([], [self.summary(other)]):
            with self.subTest(listing=listing), patch.object(updater, 'native', return_value=json.dumps({'marketplace': listing})) as native:
                with self.assertRaises(updater.UpdateError):
                    updater.install_update('work', self.root)
                self.assertEqual([call.args[1] for call in native.call_args_list], ['list'])
        self.assertEqual(updater.current_version(self.root), '1.0.0')

    def test_shadowed_or_ambiguous_installation_is_not_an_update_target(self):
        shadowed = dict(self.summary(), shadowedBy='project')
        ambiguous = self.summary()
        ambiguous['entries'] *= 2
        for listing in ([shadowed], [ambiguous]):
            with self.subTest(listing=listing), patch.object(updater, 'installed_plugins', return_value=listing):
                with self.assertRaises(updater.UpdateError):
                    updater.managed_install('work', self.root)

    def test_registry_version_mismatch_is_refused_before_network(self):
        with patch.object(updater, 'installed_plugins', return_value=[self.summary(version='1.0.2')]), \
                patch.object(updater, 'latest_release') as fetch:
            with self.assertRaises(updater.UpdateError):
                updater.install_update('work', self.root)
            fetch.assert_not_called()

    def native_fixture(self, *, unchanged=False, race=False):
        """Model native ownership: only a fully-qualified single scope can mutate."""
        self.registry = [self.summary(scope='project')]
        self.upgraded = self.home / 'installed-1.0.1'
        self.package(self.upgraded, '1.0.1')

        def native(profile, *args, **kwargs):
            if args == ('list', '--json'):
                return json.dumps({'npm': [], 'marketplace': self.registry})
            if args == ('marketplace', 'update', updater.MARKETPLACE):
                if race:
                    self.registry[0]['entries'][0]['installPath'] = str(self.upgraded)
                    self.registry[0]['entries'][0]['version'] = '1.0.1'
                return ''
            if args == ('upgrade', updater.PLUGIN_ID, '--scope', 'project'):
                if not unchanged:
                    self.registry[0] = self.summary(self.upgraded, scope='project', version='1.0.1')
                return ''
            raise AssertionError(f'Unscoped or unexpected native mutation: {args}')

        return patch.object(updater, 'native', side_effect=native)

    def test_successful_native_exit_without_new_version_is_not_success(self):
        with self.native_fixture(unchanged=True), patch.object(updater, 'latest_release', return_value=self.release):
            with self.assertRaises(updater.UpdateError):
                updater.install_update('work', self.root)

    def test_concurrent_native_installation_change_stops_upgrade(self):
        with self.native_fixture(race=True), patch.object(updater, 'latest_release', return_value=self.release):
            with self.assertRaises(updater.UpdateError):
                updater.install_update('work', self.root)
        self.assertEqual(updater.current_version(self.root), '1.0.0')


if __name__ == '__main__':
    unittest.main()
