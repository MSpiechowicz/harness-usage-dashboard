"""Protect non-interactive CLI use and distinguish option values from commands."""
import unittest

from launcher import should_wrap


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


if __name__ == '__main__':
    unittest.main()
