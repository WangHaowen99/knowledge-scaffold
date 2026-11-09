"""Issue 389 deployment regressions with synthetic packages and local command stubs."""
import os
import secrets

import pytest

from test_knowledge_hub_deploy import _run, _source_checkout, _write_executable
from test_separated_evaluation_deploy import _prepare, _run as run_installation
from test_workbench_deploy import (
    DEPLOY_ROOT, _calls, _drive_tty, _interactive_upgrade_answers, _legacy_answers,
    _prepare_legacy_upgrade, _prepare_separated_upgrade, _run_separated_upgrade,
)


@pytest.mark.parametrize('interactive', [True, False])
def test_first_install_rejects_missing_hub_before_service_changes(tmp_path, interactive):
    setup = _prepare(tmp_path, 'install')
    status, output = run_installation(setup, interactive=interactive, package='', hub=False)
    assert status != 0
    assert '知识包发布不可用' in output
    assert _calls(setup) == ['onto-preflight']
    assert not setup['target'].exists()


@pytest.mark.parametrize('interactive', [True, False])
@pytest.mark.parametrize('failure', ['missing', 'unhealthy'])
def test_upgrade_rejects_missing_hub_before_service_changes(tmp_path, interactive, failure):
    setup = _prepare_separated_upgrade(tmp_path, modeling_port='8100')
    overrides = {'STUB_KNOWLEDGE_HUB_DIR': ''} if failure == 'missing' else {'STUB_HUB_HEALTH_EXIT': '1'}
    if interactive:
        status, output = _drive_tty(DEPLOY_ROOT / 'install-consumer-modeling-interactive.sh',
                                    _interactive_upgrade_answers(setup), {**setup['env'], **overrides})
    else:
        result = _run_separated_upgrade(setup, **overrides)
        status, output = result.returncode, result.stdout + result.stderr
    assert status != 0
    assert '知识包发布不可用' in output
    assert _calls(setup) == ['onto-preflight']


def test_legacy_upgrade_rejects_missing_hub_before_service_changes(tmp_path):
    setup = _prepare_legacy_upgrade(tmp_path)
    status, output = _drive_tty(
        DEPLOY_ROOT / 'upgrade-consumer-modeling-legacy-interactive.sh',
        _legacy_answers(setup, str(setup['packages'][2]), ('请输入 Onto Modeling 宿主端口', '5110')),
        {**setup['env'], 'STUB_KNOWLEDGE_HUB_DIR': ''})
    assert status != 0
    assert '知识包发布不可用' in output
    assert _calls(setup) == ['onto-preflight']


@pytest.mark.parametrize('only', [True, False])
def test_source_deployment_reports_hub_dependency_failure(tmp_path, only):
    deploy_dir, env = _source_checkout(tmp_path)
    docker = tmp_path / 'bin/docker'
    body = docker.read_text()
    docker.write_text(body.replace('set -euo pipefail\n',
        'set -euo pipefail\nif [ "$1" = exec ]; then case "$*" in *health/dependencies*) exit 1 ;; esac; fi\n', 1))
    args = [str(deploy_dir / 'deploy.sh')]
    if only:
        args.append('--onto-modeling-only')
    result = _run(args, env)
    assert result.returncode == (0 if only else 1)
    assert '知识包发布不可用' in result.stderr
    assert 'Deployment finished successfully' not in result.stdout
    if only:
        assert '--knowledge-hub-only' in result.stderr


def test_standalone_modeling_deployment_warns_when_publication_is_unavailable(tmp_path):
    source = DEPLOY_ROOT.parent / 'modeling/onto-modeling/deploy/deploy.sh'
    package = tmp_path / 'onto-modeling'
    package.mkdir()
    (package / 'deploy.sh').write_bytes(source.read_bytes())
    configuration = {
        'ONTO_HUB_SERVICE_TOKEN': secrets.token_hex(32),
        'ONTO_EVALUATION_SERVICE_TOKEN': secrets.token_hex(32),
        'AUTH_BOOTSTRAP_ADMIN_PASSWORD': secrets.token_urlsafe(24),
    }
    (package / '.env').write_text(''.join(f'{key}={value}\n' for key, value in configuration.items()))
    _write_executable(package / 'preflight.sh', 'exit 0')
    _write_executable(tmp_path / 'bin/docker',
        'if [ "$1" = exec ]; then case "$*" in *health/dependencies*) exit 1 ;; esac; fi\nexit 0')
    result = _run([str(package / 'deploy.sh')], {**os.environ, 'PATH': str(tmp_path / 'bin') + ':' + os.environ['PATH']})
    assert result.returncode == 0, result.stdout + result.stderr
    assert '知识包发布不可用' in result.stderr
    assert '--knowledge-hub-package' in result.stderr
    assert '建模服务与知识包发布依赖已就绪' not in result.stdout
