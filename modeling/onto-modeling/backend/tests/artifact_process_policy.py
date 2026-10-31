"""Narrow execution policy for synthetic artifact CLI and Git integration tests.

This is not a sandbox: the checkout, interpreter and OS tool installation must
be trusted. Never forward the host's credentials, shell startup or Git settings.
"""
import os
from pathlib import Path
import sys


def system_search_path():
    # Windows os.defpath may contain '.', which must not become a tool source.
    return os.pathsep.join(part for part in os.defpath.split(os.pathsep) if os.path.isabs(part))


def git_executable():
    # Do not search an inherited PATH (which can contain the fixture or cwd).
    for directory in system_search_path().split(os.pathsep):
        if not directory:
            continue
        executable = Path(directory) / ('git.exe' if os.name == 'nt' else 'git')
        if executable.is_file() and os.access(executable, os.X_OK):
            return str(executable.resolve(strict=True))
    raise RuntimeError('Git must be installed on the OS default executable path')


def child_environment(scratch):
    scratch = Path(scratch).resolve(strict=True)
    home = scratch / 'process-home'
    home.mkdir(exist_ok=True)
    for name in ('tmp', 'empty-hooks', 'empty-template'):
        (home / name).mkdir(exist_ok=True)
    # Keep only Windows loader essentials, not the complete host environment.
    env = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
    env.update({
        'PATH': os.pathsep.join(filter(None, (str(Path(sys.executable).parent), system_search_path()))),
        'HOME': str(home), 'USERPROFILE': str(home), 'XDG_CONFIG_HOME': str(home),
        'TMPDIR': str(home / 'tmp'), 'TMP': str(home / 'tmp'), 'TEMP': str(home / 'tmp'),
        'LANG': 'C', 'LC_ALL': 'C', 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
        'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': os.devnull,
        'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
        'GIT_NO_REPLACE_OBJECTS': '1',
    })
    settings = (
        ('core.hooksPath', str(home / 'empty-hooks')),
        ('init.templateDir', str(home / 'empty-template')),
        ('core.fsmonitor', 'false'), ('commit.gpgsign', 'false'),
        ('tag.gpgsign', 'false'), ('protocol.allow', 'never'),
    )
    env['GIT_CONFIG_COUNT'] = str(len(settings))
    for index, (key, value) in enumerate(settings):
        env[f'GIT_CONFIG_KEY_{index}'] = key
        env[f'GIT_CONFIG_VALUE_{index}'] = value
    return env


def validate_fixture_git_args(args):
    allowed = {
        ('init', '-q'),
        ('config', 'user.name', 'Synthetic Test'),
        ('config', 'user.email', 'test@example.invalid'),
        ('commit', '--allow-empty', '-qm', 'init: synthetic'),
        ('commit', '-qm', 'extract: synthetic objects'),
        ('add', 'ontology/object'), ('add', 'ontology/diagrams'),
    }
    if tuple(args) not in allowed:
        raise ValueError('Git operation is outside the synthetic fixture allowlist')
