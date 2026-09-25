"""Consumer-facing routing, merge, lifecycle, and telemetry precedence contracts."""

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import claude_launcher as launcher


class ClaudeRoutingTests(unittest.TestCase):
    def test_interactive_prompts_and_resume_flags_wrap(self):
        for arguments in ([], ['hello'], ['review this'], ['--continue'],
                          ['--resume', 'session-id'], ['-r'],
                          ['--session-id', '3c6c6200-785d-400c-9000-54edc31473f0'],
                          ['--settings', '{"note":"--print"}', 'hello']):
            with self.subTest(arguments=arguments):
                self.assertTrue(launcher.should_wrap(arguments, True))
                self.assertFalse(launcher.should_wrap(arguments, False))

    def test_noninteractive_modes_and_commands_bypass(self):
        for arguments in (['--print', 'hello'], ['-p'], ['--help'], ['--version'],
                          ['update'], ['mcp', 'list'], ['--background'],
                          ['--tools', 'Bash'], ['--unrecognized', 'hello'],
                          ['--session-id']):
            with self.subTest(arguments=arguments):
                self.assertFalse(launcher.should_wrap(arguments, True))

    def test_noninteractive_passthrough_is_exact(self):
        original = ['--print', '--settings', '{"apiKeyHelper":"secret"}', 'hello']
        with mock.patch.object(launcher.shutil, 'which', return_value='/usr/bin/claude'), \
                mock.patch.object(launcher, 'should_wrap', return_value=False), \
                mock.patch.object(launcher.os, 'execv', side_effect=SystemExit) as execv:
            with self.assertRaises(SystemExit):
                launcher.main(original)
        execv.assert_called_once_with('/usr/bin/claude', ['/usr/bin/claude', *original])

    def test_custom_claude_config_respects_managed_telemetry_and_status_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'managed-settings.json').write_text(json.dumps({
                'env': {'OTEL_EXPORTER_OTLP_ENDPOINT': 'https://collector.example'},
                'statusLine': {'type': 'command', 'command': 'custom-status'},
            }))
            with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': directory}):
                self.assertEqual(launcher._existing_settings(), (True, True))


class FakeReceiver:
    def __init__(self):
        self.active = False
        self.auth = 'private-auth-token'

    def __enter__(self):
        self.active = True
        return self

    def __exit__(self, *_):
        self.active = False

    def settings(self, include_status_line=True):
        settings = {'hooks': {
            'SessionStart': [{'hooks': [{'type': 'command', 'command': 'receive-start'}]}],
            'SessionEnd': [{'hooks': [{'type': 'command', 'command': 'receive-end'}]}],
        }}
        if include_status_line:
            settings['statusLine'] = {'type': 'command', 'command': 'receive-status'}
        return settings

    def environment(self):
        return {'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT': 'http://127.0.0.1:4001/v1/logs',
                'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://127.0.0.1:4001',
                'OTEL_EXPORTER_OTLP_LOGS_HEADERS': 'Authorization=Bearer ' + self.auth,
                'CLAUDE_USAGE_BRIDGE_AUTH': self.auth,
                'CLAUDE_USAGE_BRIDGE_URL': 'http://127.0.0.1:4001'}


class ClaudeLaunchTests(unittest.TestCase):
    def setUp(self):
        self.receiver = FakeReceiver()
        self.patchers = [
            mock.patch('claude_bridge.start_receiver', return_value=self.receiver),
            mock.patch.object(launcher, '_existing_settings', return_value=(False, False)),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _launch(self, arguments, *, environment=None):
        seen = {}

        def launch(args, argv):
            settings_arg = next((item.partition('=')[2] for item in argv
                                 if item.startswith('--settings=')), None)
            if settings_arg is None:
                settings_arg = argv[argv.index('--settings') + 1]
            path = Path(settings_arg)
            seen.update(args=args, argv=list(argv), settings=json.loads(path.read_text()),
                        settings_file=path, file_mode=path.stat().st_mode & 0o777,
                        environment=os.environ.copy(), receiver_active=self.receiver.active)
            return 17

        selected = launcher._settings_argument(arguments)
        original = launcher._read_settings(selected[2]) if selected else {}
        with mock.patch('dashboard.launch', side_effect=launch), \
                mock.patch.dict(os.environ, environment or {}, clear=True):
            result = launcher._run_wrapped('/usr/bin/claude', arguments, selected, original)
        self.assertEqual(result, 17)
        self.assertFalse(self.receiver.active)
        self.assertFalse(seen['settings_file'].exists())
        self.assertTrue(seen['receiver_active'])
        self.assertEqual(seen['args'].host, 'claude')
        self.assertEqual(seen['file_mode'], 0o600)
        self.assertNotIn(self.receiver.auth, ' '.join(seen['argv']))
        return seen

    def test_per_invocation_settings_preserve_hooks_statusline_and_argv(self):
        original_settings = {'statusLine': {'type': 'command', 'command': 'user-status'},
                             'hooks': {'SessionStart': [{'hooks': [{'type': 'command',
                                                                   'command': 'user-hook'}]}],
                                       'PostToolUse': [{'hooks': []}]},
                             'env': {'UNRELATED': 'keep-me'}, 'permissions': {'defaultMode': 'plan'}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps(original_settings))
            argv = ['--continue', '--settings', str(path), '--session-id',
                    '3c6c6200-785d-400c-9000-54edc31473f0']
            seen = self._launch(argv)
            self.assertEqual(path.read_text(), json.dumps(original_settings))
        self.assertEqual(seen['argv'][:2], argv[:2])
        self.assertEqual(seen['argv'][3:], argv[3:])
        self.assertEqual(seen['settings']['statusLine'], original_settings['statusLine'])
        self.assertEqual(seen['settings']['permissions'], original_settings['permissions'])
        self.assertEqual(seen['settings']['env'], original_settings['env'])
        self.assertEqual([entry['hooks'][0]['command'] for entry in
                          seen['settings']['hooks']['SessionStart']], ['user-hook', 'receive-start'])
        self.assertEqual(seen['settings']['hooks']['PostToolUse'], original_settings['hooks']['PostToolUse'])
        self.assertEqual(seen['environment']['CLAUDE_USAGE_BRIDGE_AUTH'], self.receiver.auth)

    def test_inline_settings_and_existing_user_statusline_are_preserved(self):
        with mock.patch.object(launcher, '_existing_settings', return_value=(True, False)):
            seen = self._launch(['--settings={"env":{"OTHER":"value"}}', 'hello'])
        self.assertEqual(seen['argv'][1], 'hello')
        self.assertNotIn('statusLine', seen['settings'])
        self.assertEqual(seen['settings']['env'], {'OTHER': 'value'})

    def test_inline_secrets_move_from_cli_argument_into_private_settings_file(self):
        inline = '{\"apiKeyHelper\":\"/bin/echo private-user-setting\"}'
        seen = self._launch(['--settings=' + inline, '--continue'])
        self.assertEqual(seen['settings']['apiKeyHelper'], '/bin/echo private-user-setting')
        self.assertNotIn('private-user-setting', ' '.join(seen['argv']))

    def test_existing_otel_destinations_are_never_replaced(self):
        for key in ('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', 'OTEL_EXPORTER_OTLP_ENDPOINT'):
            with self.subTest(key=key):
                destination = 'https://collector.example/v1/logs'
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    seen = self._launch(['--continue'], environment={key: destination})
                self.assertEqual(seen['environment'][key], destination)
                self.assertNotIn('OTEL_EXPORTER_OTLP_LOGS_HEADERS', seen['environment'])
                self.assertIn('token capture unavailable', stderr.getvalue())
                self.assertEqual(seen['environment']['CLAUDE_USAGE_BRIDGE_AUTH'], self.receiver.auth)

    def test_managed_telemetry_settings_keep_destination(self):
        with mock.patch.object(launcher, '_existing_settings', return_value=(True, True)):
            seen = self._launch(['--continue'])
        self.assertNotIn('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', seen['environment'])
        self.assertNotIn('statusLine', seen['settings'])
        self.assertIn('SessionStart', seen['settings']['hooks'])

    def test_unmergeable_settings_fall_back_without_leaking_receiver(self):
        argv = ['--settings', '{"hooks":{"SessionStart":"not-a-list"}}', 'hello']
        called = []
        def run(command, **_):
            called.append((list(command), self.receiver.active))
            return mock.Mock(returncode=4)
        with mock.patch('claude_launcher.subprocess.run', side_effect=run), \
                mock.patch.dict(os.environ, {}, clear=True):
            code = launcher._run_wrapped('/usr/bin/claude', argv,
                                         launcher._settings_argument(argv),
                                         launcher._read_settings(argv[1]))
        self.assertEqual(code, 4)
        self.assertEqual(called, [(['/usr/bin/claude', *argv], True)])
        self.assertFalse(self.receiver.active)

    def test_existing_tmux_runs_claude_in_same_pane_without_exec(self):
        argv = ['--resume', 'session-id']
        captured = {}
        def start(command, **kwargs):
            captured.update(command=list(command), environment=kwargs['env'],
                            receiver_active=self.receiver.active)
            return mock.Mock(wait=lambda **_: 9)
        with mock.patch.dict(os.environ, {'TMUX': '/tmp/tmux-socket,1,0', 'TMUX_PANE': '%7'}, clear=True), \
                mock.patch('dashboard.control') as control, \
                mock.patch('claude_launcher.subprocess.Popen', side_effect=start):
            code = launcher._run_wrapped('/usr/bin/claude', argv, None, {})
        self.assertEqual(code, 9)
        self.assertTrue(captured['receiver_active'])
        self.assertEqual(captured['command'][:3], ['/usr/bin/claude', '--resume', 'session-id'])
        self.assertEqual(captured['environment']['CLAUDE_USAGE_BRIDGE_AUTH'], self.receiver.auth)
        self.assertNotIn(self.receiver.auth, ' '.join(captured['command']))
        self.assertFalse(self.receiver.active)
        self.assertEqual(control.call_args.args[1], ['init'])

    def test_tmux_launch_errors_never_echo_prompt_or_private_settings(self):
        class InteractiveOutput(io.StringIO):
            def isatty(self):
                return True

        prompt = 'prompt-canary-do-not-print'
        settings_secret = 'settings-canary-do-not-print'
        argv = [f'--settings={{"apiKeyHelper":"{settings_secret}"}}', prompt]
        failures = (
            subprocess.CalledProcessError(1, ['tmux', 'new-session', *argv, self.receiver.auth]),
            OSError(2, 'No such file or directory', settings_secret),
            ValueError('Install tmux 3.3 or newer.'),
        )
        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                stdout = InteractiveOutput()
                stderr = io.StringIO()
                with mock.patch.object(launcher.shutil, 'which', return_value='/usr/bin/claude'), \
                        mock.patch.object(sys.stdin, 'isatty', return_value=True), \
                        mock.patch('dashboard.launch', side_effect=failure), \
                        mock.patch.dict(os.environ, {}, clear=True), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = launcher.main(argv)
                self.assertEqual(code, 1)
                self.assertIn('dashboard unavailable', stderr.getvalue())
                if isinstance(failure, ValueError):
                    self.assertIn('Install tmux 3.3 or newer.', stderr.getvalue())
                for secret in (prompt, settings_secret, self.receiver.auth):
                    self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
                self.assertFalse(self.receiver.active)


    def test_existing_tmux_sidebar_failure_does_not_echo_command(self):
        prompt = 'pane-prompt-canary-do-not-print'
        failure = subprocess.CalledProcessError(
            1, ['tmux', 'split-window', prompt, self.receiver.auth])
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {'TMUX': '/tmp/tmux-socket,1,0', 'TMUX_PANE': '%7'},
                             clear=True), \
                mock.patch('dashboard.control', side_effect=failure), \
                mock.patch('claude_launcher.subprocess.Popen',
                           return_value=mock.Mock(wait=lambda **_: 9)), \
                contextlib.redirect_stderr(stderr):
            code = launcher._run_wrapped('/usr/bin/claude', [prompt], None, {})
        self.assertEqual(code, 9)
        self.assertIn('sidebar unavailable', stderr.getvalue())
        self.assertNotIn(prompt, stderr.getvalue())
        self.assertNotIn(self.receiver.auth, stderr.getvalue())

    def test_interrupt_reaps_existing_pane_child_before_receiver_closes(self):
        events = []
        child = mock.Mock()
        child.wait.side_effect = [KeyboardInterrupt, 130]
        child.poll.return_value = None
        child.terminate.side_effect = lambda: events.append('terminated')
        with mock.patch('claude_launcher.subprocess.Popen', return_value=child):
            with self.assertRaises(KeyboardInterrupt):
                launcher._run_in_current_pane(['/usr/bin/claude'], {})
        self.assertEqual(events, ['terminated'])
        self.assertEqual(child.wait.call_count, 2)


if __name__ == '__main__':
    unittest.main()
