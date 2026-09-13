"""Boundary tests for the DeepSeek fallback; no real credentials or provider calls."""
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

from usage_source import UsageSourceError, fetch_usage


class BalanceFallbackTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.redirect = False
        self.payload = {
            'is_available': True,
            'balance_infos': [
                {'currency': 'USD', 'total_balance': '12.50'},
                {'currency': 'CNY', 'total_balance': '8.25'},
            ],
        }
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append(self.path)
                if test.redirect:
                    self.send_response(302)
                    self.send_header('Location', '/credential-trap')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps(test.payload).encode())

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.fake_token = 'test-credential-never-real'
        for replacement in (
            patch('usage_source._host_usage', return_value={'reports': []}),
            patch('usage_source._deepseek_token', return_value=self.fake_token),
            patch('usage_source.BALANCE_URL', f'http://127.0.0.1:{self.server.server_port}/user/balance'),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_balances_preserve_currency_without_inventing_quota(self):
        report = fetch_usage('deepseek')['reports'][0]
        balances = {entry['label']: entry for entry in report['limits']}
        self.assertEqual(balances['Remaining balance (USD)']['amount']['remaining'], 12.5)
        self.assertEqual(balances['Remaining balance (CNY)']['amount']['remaining'], 8.25)
        for entry in balances.values():
            self.assertNotIn('usedFraction', entry['amount'])
            self.assertNotIn('limit', entry['amount'])
            self.assertNotIn('window', entry)

    def test_redirect_does_not_forward_credential(self):
        self.redirect = True
        with self.assertRaises(UsageSourceError) as caught:
            fetch_usage('deepseek')
        self.assertEqual(self.requests, ['/user/balance'])
        self.assertNotIn(self.fake_token, str(caught.exception))

    def test_nonfinite_balance_is_unavailable_not_a_zero(self):
        self.payload['balance_infos'][0]['total_balance'] = 'NaN'
        with self.assertRaises(UsageSourceError):
            fetch_usage('deepseek')


class CachedUsageTests(unittest.TestCase):
    def test_poll_observes_upstream_change_instead_of_cached_snapshot(self):
        cached = {'provider': 'openai-codex', 'fetchedAt': 1000, 'limits': [
            {'id': 'weekly', 'amount': {'usedFraction': .55}},
        ]}
        current = {'provider': 'openai-codex', 'fetchedAt': 200000, 'limits': [
            {'id': 'weekly', 'amount': {'usedFraction': .60}},
        ]}

        def broker(command, _timeout, _operation):
            nonlocal cached
            if 'invalidate' in command:
                cached = None
                return subprocess.CompletedProcess(command, 0, b'', b'')
            return subprocess.CompletedProcess(command, 0, json.dumps({'reports': [cached or current]}).encode(), b'')

        with patch('usage_source._run_omp', side_effect=broker):
            report = fetch_usage('codex')['reports'][0]
        self.assertEqual(report['fetchedAt'], 200000)
        self.assertEqual(report['limits'][0]['amount']['usedFraction'], .60)


if __name__ == '__main__':
    unittest.main()
