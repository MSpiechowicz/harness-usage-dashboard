"""Static host policies for the dashboard; no host plugins or runtime discovery."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import sys
from types import MappingProxyType
from typing import Callable, Mapping

from usage_source import ALIASES


ROOT = Path(__file__).resolve().parent


def _omp_profile(profile: str | None) -> str:
    if profile is None:
        profile = os.environ.get('OMP_PROFILE', os.environ.get('PI_PROFILE', 'default'))
    if not isinstance(profile, str):
        raise ValueError('OMP profile must be a string.')
    profile = profile.strip() or 'default'
    if profile in ('.', '..') or any(char in profile for char in ('/', '\\', '\x00')):
        raise ValueError('OMP profile must be a single directory name.')
    return profile


def _ignore_profile(profile: str | None) -> None:
    return None


def _omp_root(profile: str | None) -> Path:
    profile = _omp_profile(profile)
    if profile != 'default':
        return Path.home() / '.omp' / 'profiles' / profile / 'agent'
    configured = os.environ.get('PI_CODING_AGENT_DIR')
    return Path(configured).expanduser() if configured else Path.home() / '.omp' / 'agent'


def _claude_root(profile: str | None) -> Path:
    configured = os.environ.get('CLAUDE_CONFIG_DIR')
    root = Path(configured).expanduser() if configured else Path.home() / '.claude'
    return root / 'usage-dashboard'


def _omp_fetch(provider: str, profile: str | None, owner: str | None) -> list[str]:
    command = [sys.executable, str(ROOT / 'usage_source.py'), '--provider', provider]
    if profile is not None:
        command += ['--profile', profile]
    return command


def _claude_fetch(provider: str, profile: str | None, owner: str | None) -> list[str]:
    command = [sys.executable, str(ROOT / 'claude_usage_source.py')]
    if owner is not None:
        command += ['--owner', owner]
    return command


def _omp_launch(omp_args: list[str], profile: str | None, native: bool,
                extension: str | Path | None, binary: str | None,
                status_path: str | Path | None) -> list[str]:
    executable = binary or shutil.which('omp') or str(Path.home() / '.local/bin/omp')
    command = ['env', f'OMP_USAGE_LAUNCHER={0 if native else 1}', executable]
    if not native:
        if extension is None:
            raise ValueError('OMP dashboard extension path is required.')
        command += ['-e', str(extension)]
    command.extend(omp_args)
    if profile is not None:
        command += ['--profile', profile]
    return command


def _claude_launch(omp_args: list[str], profile: str | None, native: bool,
                   extension: str | Path | None, binary: str | None,
                   status_path: str | Path | None) -> list[str]:
    executable = binary or shutil.which('claude')
    if not executable:
        raise ValueError('Install Claude Code before opening the Claude sidebar.')
    if status_path is None:
        raise ValueError('Claude exit-status path is required.')
    # Only the exit code crosses the pane boundary, not CLI output or credentials.
    script = 'status=$1; shift; "$@"; code=$?; printf "%s\\n" "$code" > "$status"; exit "$code"'
    return ['sh', '-c', script, 'claude-exit', str(status_path), executable, *omp_args]


@dataclass(frozen=True, slots=True)
class HostAdapter:
    host_id: str
    name: str
    default_providers: tuple[str, ...]
    allowed_providers: frozenset[str] | None
    aliases: Mapping[str, str]
    empty_title: str
    empty_note: str
    empty_compact_note: str
    capture_status: bool
    keep_prior_on_empty: bool
    quota_history: bool
    launch_policy: str
    profile_resolver: Callable[[str | None], str | None] = field(repr=False, compare=False)
    root_resolver: Callable[[str | None], Path] = field(repr=False, compare=False)
    fetch_builder: Callable[[str, str | None, str | None], list[str]] = field(repr=False, compare=False)
    launch_builder: Callable[[list[str], str | None, bool, str | Path | None,
                             str | None, str | Path | None], list[str]] = field(repr=False, compare=False)

    def normalize_profile(self, profile: str | None = None) -> str | None:
        return self.profile_resolver(profile)

    def data_root(self, profile: str | None = None) -> Path:
        return self.root_resolver(profile)

    def provider_id(self, value: str) -> str:
        value = self.aliases.get(value.lower(), value.lower())
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]*', value):
            raise ValueError('Use a provider ID such as codex, claude, copilot, grok, or deepseek')
        if self.allowed_providers is not None and value not in self.allowed_providers:
            if self.host_id == 'claude':
                raise ValueError('Claude dashboard only supports the Anthropic provider.')
            raise ValueError(f'Provider is not supported by {self.name}: {value}')
        return value

    def fetch_command(self, provider: str, profile: str | None = None,
                      owner: str | None = None) -> list[str]:
        return self.fetch_builder(provider, profile, owner)

    def launch_command(self, omp_args: list[str], *, profile: str | None = None,
                       native: bool = False, extension: str | Path | None = None,
                       binary: str | None = None,
                       status_path: str | Path | None = None) -> list[str]:
        return self.launch_builder(omp_args, profile, native, extension, binary, status_path)


_HOSTS = MappingProxyType({
    'omp': HostAdapter(
        host_id='omp', name='OMP', default_providers=(), allowed_providers=None,
        aliases=MappingProxyType(dict(ALIASES)), empty_title='Usage unavailable',
        empty_note='Check OMP login/support', empty_compact_note='Check login / usage support',
        capture_status=False, keep_prior_on_empty=True, quota_history=True,
        launch_policy='omp-extension', profile_resolver=_omp_profile, root_resolver=_omp_root,
        fetch_builder=_omp_fetch, launch_builder=_omp_launch,
    ),
    'claude': HostAdapter(
        host_id='claude', name='CLAUDE', default_providers=('anthropic',),
        allowed_providers=frozenset({'anthropic'}),
        aliases=MappingProxyType({'claude': 'anthropic'}), empty_title='Allowance unknown',
        empty_note='Rate limits not yet captured', empty_compact_note='Rate limits not yet captured',
        capture_status=True, keep_prior_on_empty=False, quota_history=False,
        launch_policy='exit-status', profile_resolver=_ignore_profile, root_resolver=_claude_root,
        fetch_builder=_claude_fetch, launch_builder=_claude_launch,
    ),
})
HOST_IDS = tuple(_HOSTS)


def get_host(host: str) -> HostAdapter:
    """Look up a built-in host; tests may replace the private registry in isolation."""
    try:
        return _HOSTS[host]
    except (KeyError, TypeError):
        raise ValueError('Unknown dashboard host.') from None
