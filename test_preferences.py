"""Persistence boundaries; all settings live in a temporary home."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from preferences import (DEFAULTS, agent_dir, load_preferences, migrate_history_visibility,
                         preferences_path, resolve_profile, update_preferences)


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
            'previous_visible': False,
            'history_other_visible': False,
            'history_total_visible': True,
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
                        b'{"windows": {"deepseek": "daily"}}', b'\xff'):
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
