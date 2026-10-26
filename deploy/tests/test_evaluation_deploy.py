"""Deployment tests use synthetic configuration and local command substitutes only."""

import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from contextlib import contextmanager
import unittest


ROOT = Path(__file__).resolve().parents[2]


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/bash\nset -euo pipefail\n' + body + '\n')
    path.chmod(0o755)


@contextmanager
def deployment():
    with TemporaryDirectory(prefix='evaluation-deploy-test-') as directory:
        root = Path(directory)
        (root / 'deploy').mkdir()
        shutil.copy2(ROOT / 'deploy/deploy.sh', root / 'deploy/deploy.sh')
        config = root / 'evaluation.config'
        config.write_text('# Synthetic configuration; no credentials.\n')
        calls = root / 'calls.txt'
        env = {
            **os.environ,
            'EVAL_ENV_FILE': str(config),
            'DEPLOY_CALLS_FILE': str(calls),
            'ONTO_AGENT_INSTALL_DIR': str(root / 'installation'),
            'MODELING_SERVICE_TOKEN': 'synthetic-invalid-service-placeholder-0000',
            'MODELING_MCP_TOKEN_SECRET': 'synthetic-invalid-mcp-placeholder-0000',
        }
        script(root / 'eval-service/deploy/deploy.sh',
               'test -r "$EVAL_ENV_FILE"\nprintf "evaluation\\n" >> "$DEPLOY_CALLS_FILE"')
        yield root, calls, env


def run(root, env, *args):
    return subprocess.run(['bash', str(root / 'deploy/deploy.sh'), *args],
                          env=env, capture_output=True, text=True, timeout=15)


def check_default_deploy_includes_evaluation_last(deployment, mode):
    root, calls, env = deployment
    if mode == 'source':
        script(root / 'deploy/deploy-stack.sh',
               'printf "stack\\n" >> "$DEPLOY_CALLS_FILE"')
        expected = ['stack', 'evaluation']
    elif mode == 'release':
        (root / 'backend').mkdir()
        (root / 'backend/.env.example').write_text('# Synthetic configuration\n')
        script(root / 'deploy/deploy-consumer.sh',
               'printf "consumer\\n" >> "$DEPLOY_CALLS_FILE"')
        script(root / 'deploy/modeling/deploy.sh',
               'printf "modeling\\n" >> "$DEPLOY_CALLS_FILE"')
        expected = ['consumer', 'modeling', 'evaluation']
    else:
        (root / 'deploy/manifest.json').write_text('{}\n')
        (root / 'installation').mkdir()
        (root / 'installation/VERSION').write_text('synthetic-version\n')
        script(root / 'deploy/upgrade.sh',
               'printf "upgrade\\n" >> "$DEPLOY_CALLS_FILE"')
        expected = ['upgrade', 'evaluation']
    result = run(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == expected


def check_only_modes_remain_independent(deployment, option):
    root, calls, env = deployment
    script(root / 'deploy/deploy-stack.sh',
           'printf "stack\\n" >> "$DEPLOY_CALLS_FILE"')
    if option != '--eval-only':
        env.pop('EVAL_ENV_FILE')
    result = run(root, env, option)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = calls.read_text().splitlines() if calls.exists() else []
    assert actual == (['evaluation'] if option == '--eval-only' else
                      [] if option in ('--onto-modeling-only', '--knowledge-hub-only') else ['stack'])


def check_missing_evaluation_prerequisites_fail_before_stack_changes(deployment, missing):
    root, calls, env = deployment
    script(root / 'deploy/deploy-stack.sh',
           'printf "stack\\n" >> "$DEPLOY_CALLS_FILE"')
    if missing == 'variable':
        env.pop('EVAL_ENV_FILE')
    elif missing == 'file':
        env['EVAL_ENV_FILE'] = str(root / 'absent.config')
    else:
        (root / 'eval-service/deploy/deploy.sh').unlink()
    result = run(root, env)
    assert result.returncode != 0
    assert not calls.exists()


def check_evaluation_failure_propagates(deployment):
    root, calls, env = deployment
    script(root / 'deploy/deploy-stack.sh', 'exit 0')
    script(root / 'eval-service/deploy/deploy.sh', 'exit 17')
    result = run(root, env)
    assert result.returncode == 17
    assert 'Deployment finished successfully' not in result.stdout


def check_evaluation_build_embeds_offline_service_in_full_bundle(deployment):
    root, _, env = deployment
    service = root / 'eval-service'
    shutil.copy2(ROOT / 'eval-service/build.sh', service / 'build.sh')
    for name in ['compose.yml', 'nginx.conf.example']:
        shutil.copy2(ROOT / 'eval-service/deploy' / name, service / 'deploy' / name)
    for name in ['README.md', '.env.example']:
        (service / name).write_text('# Synthetic build fixture\n')
    # Empty image substitute; no Docker daemon or image build is used.
    script(root / 'bin/docker', 'if [ "$1" = save ]; then touch "$3"; fi')
    env['PATH'] = str(root / 'bin') + os.pathsep + os.environ['PATH']
    env['RELEASE_BUNDLE_DIR'] = str(root / 'bundle')
    result = subprocess.run(['bash', str(service / 'build.sh'), 'v-test', 'amd'],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    bundle = root / 'bundle/eval-service'
    assert (bundle / 'evaluation-image.tar').is_file()
    assert (bundle / 'IMAGE').read_text().strip() == 'knowledge-evaluation:v-test'
    assert (bundle / 'deploy/deploy.sh').is_file()
    assert (bundle / 'deploy/nginx.conf.example').is_file()


class EvaluationDeploymentTests(unittest.TestCase):
    def test_default_modes(self):
        for mode in ['source', 'release', 'upgrade']:
            with self.subTest(mode=mode), deployment() as fixture:
                check_default_deploy_includes_evaluation_last(fixture, mode)

    def test_only_modes(self):
        for option in ['--consumer-only', '--modeling-only', '--onto-modeling-only',
                       '--knowledge-hub-only', '--eval-only']:
            with self.subTest(option=option), deployment() as fixture:
                check_only_modes_remain_independent(fixture, option)

    def test_preflight(self):
        for missing in ['variable', 'file', 'bundle']:
            with self.subTest(missing=missing), deployment() as fixture:
                check_missing_evaluation_prerequisites_fail_before_stack_changes(fixture, missing)

    def test_failure(self):
        with deployment() as fixture:
            check_evaluation_failure_propagates(fixture)

    def test_offline_bundle(self):
        with deployment() as fixture:
            check_evaluation_build_embeds_offline_service_in_full_bundle(fixture)


if __name__ == '__main__':
    unittest.main()
