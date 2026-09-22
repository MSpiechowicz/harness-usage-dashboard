"""Persistence boundaries; all settings live in a temporary home."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from preferences import DEFAULTS, agent_dir, load_preferences, preferences_path, resolve_profile, update_preferences


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
            'interval': 15,
            'enabled': False,
            'theme': 'blue',
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

    def test_commands_visibility_upgrades_old_settings_and_remains_profile_local(self):
        path = preferences_path(None)
        path.parent.mkdir(parents=True)
        path.write_text('{"compact": false}')
        self.assertTrue(load_preferences(None)['commands_visible'])
        update_preferences(None, {'commands_visible': False})
        self.assertFalse(load_preferences(None)['commands_visible'])
        self.assertTrue(load_preferences('other')['commands_visible'])
        update_preferences(None, {'commands_visible': True})
        self.assertTrue(load_preferences(None)['commands_visible'])
        self.assertFalse(load_preferences(None)['compact'])

    def test_malformed_file_is_neither_hidden_nor_overwritten(self):
        path = preferences_path(None)
        path.parent.mkdir(parents=True)
        for content in (b'{broken', b'[]', b'{"interval": true}', b'{"windows": {"deepseek": "daily"}}', b'\xff'):
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
            {'tokens': {'accent': '#12345'}}, {'tokens': {'warn': 'not-a-color'}},
            {'compact': 1}, {'enabled': 'false'}, {'interval': True},
            {'commands_visible': 'false'},
            {'interval': 14}, {'interval': 15.5}, {'apiKey': 'not-a-setting'},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    update_preferences(None, changes)
                self.assertEqual(path.read_bytes(), original)

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
