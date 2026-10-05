"""Persistence boundaries; all settings live in a temporary home."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

from preferences import (CHART_TYPES, DEFAULTS, agent_dir, load_preferences,
                         migrate_history_visibility, preferences_path, resolve_profile,
                         update_preferences)


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {'HOME': str(self.home)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_fresh_profile_requires_explicit_provider_selection(self):
        self.assertEqual(load_preferences(None)['providers'], [])
        self.assertEqual(load_preferences(None)['theme'], 'green')
        self.assertEqual(load_preferences(None)['chart_type'], 'bars')
        self.assertEqual(load_preferences(None)['tokens'], {})
        update_preferences(None, {'side': 'left'})
        self.assertEqual(load_preferences(None)['providers'], [])
        update_preferences(None, {'providers': ['anthropic']})
        self.assertEqual(load_preferences(None)['providers'], ['anthropic'])
        update_preferences(None, {'providers': []})
        self.assertEqual(load_preferences(None)['providers'], [])

    def test_persisted_settings_survive_load_without_transient_fields(self):
        changes = {
            'providers': ['openai-codex', 'deepseek'],
            'hidden': ['deepseek'],
            'windows': {'openai-codex': ['weekly', '7 day']},
            'side': 'left',
            'compact': False,
            'commands_visible': False,
            'rate_visible': False,
            'current_visible': False,
            'previous_visible': False,
            'history_other_visible': False,
            'history_total_visible': True,
            'interval': 15,
            'enabled': False,
            'theme': 'blue',
            'chart_type': 'trace',
            'tokens': {'accent': '#58a66a', 'warn': 'orange'},
        }
        update_preferences(None, dict(changes, profile='other', refresh=123))
        self.assertEqual(load_preferences(None), changes)
        stored = json.loads(preferences_path(None).read_text())
        self.assertEqual(stored, changes)
        self.assertEqual(stat.S_IMODE(preferences_path(None).stat().st_mode), 0o600)

    def test_patch_preserves_changes_since_an_earlier_session_load(self):
        session = load_preferences(None)
        update_preferences(None, {'windows': {'openai-codex': ['weekly']}, 'interval': 120})
        session['side'] = 'left'
        update_preferences(None, {'side': session['side']})
        result = load_preferences(None)
        self.assertEqual(result['windows'], {'openai-codex': ['weekly']})
        self.assertEqual(result['interval'], 120)
        self.assertEqual(result['side'], 'left')

    def test_concurrent_transforms_preserve_each_window_filter(self):
        update_preferences(None, {'providers': ['anthropic']})
        code = (
            'import sys; from preferences import transform_preferences; '
            'print(\"ready\", flush=True); '
            'transform_preferences(None, lambda config: '
            '{**config, \"windows\": {**config[\"windows\"], \"anthropic\": '
            'config[\"windows\"].get(\"anthropic\", []) + [sys.argv[1]]}})'
        )
        processes = []
        # Hold the real lock until every worker has reached its mutation.
        from preferences import _locked
        with _locked(preferences_path()):
            for index in range(8):
                process = subprocess.Popen(
                    [sys.executable, '-c', code, f'window-{index}'],
                    cwd=Path(__file__).resolve().parent, env=dict(os.environ),
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                processes.append(process)
            for process in processes:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
        for process in processes:
            output, errors = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, output + errors)
        saved = load_preferences(None)
        self.assertEqual(set(saved['windows']['anthropic']), {f'window-{index}' for index in range(8)})
        self.assertEqual(saved['providers'], ['anthropic'])

    def test_symlinked_preferences_or_lock_never_modify_target(self):
        path = preferences_path()
        path.parent.mkdir(parents=True)
        target = self.home / 'foreign.json'
        target.write_text('unchanged')
        for alias in (path, path.with_suffix('.lock')):
            with self.subTest(alias=alias):
                if alias.exists():
                    alias.unlink()
                alias.symlink_to(target)
                try:
                    with self.assertRaises(OSError):
                        update_preferences(None, {'theme': 'blue'})
                    self.assertEqual(target.read_text(), 'unchanged')
                finally:
                    alias.unlink()

    def test_first_use_directory_race_cannot_change_another_hosts_preferences(self):
        for kind in ('symlink', 'writable'):
            with self.subTest(kind=kind):
                target = self.home / f'victim-{kind}'
                storage = target / 'nested' / 'usage-dashboard'
                with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(storage)}):
                    update_preferences(None, {'theme': 'blue', 'providers': ['openai-codex']})
                original = (storage / 'usage-dashboard.json').read_bytes()
                original_lock = (storage / 'usage-dashboard.lock').read_bytes()
                directory = self.home / f'first-use-{kind}'
                injected = False
                mkdir = Path.mkdir

                def inject(candidate, mode=0o777, parents=False, exist_ok=False):
                    nonlocal injected
                    if candidate == directory and not injected:
                        injected = True
                        if kind == 'symlink':
                            directory.symlink_to(target, target_is_directory=True)
                        else:
                            target.rename(directory)
                            directory.chmod(0o777)
                    return mkdir(candidate, mode=mode, parents=parents, exist_ok=exist_ok)

                with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(directory / 'nested')}):
                    with patch.object(Path, 'mkdir', inject):
                        with self.assertRaises(PermissionError):
                            update_preferences(None, {'theme': 'red'}, host='claude')
                observed = target if kind == 'symlink' else directory
                saved = observed / 'nested' / 'usage-dashboard' / 'usage-dashboard.json'
                self.assertEqual(saved.read_bytes(), original)
                self.assertEqual(saved.with_suffix('.lock').read_bytes(), original_lock)
                if kind == 'writable':
                    self.assertEqual(directory.stat().st_mode & 0o777, 0o777)

    def test_raced_directory_symlink_cannot_create_preference_children_in_target(self):
        directory = self.home / 'first-use'
        target = self.home / 'untouched'
        target.mkdir()
        marker = target / 'marker'
        marker.write_bytes(b'unchanged')
        mkdir = Path.mkdir
        injected = False

        def inject(candidate, mode=0o777, parents=False, exist_ok=False):
            nonlocal injected
            if candidate == directory and not injected:
                injected = True
                directory.symlink_to(target, target_is_directory=True)
            return mkdir(candidate, mode=mode, parents=parents, exist_ok=exist_ok)

        with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(directory / 'nested' / 'agent')}):
            with patch.object(Path, 'mkdir', inject):
                with self.assertRaises(PermissionError):
                    update_preferences(None, {'theme': 'blue'})
        self.assertEqual(marker.read_bytes(), b'unchanged')
        self.assertEqual(set(target.iterdir()), {marker})

    def test_missing_preference_directories_are_private_even_with_open_umask(self):
        trusted = self.home / 'trusted'
        trusted.mkdir(mode=0o750)
        original_mode = stat.S_IMODE(trusted.stat().st_mode)
        directory = trusted / 'missing' / 'nested' / 'agent'
        previous_umask = os.umask(0)
        try:
            with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(directory)}):
                update_preferences(None, {'theme': 'blue'})
                self.assertEqual(load_preferences(None)['theme'], 'blue')
        finally:
            os.umask(previous_umask)
        for component in (trusted / 'missing', directory.parent, directory):
            self.assertEqual(stat.S_IMODE(component.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(trusted.stat().st_mode), original_mode)

    def test_retired_images_request_is_ignored_without_rewriting_other_preferences(self):
        path = preferences_path('work')
        path.parent.mkdir(parents=True)
        for old_value in (True, False):
            with self.subTest(old_value=old_value):
                saved = json.dumps({'images_enabled': old_value, 'side': 'left',
                                    'providers': ['openai-codex'], 'theme': 'blue'})
                path.write_text(saved)

                loaded = load_preferences('work')
                self.assertEqual(path.read_text(), saved)
                self.assertNotIn('images_enabled', loaded)
                self.assertEqual((loaded['side'], loaded['providers'], loaded['theme']),
                                 ('left', ['openai-codex'], 'blue'))

                changed = update_preferences('work', {'interval': 90})
                self.assertNotIn('images_enabled', changed)
                self.assertNotIn('images_enabled', json.loads(path.read_text()))
                self.assertEqual((changed['side'], changed['providers'], changed['theme'],
                                  changed['interval']),
                                 ('left', ['openai-codex'], 'blue', 90))

    def test_retired_images_field_is_not_accepted_in_new_writes(self):
        with self.assertRaises(ValueError):
            update_preferences(None, {'images_enabled': True})

    def test_legacy_history_visibility_migrates_without_defaults_or_mutation(self):
        legacy = {'history_visible': False}
        self.assertEqual(migrate_history_visibility({}), {})
        self.assertEqual(migrate_history_visibility(legacy), {
            'history_other_visible': False,
            'history_total_visible': False,
        })
        self.assertEqual(legacy, {'history_visible': False})
        self.assertEqual(migrate_history_visibility({'history_visible': True}), {
            'history_other_visible': True,
            'history_total_visible': True,
        })
        self.assertEqual(migrate_history_visibility({
            'history_visible': True,
            'history_other_visible': False,
            'history_total_visible': False,
        }), {
            'history_other_visible': False,
            'history_total_visible': False,
        })

    def test_history_visibility_rejects_non_boolean_legacy_values(self):
        for value in (0, 1, 'false', None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    migrate_history_visibility({'history_visible': value})

    def test_history_visibility_migrates_independently_and_remains_profile_local(self):
        path = preferences_path(None)
        path.parent.mkdir(parents=True)
        path.write_text('{"compact": false, "history_visible": false}')
        loaded = load_preferences(None)
        self.assertFalse(loaded['history_other_visible'])
        self.assertFalse(loaded['history_total_visible'])
        update_preferences(None, {'history_other_visible': True})
        self.assertEqual(
            (load_preferences(None)['history_other_visible'], load_preferences(None)['history_total_visible']),
            (True, False),
        )
        stored = json.loads(path.read_text())
        self.assertNotIn('history_visible', stored)
        self.assertEqual(
            (stored['history_other_visible'], stored['history_total_visible']),
            (True, False),
        )
        self.assertTrue(load_preferences('other')['history_other_visible'])
        self.assertTrue(load_preferences('other')['history_total_visible'])
        update_preferences('other', {'history_total_visible': False})
        self.assertTrue(load_preferences('other')['history_other_visible'])
        self.assertFalse(load_preferences('other')['history_total_visible'])
        self.assertTrue(load_preferences(None)['history_other_visible'])
        self.assertFalse(load_preferences(None)['history_total_visible'])

    def test_malformed_file_is_neither_hidden_nor_overwritten(self):
        path = preferences_path(None)
        path.parent.mkdir(parents=True)
        for content in (b'{broken', b'[]', b'{"interval": true}', b'{"history_visible": 1}',
                        b'{"windows": {"deepseek": "daily"}}', b'{"chart_type": "pie"}',
                        b'{"chart_type": null}', b'\xff'):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    load_preferences(None)
                with self.assertRaises(ValueError):
                    update_preferences(None, {'side': 'left'})
                self.assertEqual(path.read_bytes(), content)

    def test_invalid_patches_preserve_existing_settings(self):
        update_preferences(None, {'enabled': False})
        path = preferences_path(None)
        original = path.read_bytes()
        invalid = (
            {'providers': 'deepseek'}, {'providers': ['']}, {'hidden': [False]},
            {'windows': {'deepseek': [' ']}}, {'side': 'bottom'},
            {'theme': 'violet'}, {'tokens': {'unknown': 'green'}},
            {'chart_type': 'pie'}, {'chart_type': 'LINE'}, {'chart_type': None},
            {'chart_type': 0}, {'chart_type': ['line']},
            *({'chart_type': mode} for mode in ('line', 'area', 'heatmap', 'lollipop')),
            {'tokens': {'accent': '#12345'}}, {'tokens': {'warn': 'not-a-color'}},
            {'compact': 1}, {'enabled': 'false'}, {'interval': True},
            {'commands_visible': 'false'}, {'previous_visible': 0},
            {'history_other_visible': 1}, {'history_total_visible': 'false'},
            {'history_visible': 0}, {'interval': 14}, {'interval': 15.5},
            {'apiKey': 'not-a-setting'},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    update_preferences(None, changes)
                self.assertEqual(path.read_bytes(), original)

    def test_chart_modes_round_trip_and_legacy_files_default_to_bars(self):
        self.assertEqual(CHART_TYPES, ('bars', 'dots', 'trace'))
        path = preferences_path('work')
        path.parent.mkdir(parents=True)
        path.write_text('{"side": "left"}')
        self.assertEqual(load_preferences('work')['chart_type'], 'bars')
        self.assertEqual(path.read_text(), '{"side": "left"}')

        update_preferences('work', {'chart_type': 'bars'})
        self.assertEqual(path.read_text(), '{"side": "left"}')
        for chart_type in ('dots', 'trace', 'bars'):
            with self.subTest(chart_type=chart_type):
                update_preferences('work', {'chart_type': chart_type})
                self.assertEqual(load_preferences('work')['chart_type'], chart_type)
                self.assertEqual(json.loads(path.read_text())['chart_type'], chart_type)
                self.assertEqual(load_preferences('work')['side'], 'left')

    def test_retired_chart_modes_migrate_on_read_and_persist_on_next_update(self):
        path = preferences_path('work')
        path.parent.mkdir(parents=True)
        for mode in ('line', 'area', 'heatmap', 'lollipop'):
            with self.subTest(mode=mode):
                original = json.dumps({'chart_type': mode, 'side': 'left',
                                       'history_visible': False}).encode()
                path.write_bytes(original)
                loaded = load_preferences('work')
                self.assertEqual(loaded['chart_type'], 'bars')
                self.assertEqual(loaded['side'], 'left')
                self.assertFalse(loaded['history_other_visible'])
                self.assertFalse(loaded['history_total_visible'])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(update_preferences('work', {}), loaded)
                self.assertEqual(path.read_bytes(), original)

                with self.assertRaises(ValueError):
                    update_preferences('work', {'chart_type': mode})
                self.assertEqual(path.read_bytes(), original)
                updated = update_preferences('work', {'side': 'right'})
                self.assertEqual(updated['chart_type'], 'bars')
                self.assertEqual(updated['side'], 'right')
                self.assertEqual(json.loads(path.read_text())['chart_type'], 'bars')


    def test_chart_mode_is_separate_per_profile_and_host(self):
        update_preferences('work', {'chart_type': 'trace'})
        update_preferences('personal', {'chart_type': 'dots'})
        update_preferences(None, {'chart_type': 'trace'}, host='claude')
        self.assertEqual(load_preferences('work')['chart_type'], 'trace')
        self.assertEqual(load_preferences('personal')['chart_type'], 'dots')
        self.assertEqual(load_preferences('default')['chart_type'], 'bars')
        self.assertEqual(load_preferences(None, host='claude')['chart_type'], 'trace')

    def test_profiles_are_isolated_and_explicit_default_overrides_environment(self):
        custom = self.home / 'custom-agent'
        with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(custom), 'PI_PROFILE': 'personal'}):
            self.assertEqual(resolve_profile(None), 'personal')
            update_preferences(None, {'side': 'left'})
            with patch.dict(os.environ, {'OMP_PROFILE': 'work'}):
                self.assertEqual(resolve_profile(None), 'work')
                update_preferences(None, {'interval': 90})
                update_preferences('', {'enabled': False})
                self.assertEqual(agent_dir('default'), custom)
                self.assertEqual(agent_dir('work'), self.home / '.omp/profiles/work/agent')
                self.assertEqual(load_preferences('personal')['side'], 'left')
                self.assertEqual(load_preferences('personal')['interval'], 60)
                self.assertEqual(load_preferences('work')['interval'], 90)
                self.assertEqual(load_preferences('work')['side'], 'right')
                self.assertFalse(load_preferences('default')['enabled'])
                self.assertTrue(load_preferences('work')['enabled'])
            with patch.dict(os.environ, {'OMP_PROFILE': ''}):
                self.assertEqual(resolve_profile(None), 'default')

    def test_profile_traversal_is_rejected_before_creating_paths(self):
        for profile in ('..', '.', '../outside', '/absolute', 'a/b', 'a\\b', 'bad\x00name'):
            with self.subTest(profile=profile):
                with self.assertRaises(ValueError):
                    update_preferences(profile, {'enabled': False})
        self.assertFalse((self.home / '.omp').exists())

    def test_claude_preferences_are_dashboard_owned_and_ignore_omp_profile_settings(self):
        claude_root = self.home / 'claude-config'
        claude_root.mkdir()
        settings = claude_root / 'settings.json'
        settings.write_text('{"hooks": {"SessionStart": []}}')
        omp_root = self.home / 'custom-agent'
        with patch.dict(os.environ, {
            'CLAUDE_CONFIG_DIR': str(claude_root), 'OMP_PROFILE': 'work',
            'PI_PROFILE': 'personal', 'PI_CODING_AGENT_DIR': str(omp_root),
        }):
            expected = claude_root / 'usage-dashboard' / 'usage-dashboard.json'
            self.assertEqual(agent_dir('../ignored', host='claude'), expected.parent)
            self.assertEqual(preferences_path('work', host='claude'), expected)
            self.assertEqual(load_preferences(None, host='claude')['providers'], ['anthropic'])
            self.assertFalse(expected.exists())
            update_preferences('work', {'side': 'left'}, host='claude')
            self.assertEqual(load_preferences('personal', host='claude')['side'], 'left')
            self.assertEqual(load_preferences(None, host='claude')['providers'], ['anthropic'])
            self.assertEqual(load_preferences('work')['side'], 'right')
            self.assertEqual(settings.read_text(), '{"hooks": {"SessionStart": []}}')
            self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o600)

        with patch.dict(os.environ, {'OMP_PROFILE': 'work', 'PI_CODING_AGENT_DIR': str(omp_root)}):
            self.assertEqual(agent_dir(None, host='claude'), self.home / '.claude/usage-dashboard')
            self.assertEqual(load_preferences(None, host='claude')['providers'], ['anthropic'])
            self.assertFalse((self.home / '.claude/usage-dashboard/usage-dashboard.json').exists())

    def test_claude_named_theme_round_trips_independently_per_host(self):
        omp_path = preferences_path(None)
        claude_path = preferences_path(None, host='claude')
        self.assertNotEqual(omp_path, claude_path)
        self.assertEqual(load_preferences(None)['theme'], 'green')
        self.assertEqual(load_preferences(None, host='claude')['theme'], 'green')

        update_preferences(None, {'theme': 'claude', 'tokens': {'accent': '#123456'}})
        self.assertEqual(load_preferences(None, host='claude')['theme'], 'green')
        self.assertEqual(load_preferences(None, host='claude')['tokens'], {})

        update_preferences(None, {'theme': 'claude', 'tokens': {'text': '#abcdef'}},
                           host='claude')
        self.assertEqual(load_preferences(None)['theme'], 'claude')
        self.assertEqual(load_preferences(None)['tokens'], {'accent': '#123456'})
        self.assertEqual(load_preferences(None, host='claude')['theme'], 'claude')
        self.assertEqual(load_preferences(None, host='claude')['tokens'], {'text': '#abcdef'})
        self.assertEqual(json.loads(omp_path.read_text())['theme'], 'claude')
        self.assertEqual(json.loads(claude_path.read_text())['theme'], 'claude')

        update_preferences(None, {'theme': 'green'}, host='claude')
        self.assertEqual(load_preferences(None, host='claude')['theme'], 'green')
        self.assertEqual(load_preferences(None, host='claude')['tokens'], {'text': '#abcdef'})
        self.assertEqual(load_preferences(None)['theme'], 'claude')
        self.assertEqual(load_preferences(None)['tokens'], {'accent': '#123456'})

    def test_claude_provider_override_and_invalid_host(self):
        update_preferences(None, {'providers': []}, host='claude')
        self.assertEqual(load_preferences(None, host='claude')['providers'], [])
        self.assertEqual(load_preferences(None)['providers'], [])
        with self.assertRaises(ValueError):
            load_preferences(None, host='unknown')

    def test_default_and_loaded_mutable_values_are_detached(self):
        first = load_preferences(None)
        first['providers'].append('deepseek')
        first['hidden'].append('openai-codex')
        first['windows']['openai-codex'] = ['weekly']
        self.assertEqual(load_preferences(None), DEFAULTS)
        patterns = ['weekly']
        saved = update_preferences(None, {'windows': {'openai-codex': patterns}})
        patterns.append('daily')
        saved['windows']['openai-codex'].append('hourly')
        self.assertEqual(load_preferences(None)['windows'], {'openai-codex': ['weekly']})


if __name__ == '__main__':
    unittest.main()
