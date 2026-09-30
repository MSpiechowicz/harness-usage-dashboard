"""Protect interactive CLI routing."""
import unittest
from unittest.mock import patch

from launcher import select_image_environment, should_wrap


class LauncherRoutingTests(unittest.TestCase):
    def test_noninteractive_and_protocol_modes_never_open_tmux(self):
        self.assertFalse(should_wrap([], False))
        self.assertFalse(should_wrap(['--print', 'explain this code'], True))
        self.assertFalse(should_wrap(['--mode=rpc'], True))
        self.assertFalse(should_wrap(['usage', '--json'], True))
        self.assertFalse(should_wrap(['update'], True))
        self.assertFalse(should_wrap(['--help'], True))

    def test_option_values_and_prompt_words_are_not_cli_commands(self):
        self.assertTrue(should_wrap(['--system-prompt', 'usage', 'explain', 'usage'], True))
        self.assertTrue(should_wrap(['--system-prompt', '--print'], True))
        self.assertTrue(should_wrap(['--continue'], True))
        self.assertTrue(should_wrap(['--profile', 'work', '--resume'], True))
        self.assertFalse(should_wrap(['--profile', 'work', 'usage'], True))


class ImageEnvironmentTests(unittest.TestCase):
    def test_sixel_requires_build_and_every_attached_client(self):
        cases = (
            ('work|RGB,sixel\n', '1', True),
            ('work|sixel\nwork|RGB,sixel\n', '1', True),
            ('work|sixel\nwork|RGB\n', '1', False),
            ('work|RGB\n', '1', False),
            ('work|sixel\n', '0', False),
            ('work|sixel\n', None, False),
            ('', '1', False),
            (None, '1', False),
            ('malformed', '1', False),
        )
        for clients, support, enabled in cases:
            with self.subTest(clients=clients, support=support):
                environment = {'TMUX': '/isolated/socket,1,0', 'TMUX_PANE': '%2'}
                def query(_environment, command, *args):
                    return clients if command == 'list-clients' else support
                with patch('launcher._tmux_query', side_effect=query):
                    selected = select_image_environment(environment)
                self.assertEqual(selected.get('PI_FORCE_IMAGE_PROTOCOL'),
                                 'sixel' if enabled else None)
                self.assertEqual(selected.get('PI_ALLOW_SIXEL_PASSTHROUGH'),
                                 '1' if enabled else None)

    def test_explicit_image_choices_are_preserved(self):
        overrides = (
            {'PI_FORCE_IMAGE_PROTOCOL': 'off'},
            {'PI_FORCE_IMAGE_PROTOCOL': 'kitty'},
            {'PI_FORCE_IMAGE_PROTOCOL': ''},
            {'PI_FORCE_IMAGE_PROTOCOL': 'sixel', 'PI_ALLOW_SIXEL_PASSTHROUGH': '0'},
            {'PI_ALLOW_SIXEL_PASSTHROUGH': '0'},
        )
        for override in overrides:
            with self.subTest(override=override):
                environment = {'TMUX': '/isolated/socket,1,0', 'TMUX_PANE': '%2', **override}
                with patch('launcher._tmux_query', side_effect=lambda _env, command, *args:
                           'work|sixel\n' if command == 'list-clients' else '1'):
                    selected = select_image_environment(environment)
                for key, value in override.items():
                    self.assertEqual(selected[key], value)


if __name__ == '__main__':
    unittest.main()
