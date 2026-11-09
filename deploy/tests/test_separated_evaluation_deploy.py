"""Separated deployment regression tests; every image, identity and credential is synthetic."""
import base64
import os
from pathlib import Path
import secrets
import shutil

import pytest

from test_knowledge_hub_deploy import _run as _run_script
from test_workbench_deploy import (
    DEPLOY_ROOT, REPO_ROOT, _calls, _drive_tty, _interactive_upgrade_answers,
    _legacy_answers, _prepare_legacy_upgrade, _prepare_separated_upgrade,
    _write_executable, _write_release_package,
)


def _repack(package):
    stage = package.parent / (package.name + '.stage')
    # Without files, the package helper archives its stage directory as edited by the test.
    _write_release_package(package, next(stage.iterdir()).name, {})


def _prepare(tmp_path, mode='upgrade'):
    setup = (_prepare_legacy_upgrade(tmp_path) if mode == 'legacy'
             else _prepare_separated_upgrade(tmp_path, modeling_port='8100'))
    root = tmp_path / 'fresh' if mode == 'install' else (
        setup['legacy'].parent if mode == 'legacy' else setup['root'])
    consumer = root / 'onto-agent'
    if mode == 'legacy':
        (consumer / 'deploy/manifest.json').write_text('{}\n')
    env = {key: value for key, value in setup['env'].items()
           if not key.startswith('EVAL_') and key not in ('HUB_EVALUATION_SERVICE_TOKEN', 'EVALUATION_SERVICE_TOKEN')}
    env.update(EVALUATION_CONFIG_SNAPSHOT=str(tmp_path / 'evaluation-config.snapshot'),
               CONSUMER_TOKEN_SNAPSHOT=str(tmp_path / 'consumer-token.snapshot'),
               EVALUATION_DOCKER_CALLS=str(tmp_path / 'evaluation-docker-calls.txt'),
               STUB_HUB_EVALUATION_TOKEN=secrets.token_hex(32), STUB_CONSUMER_DIR=str(consumer),
               STUB_KNOWLEDGE_HUB_DIR='' if mode == 'install' else str(root / 'knowledge-hub'),
               STUB_ONTO_EVALUATION_TOKEN=secrets.token_hex(32))
    package = tmp_path / 'evaluation-v-test'
    (package / 'deploy').mkdir(parents=True)
    for name in ('deploy.sh', 'docker-compose.eval-service.yml'):
        shutil.copy2(REPO_ROOT / 'eval-service/deploy' / name, package / 'deploy' / name)
    (package / 'evaluation-image.tar').write_bytes(b'Synthetic image; Docker is replaced by a test stub.')
    (package / 'IMAGE').write_text('knowledge-evaluation:v-test\n')

    # Read the Consumer credential when installation/upgrade would restart it.
    consumer_package = setup['packages'][0]
    stage = consumer_package.parent / (consumer_package.name + '.stage') / 'onto-agent'
    _write_executable(stage / 'deploy/upgrade.sh', '''
printf 'consumer-upgrade\\n' >> "$CALLS_FILE"
sed -n 's/^EVALUATION_SERVICE_TOKEN=//p' "$1/backend/.env" > "$CONSUMER_TOKEN_SNAPSHOT"
''')
    _write_executable(stage / 'deploy/install.sh', '''
if [ "${1:-}" = --preflight-only ]; then
    printf 'consumer-preflight\\n' >> "$CALLS_FILE"
else
    printf 'consumer-install\\n' >> "$CALLS_FILE"
    root="$(cd "$(dirname "$0")/.." && pwd)"
    sed -n 's/^EVALUATION_SERVICE_TOKEN=//p' "$root/backend/.env" > "$CONSUMER_TOKEN_SNAPSHOT"
fi
''')
    _repack(consumer_package)
    hub_package = setup['hub_package']
    hub_script = hub_package.parent / (hub_package.name + '.stage') / 'knowledge-hub/deploy.sh'
    with hub_script.open('a') as stream:
        stream.write('\nprintf "HUB_EVALUATION_SERVICE_TOKEN=%s\\n" "$STUB_HUB_EVALUATION_TOKEN" >> .env\n')
    _repack(hub_package)
    # Like the real Onto Modeling deploy.sh, the substitute keeps an inbound notification credential in .env.
    onto_package = setup['packages'][2]
    onto_script = onto_package.parent / (onto_package.name + '.stage') / 'onto-modeling/deploy.sh'
    with onto_script.open('a') as stream:
        stream.write('\nprintf "ONTO_EVALUATION_SERVICE_TOKEN=%s\\n" "$STUB_ONTO_EVALUATION_TOKEN" >> .env\n')
    _repack(onto_package)

    # Exercise the real evaluation deployer without starting containers or using real credentials.
    docker = tmp_path / 'bin/docker'
    original = docker.read_text()
    _write_executable(docker, '''
if [ "$1" = volume ] && [ "${3:-}" = knowledge-evaluation_evaluation-data ]; then
    [ "${STUB_EVALUATION_VOLUME:-0}" = 1 ]; exit $?
fi
if [ "$1" = container ] && [ "${3:-}" = knowledge-evaluation ]; then
    [ -n "${STUB_EVALUATION_DIR:-}" ] || exit 1
    printf '%s\\n' "$STUB_EVALUATION_DIR"; exit 0
fi
if [ "$1" = compose ]; then
    printf 'evaluation-compose\\n' >> "$CALLS_FILE"
    printf '%s\\n' "$*" >> "$EVALUATION_DOCKER_CALLS"
    cp "$EVAL_ENV_FILE" "$EVALUATION_CONFIG_SNAPSHOT"
    exit "${STUB_EVALUATION_COMPOSE_EXIT:-0}"
fi
if [ "$1" = exec ] && [ "$2" = knowledge-evaluation ]; then
    printf 'evaluation-health\\n' >> "$EVALUATION_DOCKER_CALLS"
    exit "${STUB_EVALUATION_HEALTH_EXIT:-0}"
fi
''' + original)
    _write_executable(tmp_path / 'bin/sleep', 'exit 0')
    setup.update(env=env, mode=mode, target=root, evaluation_package=package)
    return setup


def _run(setup, *, package=None, hub=True, scope='3', interactive=True, **overrides):
    package = setup['evaluation_package'] if package is None else package
    mode = setup['mode']
    if not interactive:
        args = [str(DEPLOY_ROOT / 'install-consumer-modeling.sh')]
        if mode == 'upgrade':
            args.append('upgrade')
        elif mode == 'legacy':
            args.extend(['--upgrade-from', str(setup['legacy'])])
        args.extend(['--consumer-package', str(setup['packages'][0]),
                     '--modeling-package', str(setup['packages'][1])])
        if mode != 'legacy':
            args.extend(['--onto-modeling-package', str(setup['packages'][2])])
        if hub:
            args.extend(['--knowledge-hub-package', str(setup['hub_package'])])
        if package:
            args.extend(['--evaluation-package', str(package)])
        result = _run_script(args, {**setup['env'], 'INSTALL_ROOT': str(setup['target']), **overrides})
        return result.returncode, result.stdout + result.stderr
    if mode == 'legacy':
        script = DEPLOY_ROOT / 'upgrade-consumer-modeling-legacy-interactive.sh'
        answers = _legacy_answers(setup, '', hub_answer=str(setup['hub_package']) if hub else '',
                                  evaluation=str(package))
    elif mode == 'install':
        script = DEPLOY_ROOT / 'install-consumer-modeling-interactive.sh'
        answers = [
            ('请选择模式', '1'), ('安装根目录（', '2'),
            ('请输入自定义安装根目录', str(setup['target'])), ('端口配置', '1'),
            ('请输入 Consumer 发布包路径', str(setup['packages'][0])),
            ('请输入 Modeling 发布包路径', str(setup['packages'][1])),
            ('请输入 Onto Modeling 发布包路径', str(setup['packages'][2])),
            ('请输入知识包 Hub 发布包路径', str(setup['hub_package']) if hub else ''),
            ('请输入独立评测服务发布包', str(package)),
        ]
    else:
        script = DEPLOY_ROOT / 'install-consumer-modeling-interactive.sh'
        answers = _interactive_upgrade_answers(
            setup, hub=str(setup['hub_package']) if hub else '', evaluation=str(package))
        answers = [(prompt, scope if prompt == '升级范围' else reply) for prompt, reply in answers]
        if scope == '2':
            answers = [(prompt, reply) for prompt, reply in answers if prompt != '请输入 Consumer 发布包路径']
    return _drive_tty(script, answers, {**setup['env'], **overrides})


def _configuration(tmp_path):
    return dict(line.split('=', 1) for line in (tmp_path / 'evaluation-config.snapshot').read_text().splitlines()
                if line and not line.startswith('#'))


@pytest.mark.parametrize('interactive', [True, False], ids=['interactive', 'noninteractive'])
@pytest.mark.parametrize('mode', ['install', 'upgrade', 'legacy'])
def test_evaluation_uses_dependencies_after_consumer_restart(tmp_path, mode, interactive):
    setup = _prepare(tmp_path, mode)
    rc, output = _run(setup, interactive=interactive)
    assert rc == 0, output
    calls = _calls(setup)
    assert calls.index('consumer-install' if mode == 'install' else 'consumer-upgrade') < calls.index('hub-deploy')
    assert calls[-1] == 'evaluation-compose'
    if mode != 'legacy':
        assert calls.index('hub-deploy') < calls.index('onto-deploy') < calls.index('evaluation-compose')
    config = _configuration(tmp_path)
    assert config['CONSUMER_NETWORK'] == 'onto-agent_default'
    assert config['EVAL_HUB_SERVICE_TOKEN'] == setup['env']['STUB_HUB_EVALUATION_TOKEN']
    assert config['EVAL_CONSUMER_SERVICE_TOKEN'] == (tmp_path / 'consumer-token.snapshot').read_text().strip()
    assert len(config['EVAL_CONSUMER_SERVICE_TOKEN']) == 64
    assert len(base64.urlsafe_b64decode(config['EVAL_ENCRYPTION_KEY'])) == 32
    consumer_env = (setup['target'] / 'onto-agent/backend/.env').read_text()
    assert f"MODELING_SERVICE_TOKEN={config['EVAL_CONSUMER_SERVICE_TOKEN']}\n" not in consumer_env
    assert (setup['target'] / 'eval-service/deploy/.env').stat().st_mode & 0o777 == 0o600
    for key in ('EVAL_ENCRYPTION_KEY', 'EVAL_HUB_SERVICE_TOKEN', 'EVAL_CONSUMER_SERVICE_TOKEN'):
        assert config[key] not in output
    if mode == 'legacy':
        # Legacy migration in this flow skips Onto Modeling: notifications stay disabled with a warning.
        assert 'EVAL_MODELING_SERVICE_TOKEN' not in config
        assert 'EVAL_MODELING_SERVICE_TOKEN is empty' in output
    else:
        assert config['EVAL_MODELING_SERVICE_TOKEN'] == setup['env']['STUB_ONTO_EVALUATION_TOKEN']
        assert config['EVAL_MODELING_SERVICE_TOKEN'] not in output
    assert 'up -d --no-build' in (tmp_path / 'evaluation-docker-calls.txt').read_text()
    assert 'evaluation-health' in (tmp_path / 'evaluation-docker-calls.txt').read_text()
    assert '独立评测服务已就绪' in output


@pytest.mark.parametrize('interactive', [True, False], ids=['interactive', 'noninteractive'])
@pytest.mark.parametrize('mode', ['upgrade', 'legacy'])
def test_evaluation_preserves_key_port_and_consumer_credential(tmp_path, mode, interactive):
    setup = _prepare(tmp_path, mode)
    deploy = setup['target'] / 'eval-service/deploy'
    deploy.mkdir(parents=True)
    encryption_key = base64.urlsafe_b64encode(b'S' * 32).decode()
    token = secrets.token_hex(32)
    with (setup['target'] / 'onto-agent/backend/.env').open('a') as stream:
        stream.write(f'EVALUATION_SERVICE_TOKEN={token}\n')
    (deploy / '.env').write_text(f'EVAL_ENCRYPTION_KEY={encryption_key}\nEVAL_PORT=18090\nEVAL_BIND_ADDRESS=127.0.0.1\n')
    # A source directory may contain runtime material; staging must only copy release files.
    (setup['evaluation_package'] / 'deploy/.env').write_text('# Synthetic source configuration; must not be copied.\n')
    rc, output = _run(setup, interactive=interactive, STUB_EVALUATION_VOLUME='1', STUB_EVALUATION_DIR=str(deploy))
    assert rc == 0, output
    config = _configuration(tmp_path)
    assert config['EVAL_ENCRYPTION_KEY'] == encryption_key
    assert config['EVAL_PORT'] == '18090' and config['EVAL_BIND_ADDRESS'] == '127.0.0.1'
    assert config['EVAL_CONSUMER_SERVICE_TOKEN'] == token
    assert token == (tmp_path / 'consumer-token.snapshot').read_text().strip()
    assert 'Generated EVAL_ENCRYPTION_KEY' not in output


@pytest.mark.parametrize('interactive', [True, False], ids=['interactive', 'noninteractive'])
@pytest.mark.parametrize('problem', ['missing-image', 'foreign-container', 'missing-hub', 'missing-env'])
def test_evaluation_prechecks_before_changing_services(tmp_path, problem, interactive):
    setup = _prepare(tmp_path)
    before = (setup['target'] / 'onto-agent/backend/.env').read_bytes()
    overrides = {}
    if problem == 'missing-image':
        (setup['evaluation_package'] / 'evaluation-image.tar').unlink()
    elif problem == 'foreign-container':
        overrides['STUB_EVALUATION_DIR'] = str(tmp_path / 'elsewhere/deploy')
    elif problem == 'missing-env':
        overrides['EVAL_ENV_FILE'] = str(tmp_path / 'absent.config')
    rc, output = _run(setup, interactive=interactive, hub=problem != 'missing-hub', **overrides)
    assert rc != 0, output
    assert all(call == 'onto-preflight' for call in _calls(setup))
    assert (setup['target'] / 'onto-agent/backend/.env').read_bytes() == before
    assert not (setup['target'] / 'eval-service').exists()


def test_modeling_only_evaluation_requires_existing_consumer_credential(tmp_path):
    setup = _prepare(tmp_path)
    rc, output = _run(setup, scope='2')
    assert rc != 0
    assert '请选择升级 Consumer' in output
    assert 'modeling-install' not in _calls(setup)


def test_modeling_only_evaluation_reuses_consumer_credential_and_existing_hub(tmp_path):
    setup = _prepare(tmp_path)
    token = secrets.token_hex(32)
    consumer_config = setup['target'] / 'onto-agent/backend/.env'
    with consumer_config.open('a') as stream:
        stream.write(f'EVALUATION_SERVICE_TOKEN={token}\n')
    hub = setup['target'] / 'knowledge-hub'
    hub.mkdir()
    (hub / '.env').write_text('HUB_EVALUATION_SERVICE_TOKEN=' + setup['env']['STUB_HUB_EVALUATION_TOKEN'] + '\n')
    before = consumer_config.read_bytes()
    rc, output = _run(setup, scope='2', hub=False)
    assert rc == 0, output
    assert 'consumer-upgrade' not in _calls(setup) and 'hub-deploy' not in _calls(setup)
    assert consumer_config.read_bytes() == before
    assert _configuration(tmp_path)['EVAL_CONSUMER_SERVICE_TOKEN'] == token


@pytest.mark.parametrize('interactive', [True, False], ids=['interactive', 'noninteractive'])
def test_evaluation_external_config_is_resolved_before_directory_changes(tmp_path, interactive):
    setup = _prepare(tmp_path)
    token = secrets.token_hex(32)
    with (setup['target'] / 'onto-agent/backend/.env').open('a') as stream:
        stream.write(f'EVALUATION_SERVICE_TOKEN={token}\n')
    external = tmp_path / 'synthetic-evaluation.config'
    raw = ('EVAL_ENCRYPTION_KEY=' + base64.urlsafe_b64encode(b'E' * 32).decode() + '\n'
           'EVAL_HUB_SERVICE_TOKEN=' + setup['env']['STUB_HUB_EVALUATION_TOKEN'] + '\n'
           'EVAL_CONSUMER_SERVICE_TOKEN=' + token + '\n')
    external.write_text(raw)
    relative = os.path.relpath(external, Path.cwd())
    rc, output = _run(setup, interactive=interactive, hub=False, EVAL_ENV_FILE=relative)
    assert rc == 0, output
    assert external.read_text() == raw
    assert not (setup['target'] / 'eval-service/deploy/.env').exists()
    assert str(external) in (tmp_path / 'evaluation-docker-calls.txt').read_text()


@pytest.mark.parametrize('interactive', [True, False], ids=['interactive', 'noninteractive'])
@pytest.mark.parametrize('failure', ['compose', 'health'])
def test_evaluation_failure_does_not_report_completion(tmp_path, failure, interactive):
    setup = _prepare(tmp_path)
    rc, output = _run(setup, interactive=interactive, **{'STUB_EVALUATION_' + failure.upper() + '_EXIT': '1'})
    assert rc != 0
    assert '交互部署完成' not in output
    assert '分离部署升级完成' not in output
    assert '独立评测服务已就绪' not in output


@pytest.mark.parametrize('interactive', [True, False], ids=['interactive', 'noninteractive'])
def test_evaluation_blank_directory_keeps_original_flow(tmp_path, interactive):
    setup = _prepare(tmp_path, 'install')
    rc, output = _run(setup, interactive=interactive, package='')
    assert rc == 0, output
    assert 'evaluation-compose' not in _calls(setup)
    assert not (setup['target'] / 'eval-service').exists()


def _tar_package(setup, *, top='eval-service', skip=()):
    """Evaluation release as built by eval-service/build.sh: a tar.gz whose top directory is eval-service."""
    package = setup['evaluation_package']
    archive_path = package.parent / 'evaluation-v-test-linux-amd64.tar.gz'
    stage = package.parent / f'{archive_path.name}.stage'
    shutil.rmtree(stage, ignore_errors=True)
    for relative in ('deploy/deploy.sh', 'deploy/docker-compose.eval-service.yml', 'evaluation-image.tar', 'IMAGE'):
        if relative not in skip:
            (stage / top / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(package / relative, stage / top / relative)
    return _write_release_package(archive_path, top, {})


@pytest.mark.parametrize('interactive', [True, False], ids=['interactive', 'noninteractive'])
@pytest.mark.parametrize('mode', ['install', 'upgrade', 'legacy'])
def test_evaluation_tar_package_is_unpacked_like_other_components(tmp_path, mode, interactive):
    setup = _prepare(tmp_path, mode)
    rc, output = _run(setup, interactive=interactive, package=_tar_package(setup))
    assert rc == 0, output
    assert _calls(setup)[-1] == 'evaluation-compose'
    target = setup['target'] / 'eval-service'
    for relative in ('deploy/deploy.sh', 'deploy/docker-compose.eval-service.yml', 'evaluation-image.tar', 'IMAGE'):
        assert (target / relative).is_file(), relative
    assert '独立评测服务已就绪' in output


@pytest.mark.parametrize('interactive', [True, False], ids=['interactive', 'noninteractive'])
@pytest.mark.parametrize('problem', ['wrong-top', 'missing-image'])
def test_invalid_evaluation_tar_package_fails_before_changes(tmp_path, problem, interactive):
    setup = _prepare(tmp_path)
    before = (setup['target'] / 'onto-agent/backend/.env').read_bytes()
    package = (_tar_package(setup, top='evaluation') if problem == 'wrong-top'
               else _tar_package(setup, skip=('evaluation-image.tar',)))
    rc, output = _run(setup, interactive=interactive, package=package)
    assert rc != 0, output
    assert all(call == 'onto-preflight' for call in _calls(setup))
    assert (setup['target'] / 'onto-agent/backend/.env').read_bytes() == before
    assert not (setup['target'] / 'eval-service').exists()


def _attach(setup, *, modeling=False, **overrides):
    """Install Hub, Onto Modeling and evaluation beside an already running Consumer."""
    args = [str(DEPLOY_ROOT / 'install-consumer-modeling.sh')]
    if modeling:
        args.extend(['--modeling-package', str(setup['packages'][1])])
    args.extend(['--knowledge-hub-package', str(setup['hub_package']),
                 '--onto-modeling-package', str(setup['packages'][2]),
                 '--evaluation-package', str(_tar_package(setup))])
    env = {key: value for key, value in setup['env'].items() if key != 'INSTALL_ROOT'}
    env.update(STUB_CONSUMER_RUNNING='true')
    env.update(overrides)
    result = _run_script(args, env)
    return result.returncode, result.stdout + result.stderr


def _with_consumer_evaluation_token(setup):
    token = secrets.token_hex(32)
    with (setup['target'] / 'onto-agent/backend/.env').open('a') as stream:
        stream.write(f'EVALUATION_SERVICE_TOKEN={token}\n')
    return token


def test_running_consumer_is_reused_without_consumer_package(tmp_path):
    setup = _prepare(tmp_path)
    token = _with_consumer_evaluation_token(setup)
    consumer_env = setup['target'] / 'onto-agent/backend/.env'
    before = consumer_env.read_bytes()
    rc, output = _attach(setup)
    assert rc == 0, output
    assert _calls(setup) == ['onto-preflight', 'hub-deploy', 'onto-deploy', 'evaluation-compose']
    assert consumer_env.read_bytes() == before
    # Without INSTALL_ROOT the components sit beside the running Consumer's install directory.
    for component in ('knowledge-hub', 'onto-modeling', 'eval-service'):
        assert (setup['target'] / component).is_dir(), component
    config = _configuration(tmp_path)
    assert config['CONSUMER_NETWORK'] == 'onto-agent_default'
    assert config['EVAL_CONSUMER_SERVICE_TOKEN'] == token
    assert config['EVAL_HUB_SERVICE_TOKEN'] == setup['env']['STUB_HUB_EVALUATION_TOKEN']
    assert token not in output


@pytest.mark.parametrize('problem', ['consumer-stopped', 'modeling-package', 'missing-token', 'old-consumer'])
def test_reusing_consumer_checks_before_changes(tmp_path, problem):
    setup = _prepare(tmp_path)
    if problem != 'missing-token':
        _with_consumer_evaluation_token(setup)
    consumer_env = setup['target'] / 'onto-agent/backend/.env'
    before = consumer_env.read_bytes()
    overrides = {'consumer-stopped': {'STUB_CONSUMER_RUNNING': 'false'},
                 'old-consumer': {'STUB_EVALUATION_ROUTE_STATUS': '404'}}.get(problem, {})
    rc, output = _attach(setup, modeling=problem == 'modeling-package', **overrides)
    assert rc != 0, output
    assert not {'hub-deploy', 'onto-deploy', 'evaluation-compose'} & set(_calls(setup))
    assert not {'consumer-install', 'consumer-upgrade', 'modeling-install'} & set(_calls(setup))
    assert consumer_env.read_bytes() == before
    assert not (setup['target'] / 'eval-service').exists()


def _attach_interactively(setup, *, extra=(), **overrides):
    """Interactive mode 3: Hub, Onto Modeling and evaluation beside an already running Consumer."""
    answers = [('请选择模式', '3'), ('组件安装根目录', '1'),
               ('请输入 Onto Modeling 发布包路径', str(setup['packages'][2])),
               ('请输入知识包 Hub 发布包路径', str(setup['hub_package'])),
               ('请输入独立评测服务发布包路径', str(_tar_package(setup))), *extra]
    env = {**setup['env'], 'STUB_CONSUMER_RUNNING': 'true', **overrides}
    return _drive_tty(DEPLOY_ROOT / 'install-consumer-modeling-interactive.sh', answers, env)


def test_interactive_mode_reuses_running_consumer(tmp_path):
    setup = _prepare(tmp_path)
    token = _with_consumer_evaluation_token(setup)
    consumer_env = setup['target'] / 'onto-agent/backend/.env'
    before = consumer_env.read_bytes()
    rc, output = _attach_interactively(setup)
    assert rc == 0, output
    assert _calls(setup) == ['onto-preflight', 'hub-deploy', 'onto-deploy', 'evaluation-compose']
    assert consumer_env.read_bytes() == before
    for component in ('knowledge-hub', 'onto-modeling', 'eval-service'):
        assert (setup['target'] / component).is_dir(), component
    config = _configuration(tmp_path)
    assert config['EVAL_CONSUMER_SERVICE_TOKEN'] == token
    assert config['EVAL_HUB_SERVICE_TOKEN'] == setup['env']['STUB_HUB_EVALUATION_TOKEN']
    assert token not in output
    assert '交互部署完成' in output


def test_interactive_mode_moves_onto_modeling_off_a_published_port(tmp_path):
    setup = _prepare(tmp_path)
    _with_consumer_evaluation_token(setup)
    rc, output = _attach_interactively(setup, extra=[('请输入 Onto Modeling 宿主端口', '5300')], STUB_BUSY_PORT='5100')
    assert rc == 0, output
    assert '已被容器 synthetic-other-service 占用' in output
    assert 'ONTO_WEB_PORT=5300' in (tmp_path / 'onto-modeling.env').read_text()


@pytest.mark.parametrize('problem', ['consumer-stopped', 'missing-token', 'old-consumer'])
def test_interactive_mode_checks_before_changes(tmp_path, problem):
    setup = _prepare(tmp_path)
    if problem != 'missing-token':
        _with_consumer_evaluation_token(setup)
    consumer_env = setup['target'] / 'onto-agent/backend/.env'
    before = consumer_env.read_bytes()
    overrides = {'consumer-stopped': {'STUB_CONSUMER_RUNNING': 'false'},
                 'old-consumer': {'STUB_EVALUATION_ROUTE_STATUS': '404'}}.get(problem, {})
    rc, output = _attach_interactively(setup, **overrides)
    assert rc != 0, output
    assert not {'hub-deploy', 'onto-deploy', 'evaluation-compose'} & set(_calls(setup))
    assert not {'consumer-install', 'consumer-upgrade', 'modeling-install'} & set(_calls(setup))
    assert consumer_env.read_bytes() == before
    assert not (setup['target'] / 'eval-service').exists()
