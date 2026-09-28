"""Protect CLI routing and OMP's process-local image protocol selection."""
import os
import sys
import unittest
from unittest.mock import patch

from launcher import (await_client_and_exec, main, select_image_environment,
                      should_wrap)


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



class SixelSelectionTests(unittest.TestCase):
    @staticmethod
    def tmux_reply(environment, *arguments):
        if arguments[0] == 'list-clients':
            return 'work|RGB,sixel,256\n'
        return '1\n'

    def test_capable_attached_client_selects_omp_sixel_without_changing_parent(self):
        parent = {'TMUX': '/tmp/tmux-socket,11,0', 'TMUX_PANE': '%11',
                  'PI_ALLOW_SIXEL_PASSTHROUGH': '0'}
        with patch('launcher._tmux_query', side_effect=self.tmux_reply):
            selected = select_image_environment(parent.copy())
        self.assertEqual(selected['PI_FORCE_IMAGE_PROTOCOL'], 'sixel')
        self.assertEqual(selected['PI_ALLOW_SIXEL_PASSTHROUGH'], '0')
        self.assertNotIn('PI_FORCE_IMAGE_PROTOCOL', parent)

        with patch('launcher._tmux_query', side_effect=self.tmux_reply):
            selected = select_image_environment({'TMUX': parent['TMUX'], 'TMUX_PANE': '%11'})
        self.assertEqual(selected['PI_ALLOW_SIXEL_PASSTHROUGH'], '1')

    def test_unknown_or_mixed_capabilities_never_force_protocol(self):
        for clients, support in (
                ('', '1\n'),
                ('work|RGB,sixel\nwork|RGB\n', '1\n'),
                ('work|RGB,sixel\n', '0\n'),
                ('work|RGB,sixel\n', ''),
                ('work|RGB,sixel\n', None),
                ('work|not-sixel\n', '1\n'),
                ('broken-client\n', '1\n')):
            with self.subTest(clients=clients, support=support):
                def reply(_environment, *arguments):
                    return clients if arguments[0] == 'list-clients' else support

                with patch('launcher._tmux_query', side_effect=reply):
                    result = select_image_environment(
                        {'TMUX': '/tmp/socket,1,0', 'TMUX_PANE': '%1'})
                self.assertNotIn('PI_FORCE_IMAGE_PROTOCOL', result)
                self.assertNotIn('PI_ALLOW_SIXEL_PASSTHROUGH', result)

    def test_explicit_protocol_and_passthrough_are_preserved(self):
        for force, passthrough in (('', '0'), ('kitty', '1'), ('kitty', None),
                                   ('sixel', '0'), ('sixel', '1'),
                                   (' SIXEL ', 'custom'), ('SiXeL', '0')):
            with self.subTest(force=force, passthrough=passthrough):
                initial = {'TMUX': '/tmp/socket,1,0', 'TMUX_PANE': '%1',
                           'PI_FORCE_IMAGE_PROTOCOL': force}
                if passthrough is not None:
                    initial['PI_ALLOW_SIXEL_PASSTHROUGH'] = passthrough
                with patch('launcher._tmux_query', side_effect=AssertionError('no probe')):
                    selected = select_image_environment(initial)
                self.assertEqual(selected['PI_FORCE_IMAGE_PROTOCOL'], force)
                self.assertEqual(selected.get('PI_ALLOW_SIXEL_PASSTHROUGH'), passthrough)

    def test_forced_sixel_does_not_default_permission_without_verified_capabilities(self):
        for clients, support in (
                ('', '1\n'),
                ('work|RGB\n', '1\n'),
                ('work|RGB,sixel\n', '0\n'),
                ('work|RGB,sixel\n', None)):
            with self.subTest(clients=clients, support=support):
                def reply(_environment, *arguments):
                    return clients if arguments[0] == 'list-clients' else support

                initial = {'TMUX': '/tmp/socket,1,0', 'TMUX_PANE': '%1',
                           'PI_FORCE_IMAGE_PROTOCOL': ' SIXEL '}
                with patch('launcher._tmux_query', side_effect=reply):
                    selected = select_image_environment(initial)
                self.assertEqual(selected['PI_FORCE_IMAGE_PROTOCOL'], ' SIXEL ')
                self.assertNotIn('PI_ALLOW_SIXEL_PASSTHROUGH', selected)

    def test_forced_sixel_defaults_permission_only_with_verified_capabilities(self):
        initial = {'TMUX': '/tmp/socket,1,0', 'TMUX_PANE': '%1',
                   'PI_FORCE_IMAGE_PROTOCOL': 'SiXeL'}
        with patch('launcher._tmux_query', side_effect=self.tmux_reply):
            selected = select_image_environment(initial)
        self.assertEqual(selected['PI_FORCE_IMAGE_PROTOCOL'], 'SiXeL')
        self.assertEqual(selected['PI_ALLOW_SIXEL_PASSTHROUGH'], '1')

    def test_new_pane_does_not_start_omp_before_client_attaches(self):
        checks = iter(([], [], [{'sixel'}], [{'sixel'}]))
        events = []

        def clients(_environment):
            events.append('check')
            return next(checks)

        def sleep(_duration):
            events.append('wait')

        def execute(_binary, _command, environment):
            events.append('omp')
            self.assertEqual(environment['PI_FORCE_IMAGE_PROTOCOL'], 'sixel')
            self.assertEqual(environment['PI_ALLOW_SIXEL_PASSTHROUGH'], '1')

        with patch.dict(os.environ, {'TMUX': '/tmp/socket,1,0', 'TMUX_PANE': '%1'}, clear=True), \
             patch('launcher._attached_features', side_effect=clients), \
             patch('launcher._tmux_query', return_value='1\n'), \
             patch('launcher.time.sleep', side_effect=sleep), \
             patch('launcher.os.execvpe', side_effect=execute):
            await_client_and_exec(['env', 'OMP_USAGE_LAUNCHER=1', '/omp', '--profile', 'work'])
        self.assertEqual(events, ['check', 'wait', 'check', 'wait', 'check', 'check', 'omp'])

    def test_unknown_capability_does_not_delay_omp_indefinitely(self):
        seen = []
        with patch.dict(os.environ, {'TMUX': '/tmp/socket,1,0', 'TMUX_PANE': '%1'}, clear=True), \
             patch('launcher._attached_features', return_value=[]), \
             patch('launcher._tmux_query', return_value='1\n'), \
             patch('launcher.time.monotonic', side_effect=[0, 9]), \
             patch('launcher.time.sleep', side_effect=AssertionError('timeout reached')), \
             patch('launcher.os.execvpe', side_effect=lambda _binary, _command, env: seen.append(env)):
            await_client_and_exec(['env', '/omp'])
        self.assertEqual(len(seen), 1)
        self.assertNotIn('PI_FORCE_IMAGE_PROTOCOL', seen[0])

    def test_queries_the_pane_socket_and_fails_closed_on_tmux_error(self):
        environment = {'TMUX': '/tmp/right-socket,77,0', 'TMUX_PANE': '%77'}

        def respond(command, **_kwargs):
            self.assertEqual(command[1:3], ['-S', '/tmp/right-socket'])
            raise OSError('unavailable')

        with patch('dashboard.tmux_binary', return_value='/tmux'), \
             patch('launcher.subprocess.check_output', side_effect=respond):
            selected = select_image_environment(environment)
        self.assertNotIn('PI_FORCE_IMAGE_PROTOCOL', selected)
        self.assertNotIn('PI_ALLOW_SIXEL_PASSTHROUGH', selected)

    def test_comma_in_socket_path_still_uses_the_panes_capabilities(self):
        environment = {'TMUX': '/tmp/right,socket,77,0', 'TMUX_PANE': '%77'}

        def respond(command, **_kwargs):
            self.assertEqual(command[1:3], ['-S', '/tmp/right,socket'])
            return 'work|RGB,sixel\n' if command[3] == 'list-clients' else '1\n'

        with patch('dashboard.tmux_binary', return_value='/tmux'), \
             patch('launcher.subprocess.check_output', side_effect=respond):
            selected = select_image_environment(environment.copy())
        self.assertEqual(selected['PI_FORCE_IMAGE_PROTOCOL'], 'sixel')
        self.assertEqual(selected['PI_ALLOW_SIXEL_PASSTHROUGH'], '1')
        self.assertNotIn('PI_FORCE_IMAGE_PROTOCOL', environment)

    def test_capable_session_ignores_other_clients_on_the_same_socket(self):
        sessions = {'%11': 'work', '%22': 'other'}
        clients = [('work', 'RGB,sixel'), ('work', 'sixel'),
                   ('other', 'RGB')]

        def respond(command, **_kwargs):
            if command[3] == 'display-message':
                return '1\n'
            target = command[command.index('-t') + 1] if '-t' in command else None
            session = sessions.get(target)
            return ''.join(f'{name}|{features}\n' for name, features in clients
                           if session is None or name == session)

        with patch('dashboard.tmux_binary', return_value='/tmux'), \
             patch('launcher.subprocess.check_output', side_effect=respond):
            work = select_image_environment(
                {'TMUX': '/tmp/socket,11,0', 'TMUX_PANE': '%11'})
            other = select_image_environment(
                {'TMUX': '/tmp/socket,22,0', 'TMUX_PANE': '%22'})

        self.assertEqual(work['PI_FORCE_IMAGE_PROTOCOL'], 'sixel')
        self.assertEqual(work['PI_ALLOW_SIXEL_PASSTHROUGH'], '1')
        self.assertNotIn('PI_FORCE_IMAGE_PROTOCOL', other)
        self.assertNotIn('PI_ALLOW_SIXEL_PASSTHROUGH', other)

    def test_detached_session_ignores_capable_client_in_other_session(self):
        def respond(command, **_kwargs):
            if command[3] == 'display-message':
                return '1\n'
            if '-t' not in command:
                return 'other|RGB,sixel\n'
            return ''

        with patch('dashboard.tmux_binary', return_value='/tmux'), \
             patch('launcher.subprocess.check_output', side_effect=respond):
            selected = select_image_environment(
                {'TMUX': '/tmp/socket,11,0', 'TMUX_PANE': '%11',
                 'PI_FORCE_IMAGE_PROTOCOL': 'sixel'})

        self.assertEqual(selected['PI_FORCE_IMAGE_PROTOCOL'], 'sixel')
        self.assertNotIn('PI_ALLOW_SIXEL_PASSTHROUGH', selected)

    def test_missing_or_invalid_pane_never_selects_or_grants_sixel(self):
        for pane in (None, '', '1', '%', '%-1', '%1;exit', '%١'):
            with self.subTest(pane=pane):
                environment = {'TMUX': '/tmp/socket,1,0',
                               'PI_FORCE_IMAGE_PROTOCOL': 'sixel'}
                if pane is not None:
                    environment['TMUX_PANE'] = pane
                with patch('launcher._tmux_query', return_value='work|RGB,sixel\n'):
                    selected = select_image_environment(environment)
                self.assertEqual(selected['PI_FORCE_IMAGE_PROTOCOL'], 'sixel')
                self.assertNotIn('PI_ALLOW_SIXEL_PASSTHROUGH', selected)

    def test_existing_tmux_runs_omp_with_selected_process_environment(self):
        seen = []

        def execute(_binary, _args, environment):
            seen.append(environment)
            raise SystemExit(0)

        with patch.dict(os.environ, {'TMUX': '/tmp/socket,1,0', 'TMUX_PANE': '%1'}, clear=True), \
             patch.object(sys, 'argv', ['launcher.py', '--native', '--model', 'selected']), \
             patch('launcher.should_wrap', return_value=True), \
             patch('launcher.shutil.which', return_value='/omp'), \
             patch('preferences.resolve_profile', return_value='work'), \
             patch('launcher._tmux_query', side_effect=self.tmux_reply), \
             patch('launcher.os.execve', side_effect=execute):
            with self.assertRaises(SystemExit):
                main()
            self.assertNotIn('PI_FORCE_IMAGE_PROTOCOL', os.environ)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['OMP_PROFILE'], 'work')
        self.assertEqual(seen[0]['PI_FORCE_IMAGE_PROTOCOL'], 'sixel')
        self.assertEqual(seen[0]['PI_ALLOW_SIXEL_PASSTHROUGH'], '1')


if __name__ == '__main__':
    unittest.main()
