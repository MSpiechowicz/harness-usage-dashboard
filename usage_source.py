#!/usr/bin/env python3
"""Account usage from OMP, with an official DeepSeek balance fallback."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


ALIASES = {
    'codex': 'openai-codex',
    'claude': 'anthropic',
    'copilot': 'github-copilot',
    'gemini': 'google-gemini-cli',
    'grok': 'xai',
}
USAGE_TIMEOUT = 25
TOKEN_TIMEOUT = 10
HTTP_TIMEOUT = 10
BALANCE_URL = 'https://api.deepseek.com/user/balance'
MAX_BALANCE_BYTES = 128 * 1024


class UsageSourceError(RuntimeError):
    """A safe, fixed-message error suitable for display without a traceback."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials, including to a different origin or HTTP.
        raise UsageSourceError('DeepSeek balance endpoint redirected; request refused.')


def _omp_command(profile: str | None) -> list[str]:
    command = [shutil.which('omp') or str(Path.home() / '.local/bin/omp')]
    if profile is not None:
        if not isinstance(profile, str) or not profile or '\x00' in profile:
            raise UsageSourceError('Invalid OMP profile name.')
        command += ['--profile', profile]
    return command


def _run_omp(command: list[str], timeout: int, operation: str):
    try:
        return subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        # TimeoutExpired includes captured stdout (possibly a credential).
        raise UsageSourceError(f'OMP {operation} timed out.') from None
    except OSError:
        raise UsageSourceError('Could not run OMP; install it or check its executable path.') from None


def _host_usage(command: list[str], provider: str) -> dict:
    # Broker reports survive CLI exits. Request fresh data for this provider only.
    invalidated = _run_omp(command + ['usage', 'invalidate', '--provider', provider], TOKEN_TIMEOUT, 'usage refresh')
    if invalidated.returncode:
        raise UsageSourceError('Could not refresh the OMP usage cache; previous data may be stale.')
    result = _run_omp(command + ['usage', '--provider', provider, '--json'], USAGE_TIMEOUT, 'usage')
    if result.returncode:
        # Host stderr can contain provider responses; never copy it into errors.
        raise UsageSourceError('OMP usage failed; check provider authentication and OMP configuration.')
    try:
        data = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise UsageSourceError('OMP returned an invalid usage response.') from None
    if not isinstance(data, dict) or not isinstance(data.get('reports'), list):
        raise UsageSourceError('OMP returned an unexpected usage response.')
    for report in data['reports']:
        if (not isinstance(report, dict) or not isinstance(report.get('provider'), str)
                or not isinstance(report.get('limits'), list)
                or any(not isinstance(limit, dict) for limit in report['limits'])):
            raise UsageSourceError('OMP returned an unexpected usage report.')
    data['reports'] = [report for report in data['reports'] if report['provider'].lower() == provider]
    return data


def _deepseek_token(command: list[str]) -> str | None:
    # OMP owns credential resolution (profile, broker, key commands, environment).
    # The credential is captured in memory only: never a file, log, or argv value.
    result = _run_omp(command + ['token', 'deepseek'], TOKEN_TIMEOUT, 'credential lookup')
    if result.returncode or not result.stdout.strip():
        return None
    try:
        token = result.stdout.decode('ascii').strip()
    except UnicodeError:
        raise UsageSourceError('OMP returned an invalid DeepSeek credential.') from None
    if not re.fullmatch(r'[!-~]{1,8192}', token):
        raise UsageSourceError('OMP returned an invalid DeepSeek credential.')
    return token


def _balance_amount(value) -> float:
    if not isinstance(value, str):
        raise UsageSourceError('DeepSeek returned an invalid balance amount.')
    try:
        amount = float(value)
    except ValueError:
        raise UsageSourceError('DeepSeek returned an invalid balance amount.') from None
    if not math.isfinite(amount):
        raise UsageSourceError('DeepSeek returned an invalid balance amount.')
    return amount


def _deepseek_balance(token: str) -> dict:
    request = urllib.request.Request(
        BALANCE_URL, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
        method='GET',
    )
    try:
        with urllib.request.build_opener(_NoRedirects()).open(request, timeout=HTTP_TIMEOUT) as response:
            payload = response.read(MAX_BALANCE_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if status in (401, 403):
            raise UsageSourceError('DeepSeek rejected the credential; check OMP authentication.') from None
        if status == 429:
            raise UsageSourceError('DeepSeek balance request was rate limited; try again later.') from None
        raise UsageSourceError('DeepSeek balance service returned an HTTP error.') from None
    except (urllib.error.URLError, OSError, ValueError):
        raise UsageSourceError('Could not reach the DeepSeek balance service.') from None
    if len(payload) > MAX_BALANCE_BYTES:
        raise UsageSourceError('DeepSeek balance response was too large.')
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        raise UsageSourceError('DeepSeek returned an invalid balance response.') from None
    if (not isinstance(data, dict) or not isinstance(data.get('is_available'), bool)
            or not isinstance(data.get('balance_infos'), list)):
        raise UsageSourceError('DeepSeek returned an unexpected balance response.')

    limits = []
    currencies = set()
    for item in data['balance_infos']:
        if not isinstance(item, dict) or item.get('currency') not in ('USD', 'CNY'):
            raise UsageSourceError('DeepSeek returned an unexpected balance currency.')
        currency = item['currency']
        if currency in currencies:
            raise UsageSourceError('DeepSeek returned duplicate balance currencies.')
        currencies.add(currency)
        remaining = _balance_amount(item.get('total_balance'))
        limits.append({
            'id': f'deepseek:balance:{currency.lower()}',
            'label': f'Remaining balance ({currency})',
            'scope': {'provider': 'deepseek', 'shared': True},
            # CNY is not an OMP UsageUnit; retain its currency in the label.
            'amount': {'remaining': remaining, 'unit': 'usd' if currency == 'USD' else 'unknown'},
        })
    return {
        'provider': 'deepseek', 'fetchedAt': int(time.time() * 1000), 'limits': limits,
        'metadata': {'source': 'deepseek-balance', 'isAvailable': data['is_available']},
        'notes': ['Current API account balance; not a quota or a usage percentage.'],
    }


def fetch_usage(provider: str, profile: str | None = None) -> dict:
    """Fetch normalized account reports; absent support is not zero usage.

    Raises UsageSourceError with a display-safe message on operational failure.
    OMP reports take precedence over fallback endpoints, including future adapters.
    """
    if not isinstance(provider, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', provider):
        raise UsageSourceError('Invalid provider ID.')
    provider = ALIASES.get(provider.lower(), provider.lower())
    command = _omp_command(profile)
    data = _host_usage(command, provider)
    if data['reports']:
        return data
    if provider == 'deepseek':
        token = _deepseek_token(command)
        if token is None:
            data['dashboardNote'] = 'No DeepSeek credential was resolved by OMP for this profile. Configure it in OMP.'
            return data
        report = _deepseek_balance(token)
        if report['limits']:
            data['reports'] = [report]
            data['dashboardNote'] = (
                'DeepSeek API balance only; no quota percentage or reset time is provided.'
                if report['metadata']['isAvailable'] else
                'DeepSeek reports insufficient balance for API calls; no quota percentage is provided.'
            )
        else:
            data['dashboardNote'] = 'DeepSeek returned no balance entries; available balance is unknown.'
    elif provider == 'xai':
        data['dashboardNote'] = (
            'OMP returned no xAI account usage. xAI API billing requires a separate management key '
            'and team ID; this dashboard does not configure them. Check console.x.ai.'
        )
    else:
        data['dashboardNote'] = (
            'OMP returned no account usage. This provider or credential may not support usage reporting; '
            'check its OMP login and provider console. This is not zero usage.'
        )
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--profile')
    args = parser.parse_args()
    try:
        data = fetch_usage(args.provider, args.profile)
        print(json.dumps(data, ensure_ascii=True, allow_nan=False))
    except UsageSourceError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # Never stringify unexpected exceptions: request objects may hold secrets.
        print('Could not fetch account usage due to an unexpected response or local error.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
