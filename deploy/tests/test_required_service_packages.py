"""Four required service packages; synthetic release archives and local commands only."""
import pytest

from test_knowledge_hub_deploy import _run as run_script
from test_separated_evaluation_deploy import _prepare, _run
from test_workbench_deploy import DEPLOY_ROOT, _calls, _drive_tty, _interactive_upgrade_answers, _legacy_answers


REQUIRED = ('--consumer-package', '--onto-modeling-package', '--knowledge-hub-package', '--evaluation-package')


@pytest.mark.parametrize('mode', ['install', 'upgrade', 'legacy'])
@pytest.mark.parametrize('missing', REQUIRED)
def test_missing_required_package_fails_before_any_service_change(tmp_path, mode, missing):
    setup = _prepare(tmp_path, mode)
    args = [str(DEPLOY_ROOT / 'install-consumer-modeling.sh')]
    if mode == 'upgrade':
        args.append('upgrade')
    elif mode == 'legacy':
        args.extend(['--upgrade-from', str(setup['legacy'])])
    packages = (setup['packages'][0], setup['packages'][2], setup['hub_package'], setup['evaluation_package'])
    for option, package in zip(REQUIRED, packages):
        if option != missing:
            args.extend([option, str(package)])
    result = run_script(args, setup['env'])
    assert result.returncode != 0
    assert missing in result.stderr
    assert _calls(setup) == []


@pytest.mark.parametrize('mode', ['install', 'upgrade', 'legacy'])
@pytest.mark.parametrize('missing', ['Consumer', 'Onto Modeling', 'Hub', 'evaluation'])
def test_interactive_deployment_rejects_empty_required_package(tmp_path, mode, missing):
    setup = _prepare(tmp_path, mode)
    if mode == 'legacy':
        script = DEPLOY_ROOT / 'upgrade-consumer-modeling-legacy-interactive.sh'
        answers = _legacy_answers(setup, None)
    elif mode == 'upgrade':
        script = DEPLOY_ROOT / 'install-consumer-modeling-interactive.sh'
        answers = _interactive_upgrade_answers(setup)
    else:
        script = DEPLOY_ROOT / 'install-consumer-modeling-interactive.sh'
        answers = [('请选择模式', '1'), ('安装根目录（', '2'),
                   ('请输入自定义安装根目录', str(setup['target'])), ('端口配置', '1'),
                   ('请输入 Consumer 发布包路径', str(setup['packages'][0])),
                   ('请输入 Modeling 发布包路径', ''),
                   ('请输入 Onto Modeling 发布包路径', str(setup['packages'][2])),
                   ('请输入知识包 Hub 发布包路径', str(setup['hub_package'])),
                   ('请输入独立评测服务发布包', str(setup['evaluation_package']))]
    prompt = '独立评测服务' if missing == 'evaluation' else missing
    answers = [(label, '' if prompt in label else value) for label, value in answers]
    status, output = _drive_tty(script, answers, setup['env'])
    assert status != 0, output
    assert _calls(setup) == []


@pytest.mark.parametrize('interactive', [True, False], ids=['interactive', 'noninteractive'])
@pytest.mark.parametrize('mode', ['install', 'upgrade', 'legacy'])
@pytest.mark.parametrize('modeling', [True, False], ids=['with-workbench', 'without-workbench'])
def test_all_four_services_are_deployed_with_optional_workbench(tmp_path, mode, interactive, modeling):
    setup = _prepare(tmp_path, mode)
    workbench_env = setup['target'] / 'modeling/.env'
    before = workbench_env.read_bytes() if workbench_env.is_file() else None
    status, output = _run(setup, interactive=interactive, modeling=modeling)
    assert status == 0, output
    calls = _calls(setup)
    consumer = 'consumer-install' if mode == 'install' else 'consumer-upgrade'
    assert calls.index(consumer) < calls.index('hub-deploy') < calls.index('onto-deploy')
    assert calls[-1] == 'evaluation-compose'
    assert ('modeling-install' in calls) is modeling
    assert (setup['target'] / 'knowledge-hub/deploy.sh').is_file()
    assert (setup['target'] / 'onto-modeling/deploy.sh').is_file()
    assert (setup['target'] / 'eval-service/deploy/deploy.sh').is_file()
    if not modeling:
        assert (workbench_env.read_bytes() if workbench_env.is_file() else None) == before
        stopped = tmp_path / 'calls.txt.stopped'
        if stopped.exists():
            assert 'mw-modeling' not in stopped.read_text()
        if mode != 'install':
            arguments = tmp_path / 'calls.txt.consumer-upgrade-args'
            assert '--consumer-only' in arguments.read_text().splitlines()
