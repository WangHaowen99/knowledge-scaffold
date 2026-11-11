"""Deployment tests use synthetic configuration and local command substitutes only."""

import base64
import os
from pathlib import Path
import re
import secrets
import shutil
from tempfile import TemporaryDirectory

from contextlib import contextmanager
import unittest

from test_knowledge_hub_deploy import _run as run_script


ROOT = Path(__file__).resolve().parents[2]
EVAL_DEPLOY_DIR = ROOT / 'eval-service/deploy'
EVAL_COMPOSE = 'docker-compose.eval-service.yml'
# Stub that records what the root deploy script hands to the evaluation deployer.
EVAL_STUB = ('printf "evaluation\\n" >> "$DEPLOY_CALLS_FILE"\n'
             'printf "ENV_FILE=%s\\nNETWORK=%s\\nHUB=%s\\nCONSUMER=%s\\nMODELING=%s\\n" "${EVAL_ENV_FILE:-}" '
             '"${CONSUMER_NETWORK:-}" "${HUB_EVALUATION_SERVICE_TOKEN:-}" "${EVALUATION_SERVICE_TOKEN:-}"'
             ' "${EVAL_MODELING_SERVICE_TOKEN:-}" > "$EVAL_SNAPSHOT"')
INHERITED = ('EVAL_ENV_FILE', 'CONSUMER_NETWORK', 'HUB_EVALUATION_SERVICE_TOKEN', 'EVALUATION_SERVICE_TOKEN',
             'EVAL_ENCRYPTION_KEY', 'EVAL_HUB_SERVICE_TOKEN', 'EVAL_CONSUMER_SERVICE_TOKEN',
             'EVAL_MODELING_SERVICE_TOKEN', 'ONTO_EVALUATION_SERVICE_TOKEN',
             'PIP_INDEX_URL', 'NPM_REGISTRY', 'EVAL_PIP_INDEX_URL', 'EVAL_NPM_REGISTRY')
MIRRORS = {'EVAL_PIP_INDEX_URL': 'https://pypi.example.com/simple',
           'EVAL_NPM_REGISTRY': 'https://npm.example.com'}


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/bash\nset -euo pipefail\n' + body + '\n')
    path.chmod(0o755)


def lines(path):
    return path.read_text().splitlines() if path.exists() else []


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
            **{key: value for key, value in os.environ.items() if key not in INHERITED},
            'EVAL_ENV_FILE': str(config),
            'CONSUMER_NETWORK': 'synthetic-network',
            'DEPLOY_CALLS_FILE': str(calls),
            'EVAL_SNAPSHOT': str(root / 'evaluation-snapshot.txt'),
            'ONTO_AGENT_INSTALL_DIR': str(root / 'installation'),
            'MODELING_SERVICE_TOKEN': 'synthetic-invalid-service-placeholder-0000',
        }
        script(root / 'eval-service/deploy/deploy.sh', EVAL_STUB)
        script(root / 'knowledge-hub/deploy.sh', 'exit 0')
        script(root / 'knowledge-hub/deploy/deploy.sh', 'exit 0')
        script(root / 'onto-modeling/deploy.sh', 'exit 0')
        (root / 'modeling/onto-modeling').mkdir(parents=True)
        script(root / 'bin/docker', 'exit 0')
        env['PATH'] = str(root / 'bin') + os.pathsep + os.environ['PATH']
        yield root, calls, env


def run(root, env, *args):
    return run_script([str(root / 'deploy/deploy.sh'), *args], env)


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
        expected = ['consumer', 'evaluation']
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
    assert f'NETWORK={env["CONSUMER_NETWORK"]}' in lines(root / 'evaluation-snapshot.txt')


def check_only_modes_remain_independent(deployment, option):
    root, calls, env = deployment
    script(root / 'deploy/deploy-stack.sh',
           'printf "stack\\n" >> "$DEPLOY_CALLS_FILE"')
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
    if missing == 'file':
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


def check_stack_hands_dependency_credentials_to_evaluation(deployment):
    root, _, env = deployment
    env.pop('EVAL_ENV_FILE')
    script(root / 'deploy/deploy-stack.sh', 'exit 0')
    hub, consumer = secrets.token_hex(32), secrets.token_hex(32)
    (root / 'knowledge-hub/deploy').mkdir(parents=True, exist_ok=True)
    (root / 'knowledge-hub/deploy/.env').write_text(f'HUB_EVALUATION_SERVICE_TOKEN={hub}\n')
    (root / 'consumer/backend').mkdir(parents=True)
    (root / 'consumer/backend/.env').write_text(f'EVALUATION_SERVICE_TOKEN={consumer}\n')
    result = run(root, env, '--eval-only')
    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = lines(root / 'evaluation-snapshot.txt')
    assert 'ENV_FILE=' in snapshot and f'HUB={hub}' in snapshot and f'CONSUMER={consumer}' in snapshot
    # Onto Modeling is not deployed yet: evaluation receives no notification credential.
    assert 'MODELING=' in snapshot
    assert hub not in result.stdout + result.stderr and consumer not in result.stdout + result.stderr

    # Once Onto Modeling holds its inbound credential, evaluation reuses the same value.
    modeling = secrets.token_hex(32)
    (root / 'modeling/onto-modeling').mkdir(parents=True, exist_ok=True)
    (root / 'modeling/onto-modeling/.env').write_text(f'ONTO_EVALUATION_SERVICE_TOKEN={modeling}\n')
    result = run(root, env, '--eval-only')
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'MODELING={modeling}' in lines(root / 'evaluation-snapshot.txt')
    assert modeling not in result.stdout + result.stderr


def check_release_generates_the_consumer_evaluation_token(deployment):
    root, _, env = deployment
    (root / 'backend').mkdir()
    (root / 'backend/.env.example').write_text('# Synthetic configuration\n')
    script(root / 'deploy/deploy-consumer.sh', 'exit 0')
    result = run(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    consumer_env = root / 'backend/.env'
    token = re.search(r'^EVALUATION_SERVICE_TOKEN=([0-9a-f]{64})$', consumer_env.read_text(), re.MULTILINE).group(1)
    assert consumer_env.stat().st_mode & 0o777 == 0o600
    assert f'CONSUMER={token}' in lines(root / 'evaluation-snapshot.txt')
    assert token not in result.stdout + result.stderr


def check_evaluation_build_embeds_offline_service_in_full_bundle(deployment):
    root, _, env = deployment
    service = root / 'eval-service'
    shutil.copy2(ROOT / 'eval-service/build.sh', service / 'build.sh')
    for name in [EVAL_COMPOSE]:
        shutil.copy2(EVAL_DEPLOY_DIR / name, service / 'deploy' / name)
    for name in ['README.md', '.env.example']:
        (service / name).write_text('# Synthetic build fixture\n')
    # Empty image substitute; no Docker daemon or image build is used.
    script(root / 'bin/docker', 'printf "%s\\n" "$*" >> "$DOCKER_CALLS_FILE"\n'
                                'if [ "$1" = save ]; then touch "$3"; fi')
    env['PATH'] = str(root / 'bin') + os.pathsep + os.environ['PATH']
    env['RELEASE_BUNDLE_DIR'] = str(root / 'bundle')
    env['DOCKER_CALLS_FILE'] = str(root / 'docker-calls.txt')
    env.update(MIRRORS)
    result = run_script([str(service / 'build.sh'), 'v-test', 'amd'], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ('build --platform linux/amd64 --build-arg PIP_INDEX_URL=https://pypi.example.com/simple '
            f'--build-arg NPM_REGISTRY=https://npm.example.com -t knowledge-evaluation:v-test {service}'
            ) in lines(root / 'docker-calls.txt')
    bundle = root / 'bundle/eval-service'
    assert (bundle / 'evaluation-image.tar').is_file()
    assert (bundle / 'IMAGE').read_text().strip() == 'knowledge-evaluation:v-test'
    for name in ['deploy.sh', EVAL_COMPOSE]:
        assert (bundle / 'deploy' / name).is_file(), name


def check_standalone_evaluation_build_produces_tar_package(deployment):
    root, _, env = deployment
    service = root / 'eval-service'
    shutil.copy2(ROOT / 'eval-service/build.sh', service / 'build.sh')
    shutil.copy2(EVAL_DEPLOY_DIR / EVAL_COMPOSE, service / 'deploy' / EVAL_COMPOSE)
    for name in ['README.md', '.env.example']:
        (service / name).write_text('# Synthetic build fixture\n')
    script(root / 'bin/docker', 'if [ "$1" = save ]; then printf synthetic > "$3"; fi')
    env['PATH'] = str(root / 'bin') + os.pathsep + os.environ['PATH']
    env.pop('RELEASE_BUNDLE_DIR', None)
    result = run_script([str(service / 'build.sh'), 'v-test', 'amd'], env)
    assert result.returncode == 0, result.stdout + result.stderr
    package = service / 'dist/evaluation-v-test-linux-amd64.tar.gz'
    # The system tar lists members the way the installer reads the package.
    listing = run_script(['-c', 'tar -tzf "$1"', 'list', str(package)], env)
    assert listing.returncode == 0, listing.stderr
    names = set(listing.stdout.split())
    assert all(name == 'eval-service' or name.startswith('eval-service/') for name in names)
    for relative in ['deploy/deploy.sh', 'deploy/' + EVAL_COMPOSE, 'evaluation-image.tar', 'IMAGE']:
        assert 'eval-service/' + relative in names, relative
    assert not (service / 'dist/release-v-test-amd64').exists()


# ---------- eval-service/deploy/deploy.sh ----------

DOCKER_STUB = '''printf '%s\\n' "$*" >> "$DOCKER_CALLS_FILE"
case "$1" in
    volume) [ "${STUB_VOLUME_EXISTS:-0}" = 1 ]; exit $? ;;
    network) [ "$3" = "${STUB_NETWORK:-}" ]; exit $? ;;
    compose) printf 'ENV_FILE=%s\\nPIP=%s\\nNPM=%s\\n' "$EVAL_ENV_FILE" "${PIP_INDEX_URL-<unset>}" \\
                 "${NPM_REGISTRY-<unset>}" > "$COMPOSE_SNAPSHOT" ;;
esac
exit 0'''


@contextmanager
def evaluation_service():
    with TemporaryDirectory(prefix='evaluation-service-deploy-test-') as directory:
        root = Path(directory)
        deploy_dir = root / 'eval-service/deploy'
        deploy_dir.mkdir(parents=True)
        for name in ['deploy.sh', EVAL_COMPOSE]:
            shutil.copy2(EVAL_DEPLOY_DIR / name, deploy_dir / name)
        script(root / 'bin/docker', DOCKER_STUB)
        env = {
            **{key: value for key, value in os.environ.items()
               if key not in INHERITED and not key.startswith('EVAL_')},
            'PATH': str(root / 'bin') + os.pathsep + os.environ['PATH'],
            'DOCKER_CALLS_FILE': str(root / 'docker-calls.txt'),
            'COMPOSE_SNAPSHOT': str(root / 'compose-snapshot.txt'),
            'STUB_NETWORK': 'synthetic-network',
        }
        yield root, deploy_dir, env


def run_service(deploy_dir, env):
    return run_script([str(deploy_dir / 'deploy.sh')], env)


def compose_calls(root):
    return [call for call in lines(root / 'docker-calls.txt') if call.startswith('compose')]


class EvaluationServiceDeployTests(unittest.TestCase):
    def test_local_configuration_is_generated_and_reused(self):
        with evaluation_service() as (root, deploy_dir, env):
            hub, consumer, modeling = secrets.token_hex(32), secrets.token_hex(32), secrets.token_hex(32)
            env.update(CONSUMER_NETWORK='synthetic-network', HUB_EVALUATION_SERVICE_TOKEN=hub,
                       EVALUATION_SERVICE_TOKEN=consumer, EVAL_MODELING_SERVICE_TOKEN=modeling)
            result = run_service(deploy_dir, env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            config = deploy_dir / '.env'
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            values = dict(line.split('=', 1) for line in lines(config))
            self.assertEqual(len(base64.urlsafe_b64decode(values['EVAL_ENCRYPTION_KEY'])), 32)
            # Non-secret stack addresses default in the compose file, not in the local configuration.
            for key in ('EVAL_AUTH_URL', 'EVAL_HUB_URL', 'EVAL_CONSUMER_API_URL'):
                self.assertNotIn(key, values)
            self.assertEqual(values['EVAL_HUB_SERVICE_TOKEN'], hub)
            self.assertEqual(values['EVAL_CONSUMER_SERVICE_TOKEN'], consumer)
            self.assertEqual(values['EVAL_MODELING_SERVICE_TOKEN'], modeling)
            self.assertEqual(values['CONSUMER_NETWORK'], 'synthetic-network')
            output = result.stdout + result.stderr
            for secret in (values['EVAL_ENCRYPTION_KEY'], hub, consumer, modeling):
                self.assertNotIn(secret, output)
            self.assertNotIn('EVAL_MODELING_SERVICE_TOKEN is empty', output)
            self.assertIn('EVAL_JUDGE_BASE_URL is not configured', result.stderr)
            self.assertIn('EVAL_JUDGE_CONFIG_REVISION is not configured', result.stderr)
            # Mirrors are not passed unless configured, so the Dockerfile defaults apply.
            self.assertEqual(lines(root / 'compose-snapshot.txt'),
                             [f'ENV_FILE={config}', 'PIP=<unset>', 'NPM=<unset>'])
            self.assertEqual(compose_calls(root),
                             [f'compose --env-file {config} -f {deploy_dir / EVAL_COMPOSE} up -d --build'])

            # A later Hub credential wins; the encryption key is never regenerated.
            rotated = secrets.token_hex(32)
            env.update(HUB_EVALUATION_SERVICE_TOKEN=rotated, STUB_VOLUME_EXISTS='1')
            rerun = run_service(deploy_dir, env)
            self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
            again = dict(line.split('=', 1) for line in lines(config))
            self.assertEqual(again['EVAL_ENCRYPTION_KEY'], values['EVAL_ENCRYPTION_KEY'])
            self.assertEqual(again['EVAL_HUB_SERVICE_TOKEN'], rotated)

            # A later run without an injected modeling credential keeps the stored one.
            env.pop('EVAL_MODELING_SERVICE_TOKEN')
            kept = run_service(deploy_dir, env)
            self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
            self.assertEqual(dict(line.split('=', 1) for line in lines(config))['EVAL_MODELING_SERVICE_TOKEN'], modeling)

    def test_missing_modeling_notification_token_only_warns(self):
        with evaluation_service() as (root, deploy_dir, env):
            env.update(CONSUMER_NETWORK='synthetic-network')
            result = run_service(deploy_dir, env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('EVAL_MODELING_SERVICE_TOKEN is empty', result.stderr)
            self.assertNotIn('EVAL_MODELING_SERVICE_TOKEN', (deploy_dir / '.env').read_text())
            self.assertEqual(len(compose_calls(root)), 1)

    def test_configured_mirrors_reach_the_compose_build(self):
        with evaluation_service() as (root, deploy_dir, env):
            env.update(MIRRORS, CONSUMER_NETWORK='synthetic-network', PIP_INDEX_URL='')
            result = run_service(deploy_dir, env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(lines(root / 'compose-snapshot.txt')[1:],
                             ['PIP=https://pypi.example.com/simple', 'NPM=https://npm.example.com'])

    def test_existing_data_without_key_is_refused(self):
        with evaluation_service() as (root, deploy_dir, env):
            env.update(STUB_VOLUME_EXISTS='1', CONSUMER_NETWORK='synthetic-network')
            result = run_service(deploy_dir, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('EVAL_ENCRYPTION_KEY', result.stderr)
            self.assertEqual(compose_calls(root), [])
            self.assertNotIn('EVAL_ENCRYPTION_KEY', (deploy_dir / '.env').read_text())

    def test_external_configuration_is_used_unchanged(self):
        with evaluation_service() as (root, deploy_dir, env):
            external = root / 'protected.config'
            content = 'EVAL_HUB_SERVICE_TOKEN=synthetic-invalid-placeholder-0000\n'
            external.write_text(content + 'CONSUMER_NETWORK=synthetic-network\n')
            env['EVAL_ENV_FILE'] = str(external)
            result = run_service(deploy_dir, env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(external.read_text(), content + 'CONSUMER_NETWORK=synthetic-network\n')
            self.assertFalse((deploy_dir / '.env').exists())
            self.assertEqual(lines(root / 'compose-snapshot.txt')[0], f'ENV_FILE={external}')
            self.assertEqual(compose_calls(root),
                             [f'compose --env-file {external} -f {deploy_dir / EVAL_COMPOSE} up -d --build'])

            env['EVAL_ENV_FILE'] = str(root / 'absent.config')
            self.assertNotEqual(run_service(deploy_dir, env).returncode, 0)

    def test_missing_or_unknown_network_fails_before_configuration(self):
        for network, message in (('', 'CONSUMER_NETWORK is not set'), ('absent-network', 'Docker network not found')):
            with self.subTest(network=network), evaluation_service() as (root, deploy_dir, env):
                env['CONSUMER_NETWORK'] = network
                result = run_service(deploy_dir, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(compose_calls(root), [])
                self.assertFalse((deploy_dir / '.env').exists())


class EvaluationDeploymentTests(unittest.TestCase):
    def test_default_modes(self):
        for mode in ['source', 'release', 'upgrade']:
            with self.subTest(mode=mode), deployment() as fixture:
                check_default_deploy_includes_evaluation_last(fixture, mode)

    def test_only_modes(self):
        for option in ['--consumer-only', '--onto-modeling-only',
                       '--knowledge-hub-only', '--eval-only']:
            with self.subTest(option=option), deployment() as fixture:
                check_only_modes_remain_independent(fixture, option)

    def test_preflight(self):
        for missing in ['file', 'bundle']:
            with self.subTest(missing=missing), deployment() as fixture:
                check_missing_evaluation_prerequisites_fail_before_stack_changes(fixture, missing)

    def test_failure(self):
        with deployment() as fixture:
            check_evaluation_failure_propagates(fixture)

    def test_dependency_credentials(self):
        with deployment() as fixture:
            check_stack_hands_dependency_credentials_to_evaluation(fixture)

    def test_release_consumer_token(self):
        with deployment() as fixture:
            check_release_generates_the_consumer_evaluation_token(fixture)

    def test_offline_bundle(self):
        with deployment() as fixture:
            check_evaluation_build_embeds_offline_service_in_full_bundle(fixture)

    def test_standalone_package(self):
        with deployment() as fixture:
            check_standalone_evaluation_build_produces_tar_package(fixture)


if __name__ == '__main__':
    unittest.main()
