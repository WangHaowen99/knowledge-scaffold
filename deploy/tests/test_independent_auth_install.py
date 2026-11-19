"""Synthetic offline installer checks for independently owned Auth."""

import json
import os
import hashlib
import shutil
import subprocess
import tarfile

from test_knowledge_hub_deploy import _run as run_script
from test_separated_evaluation_deploy import _prepare, _repack
from test_workbench_deploy import DEPLOY_ROOT, REPO_ROOT, _calls, _write_executable


AUTH_IMAGE = 'sha256:' + 'a' * 64
AUTH_ADMIN_TOKEN = 'synthetic-auth-admin-token-00000000000000000000'


def _auth_package(tmp_path):
    root = tmp_path / 'auth-stage' / 'onto-auth-test-amd64'
    (root / 'deploy').mkdir(parents=True)
    (root / 'images').mkdir()
    (root / 'images/auth-server.tar').write_text('synthetic image')
    (root / 'deploy/auth-install.py').write_text('''import os
import sys
from pathlib import Path

with Path(os.environ['CALLS_FILE']).open('a') as stream:
    stream.write('auth-install\\n')
owner_env = Path(sys.argv[sys.argv.index('--env-file') + 1])
assert owner_env == Path(os.environ['STUB_AUTH_OWNER']) / '.env'
assert owner_env.is_file()
assert '--config' not in sys.argv
record = Path(os.environ['STUB_AUTH_RECORD']).read_text()
consumer = Path(os.environ['STUB_CONSUMER_DIR'])
owner = Path(os.environ['STUB_AUTH_OWNER'])
(consumer / 'deploy/state').mkdir(parents=True, exist_ok=True)
(consumer / 'deploy/state/auth-owner.json').write_text(record)
(owner / 'deploy/state').mkdir(parents=True, exist_ok=True)
(owner / 'deploy/state/ownership.json').write_text(record)
''')
    _write_executable(root / 'deploy/auth-upgrade.sh', '''
printf 'auth-upgrade\\n' >> "$CALLS_FILE"
exit "${STUB_AUTH_UPGRADE_RC:-0}"
''')
    for name in ('auth_upgrade.py', 'auth_external.py'):
        (root / 'deploy' / name).write_text('# synthetic\n')
    (root / 'deploy/auth-offline.py').write_text('''import json
import os
import sys
from pathlib import Path

with Path(os.environ['CALLS_FILE']).open('a') as stream:
    stream.write('auth-migrate\\n')
    if '--resume' in sys.argv:
        stream.write('auth-migrate-resume\\n')
if os.environ.get('STUB_AUTH_MIGRATE_RC'):
    raise SystemExit(int(os.environ['STUB_AUTH_MIGRATE_RC']))
root = Path(sys.argv[sys.argv.index('--install-dir') + 1])
owner = Path(sys.argv[sys.argv.index('--owner-dir') + 1])
project = sys.argv[sys.argv.index('--project') + 1]
state = json.loads(Path(os.environ['STUB_AUTH_RECORD']).read_text())
state['ownerDir'] = str(owner)
state['targetProject'] = project
state['step'] = 'completed'
(root / 'deploy/state').mkdir(parents=True, exist_ok=True)
(root / 'deploy/state/auth-owner.json').write_text(json.dumps(state))
(owner / 'deploy/state').mkdir(parents=True, exist_ok=True)
(owner / 'deploy/state/ownership.json').write_text(json.dumps(state))
''')
    files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in root.rglob('*') if path.is_file()}
    (root / 'auth-release.json').write_text(json.dumps({
        'schema_version': 1, 'contract': 'auth-v1', 'version': 'test',
        'architecture': 'amd64', 'image_id': AUTH_IMAGE, 'files': files}))
    package = tmp_path / 'onto-auth-test-amd64.tar.gz'
    with tarfile.open(package, 'w:gz') as archive:
        archive.add(root, arcname=root.name)
    return package


def _run(setup, *, interactive=False, **overrides):
    assert not interactive
    args = [str(DEPLOY_ROOT / 'install-consumer-modeling.sh')]
    if setup['mode'] == 'upgrade':
        args.append('upgrade')
    elif setup['mode'] == 'legacy':
        args.extend(['--upgrade-from', str(setup['legacy'])])
    args.extend(['--consumer-package', str(setup['packages'][0]),
                 '--auth-package', str(setup['auth_package']),
                 '--onto-modeling-package', str(setup['packages'][2]),
                 '--knowledge-hub-package', str(setup['hub_package']),
                 '--evaluation-package', str(setup['evaluation_package'])])
    env = {**setup['env'], 'INSTALL_ROOT': str(setup['target']),
           **({'AUTH_OWNER_DIR': setup['env']['STUB_AUTH_OWNER']} if setup['mode'] == 'install' else {}),
           **({'ONTO_WEB_PORT': '5110'} if setup['mode'] == 'legacy' else {}), **overrides}
    result = run_script(args, env)
    return result.returncode, result.stdout + result.stderr


def independent_setup(tmp_path, mode):
    setup = _prepare(tmp_path, mode)
    consumer = setup['target'] / 'onto-agent'
    owner = tmp_path / 'independent-auth'
    (consumer / 'deploy/state').mkdir(parents=True, exist_ok=True)
    (consumer / 'backend').mkdir(parents=True, exist_ok=True)
    (owner / 'deploy/state').mkdir(parents=True)
    state = {'step': 'completed', 'ownerDir': str(owner), 'container': 'auth-owner',
             'consumerNetworks': {'default': 'onto-agent_default'}}
    owner_record = json.dumps(state)
    (consumer / 'deploy/state/auth-owner.json').write_text(owner_record)
    (owner / 'deploy/state/ownership.json').write_text(owner_record)
    record_path = tmp_path / 'auth-record.json'
    record_path.write_text(owner_record)
    setup['env'].update(STUB_AUTH_RECORD=str(record_path), STUB_AUTH_OWNER=str(owner))
    with (consumer / 'backend/.env').open('a') as stream:
        stream.write('AUTH_SERVER_URL=http://auth-server:5056\n'
                     'AUTH_DEPLOYMENT_MODE=external\n'
                     f'AUTH_SERVER_ADMIN_TOKEN={AUTH_ADMIN_TOKEN}\n'
                     'AUTH_SERVER_ISSUER=https://auth.example.com\n'
                     'AUTH_SERVER_AUDIENCE=synthetic\n')
    verifier = '''#!/usr/bin/env python3
import os
import sys
from pathlib import Path
if sys.argv[1] not in ('verify', 'health', 'consumer-health'):
    raise SystemExit(1)
if sys.argv[1] in ('health', 'consumer-health'):
    calls_file = Path(os.environ['CALLS_FILE'])
    calls = calls_file.read_text() if calls_file.exists() else ''
    if 'consumer-install' not in calls and 'consumer-upgrade' not in calls:
        raise SystemExit(1)
    if sys.argv[1] == 'consumer-health':
        Path(str(calls_file) + '.auth-health').write_text(calls)
'''
    (consumer / 'deploy/auth_external.py').write_text(verifier)
    package = setup['packages'][0]
    stage = package.parent / (package.name + '.stage') / 'onto-agent'
    (stage / 'deploy/manifest.json').write_text(json.dumps({'auth_deployment_mode': 'external'}))
    (stage / 'deploy/docker-compose.release.yml').write_text(json.dumps({'services': {'backend': {}}, 'volumes': {}}))
    (stage / 'deploy/auth_external.py').write_text(verifier)
    (stage / 'deploy/auth_external.sh').write_text('# synthetic\n')
    (stage / 'deploy/auth_upgrade.py').write_text('# synthetic\n')
    (stage / 'deploy/auth_hooks.py').write_text('# synthetic\n')
    (stage / 'deploy/auth_contract.py').write_text('# synthetic\n')
    (stage / 'deploy/deploy_support.py').write_text('# synthetic\n')
    (stage / 'deploy/common.sh').write_text('external_auth_enabled() { :; }\n')
    _repack(package)
    docker = tmp_path / 'bin/docker'
    original = docker.read_text()
    _write_executable(docker, '''
if [ "$1" = container ] && [ "$2" = inspect ] && [ "${3:-}" = auth-owner ]; then
    [ "${STUB_AUTH_MISSING:-0}" != 1 ] || exit 1
    case "$*" in *'.Image'*) printf '%s\\n' "${STUB_AUTH_CURRENT_IMAGE:-sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}" ;; esac
    case "$*" in *NetworkSettings.Networks*) printf 'onto-agent_default\\n' ;; esac
    case "$*" in *Aliases*) printf 'auth-server\\n' ;; esac
    case "$*" in *State.Running*)
        if [ "${STUB_AUTH_STOPPED:-0}" = 1 ] && [ ! -f "${CALLS_FILE}.auth-started" ]; then
            printf 'false\\n'
        else
            printf 'true\\n'
        fi ;;
    esac
    exit 0
fi
if [ "$1" = start ] && [ "${2:-}" = auth-owner ]; then
    printf 'auth-start\\n' >> "$CALLS_FILE"
    [ "${STUB_AUTH_START_FAIL:-0}" != 1 ] || exit 1
    : > "${CALLS_FILE}.auth-started"
    exit 0
fi
if [ "$1" = stop ] && [ "${3:-}" = 30 ] && [ "${4:-}" = auth-owner ]; then
    printf 'auth-stop\\n' >> "$CALLS_FILE"
    exit "${STUB_AUTH_STOP_RC:-0}"
fi
if [ "$1" = exec ] && [ "${2:-}" = -i ] && [ "${3:-}" = auth-owner ]; then
    read -r token || :
    [ "$token" = 'synthetic-auth-admin-token-00000000000000000000' ]
    exit $?
fi
if [ "$1" = exec ] && [ "${2:-}" = auth-owner ]; then
    printf 'https://auth.example.com\\nsynthetic\\n'
    exit 0
fi
if [ "$1" = container ] && [ "$2" = inspect ] && [ "${3:-}" = onto-agent-backend ] && [ "${5:-}" = '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' ]; then
    printf 'unrelated-network\\nonto-agent_default\\n'
    exit 0
fi
''' + original)
    setup['auth_package'] = setup.get('auth_package') or _auth_package(tmp_path)
    return setup, owner_record


def test_first_install_preserves_auth_owner_record_and_never_stops_auth(tmp_path):
    setup, owner_record = independent_setup(tmp_path, 'install')
    status, output = _run(setup, interactive=False)
    assert status == 0, output
    assert (setup['target'] / 'onto-agent/deploy/state/auth-owner.json').read_text() == owner_record
    assert 'CONSUMER_NETWORK=onto-agent_default' in (tmp_path / 'knowledge-hub.snapshot').read_text()
    assert _calls(setup)[0] == 'onto-preflight'
    assert _calls(setup).index('auth-upgrade') < _calls(setup).index('consumer-install')
    assert _calls(setup)[-4:] == ['consumer-install', 'hub-deploy', 'onto-deploy', 'evaluation-compose']
    onto_env = (setup['target'] / 'onto-modeling/.env').read_text()
    assert f'ONTO_WEB_AUTH_ADMIN_TOKEN={AUTH_ADMIN_TOKEN}' in onto_env
    stopped = tmp_path / 'calls.txt.stopped'
    assert not stopped.exists() or 'auth-owner' not in stopped.read_text()


def test_upgrade_from_missing_directory_fails_without_installing(tmp_path):
    setup, _ = independent_setup(tmp_path, 'legacy')
    absent = tmp_path / 'missing' / 'onto-agent'
    setup['legacy'] = absent
    status, output = _run(setup)
    assert status != 0
    assert '--upgrade-from 指向的 Consumer 安装目录不存在' in output
    assert _calls(setup) == []


def test_fresh_install_creates_auth_from_package_before_consumer(tmp_path):
    setup, owner_record = independent_setup(tmp_path, 'install')
    consumer_state = setup['target'] / 'onto-agent/deploy/state/auth-owner.json'
    consumer_state.unlink()
    (tmp_path / 'independent-auth/deploy/state/ownership.json').unlink()
    status, output = _run(setup, interactive=False)
    assert status == 0, output
    assert _calls(setup).index('auth-install') < _calls(setup).index('consumer-install')
    assert 'consumer-install' in (tmp_path / 'calls.txt.auth-health').read_text()
    assert consumer_state.read_text() == owner_record
    owner_env = tmp_path / 'independent-auth/.env'
    values = dict(line.split('=', 1) for line in owner_env.read_text().splitlines())
    assert owner_env.stat().st_mode & 0o777 == 0o600
    assert values['AUTH_SERVER_ISSUER'] == 'http://auth-server:5056'
    assert values['AUTH_SERVER_AUDIENCE'] == 'knowledge-scaffold'
    assert len(values['AUTH_SERVER_ADMIN_TOKEN']) >= 32
    assert len(values['AUTH_BOOTSTRAP_ADMIN_PASSWORD']) >= 8
    assert values['AUTH_SERVER_ADMIN_TOKEN'] not in output
    assert values['AUTH_BOOTSTRAP_ADMIN_PASSWORD'] not in output


def test_fresh_install_preserves_existing_auth_identity_and_credentials(tmp_path):
    setup, _ = independent_setup(tmp_path, 'install')
    (setup['target'] / 'onto-agent/deploy/state/auth-owner.json').unlink()
    owner_env = tmp_path / 'independent-auth/.env'
    owner_env.write_text('AUTH_SERVER_ISSUER=https://auth.example.com\n'
                         'AUTH_SERVER_AUDIENCE=custom-audience\n'
                         'AUTH_SERVER_ADMIN_TOKEN=custom-private-token-value-at-least-32-characters\n'
                         'AUTH_BOOTSTRAP_ADMIN_PASSWORD=CustomPrivatePassword123!\n')
    owner_env.chmod(0o600)
    before = owner_env.read_bytes()
    status, output = _run(setup, interactive=False)
    assert status == 0, output
    assert owner_env.read_bytes() == before


def test_fresh_install_generates_missing_fields_without_overwriting_existing_values(tmp_path):
    setup, _ = independent_setup(tmp_path, 'install')
    (setup['target'] / 'onto-agent/deploy/state/auth-owner.json').unlink()
    owner_env = tmp_path / 'independent-auth/.env'
    owner_env.write_text('AUTH_SERVER_ISSUER=https://auth.example.com\n'
                         'AUTH_SERVER_AUDIENCE=\n')
    owner_env.chmod(0o600)
    status, output = _run(setup, interactive=False)
    assert status == 0, output
    values = dict(line.split('=', 1) for line in owner_env.read_text().splitlines())
    assert values['AUTH_SERVER_ISSUER'] == 'https://auth.example.com'
    assert values['AUTH_SERVER_AUDIENCE'] == 'knowledge-scaffold'
    assert len(values['AUTH_SERVER_ADMIN_TOKEN']) >= 32
    assert len(values['AUTH_BOOTSTRAP_ADMIN_PASSWORD']) >= 8


def test_fresh_install_rejects_unsafe_auth_owner_env_permissions(tmp_path):
    setup, _ = independent_setup(tmp_path, 'install')
    (setup['target'] / 'onto-agent/deploy/state/auth-owner.json').unlink()
    owner_env = tmp_path / 'independent-auth/.env'
    owner_env.write_text('AUTH_SERVER_ISSUER=https://auth.example.com\n')
    owner_env.chmod(0o644)
    status, output = _run(setup, interactive=False)
    assert status != 0
    assert '权限必须为 0600' in output
    assert 'consumer-install' not in _calls(setup)


def test_auth_package_is_required_before_any_change(tmp_path):
    setup, _ = independent_setup(tmp_path, 'upgrade')
    args = [str(DEPLOY_ROOT / 'install-consumer-modeling.sh'), 'upgrade',
            '--consumer-package', str(setup['packages'][0]),
            '--onto-modeling-package', str(setup['packages'][2]),
            '--knowledge-hub-package', str(setup['hub_package']),
            '--evaluation-package', str(setup['evaluation_package'])]
    result = run_script(args, setup['env'])
    assert result.returncode != 0
    assert '必须指定 --auth-package' in result.stderr
    assert _calls(setup) == []


def test_upgrade_rejects_consumer_auth_mode_before_auth_change(tmp_path):
    setup, _ = independent_setup(tmp_path, 'upgrade')
    consumer_env = setup['target'] / 'onto-agent/backend/.env'
    consumer_env.write_text(consumer_env.read_text().replace('AUTH_DEPLOYMENT_MODE=external',
                                                             'AUTH_DEPLOYMENT_MODE=internal'))
    status, output = _run(setup, interactive=False)
    assert status != 0
    assert 'Consumer 未配置 external Auth 模式' in output
    assert 'auth-upgrade' not in _calls(setup)
    assert 'consumer-upgrade' not in _calls(setup)


def test_upgrade_rejects_consumer_admin_token_mismatch_before_auth_change(tmp_path):
    setup, _ = independent_setup(tmp_path, 'upgrade')
    consumer_env = setup['target'] / 'onto-agent/backend/.env'
    consumer_env.write_text(consumer_env.read_text().replace(AUTH_ADMIN_TOKEN, 'synthetic-wrong-token'))
    status, output = _run(setup, interactive=False)
    assert status != 0
    assert 'Consumer Auth 管理令牌与独立 owner 不一致' in output
    assert 'auth-upgrade' not in _calls(setup)
    assert 'consumer-upgrade' not in _calls(setup)


def test_upgrade_rejects_mismatched_onto_identity_before_consumer_change(tmp_path):
    setup, owner_record = independent_setup(tmp_path, 'upgrade')
    onto = setup['target'] / 'onto-modeling'
    onto.mkdir(exist_ok=True)
    (onto / '.env').write_text('ONTO_WEB_AUTH_ISSUER=https://wrong.example.com\n')
    status, output = _run(setup, interactive=False)
    assert status != 0
    assert '与独立 Auth 不一致' in output
    assert 'consumer-upgrade' not in _calls(setup)
    assert 'auth-stop' not in _calls(setup)
    assert (setup['target'] / 'onto-agent/deploy/state/auth-owner.json').read_text() == owner_record


def test_upgrade_rejects_mismatched_onto_admin_token_before_auth_change(tmp_path):
    setup, _ = independent_setup(tmp_path, 'upgrade')
    onto = setup['target'] / 'onto-modeling'
    onto.mkdir(exist_ok=True)
    (onto / '.env').write_text('ONTO_WEB_AUTH_ADMIN_TOKEN=synthetic-wrong-token\n')
    status, output = _run(setup, interactive=False)
    assert status != 0
    assert 'Onto ONTO_WEB_AUTH_ADMIN_TOKEN 与独立 Auth 不一致' in output
    assert 'auth-upgrade' not in _calls(setup)
    assert 'consumer-upgrade' not in _calls(setup)


def test_install_rejects_mismatched_onto_identity_before_auth_upgrade(tmp_path):
    setup, _ = independent_setup(tmp_path, 'install')
    onto = setup['target'] / 'onto-modeling'
    onto.mkdir(exist_ok=True)
    (onto / '.env').write_text('ONTO_WEB_AUTH_ISSUER=https://wrong.example.com\n')
    status, output = _run(setup, interactive=False)
    assert status != 0
    assert '与独立 Auth 不一致' in output
    assert 'auth-upgrade' not in _calls(setup)


def test_upgrade_uses_auth_package_before_consumer(tmp_path):
    setup, owner_record = independent_setup(tmp_path, 'upgrade')
    status, output = _run(setup, interactive=False)
    assert status == 0, output
    calls = _calls(setup)
    assert calls.index('consumer-upgrade') < calls.index('hub-deploy') < calls.index('onto-deploy')
    assert calls[-1] == 'evaluation-compose'
    assert calls.index('auth-upgrade') < calls.index('consumer-upgrade')
    assert 'auth-stop' not in calls
    assert (setup['target'] / 'onto-agent/deploy/state/auth-owner.json').read_text() == owner_record


def test_legacy_upgrade_uses_auth_package_before_consumer(tmp_path):
    setup, _ = independent_setup(tmp_path, 'legacy')
    status, output = _run(setup, interactive=False)
    assert status == 0, output
    calls = _calls(setup)
    assert calls.index('auth-upgrade') < calls.index('consumer-upgrade')


def test_legacy_upgrade_migrates_merged_auth_before_package_upgrade(tmp_path):
    setup, _ = independent_setup(tmp_path, 'legacy')
    state = setup['target'] / 'onto-agent/deploy/state/auth-owner.json'
    state.unlink()
    status, output = _run(setup, interactive=False)
    assert status == 0, output
    calls = _calls(setup)
    assert calls.index('auth-migrate') < calls.index('auth-upgrade') < calls.index('consumer-upgrade')
    assert json.loads(state.read_text())['step'] == 'completed'
    backup = setup['target'] / 'onto-agent/deploy/backups/auth-migration-tools'
    assert backup.is_dir()


def test_legacy_auth_migration_failure_blocks_all_package_upgrades(tmp_path):
    setup, _ = independent_setup(tmp_path, 'legacy')
    (setup['target'] / 'onto-agent/deploy/state/auth-owner.json').unlink()
    status, output = _run(setup, interactive=False, STUB_AUTH_MIGRATE_RC='1')
    assert status != 0
    assert '旧 Auth 所有权迁移失败' in output
    calls = _calls(setup)
    assert 'auth-migrate' in calls
    assert 'auth-upgrade' not in calls
    assert 'consumer-upgrade' not in calls


def test_legacy_auth_migration_resumes_recorded_owner(tmp_path):
    setup, _ = independent_setup(tmp_path, 'legacy')
    state_path = setup['target'] / 'onto-agent/deploy/state/auth-owner.json'
    state = json.loads(state_path.read_text())
    state.update(step='backed-up', targetProject='recorded-auth')
    state_path.write_text(json.dumps(state))
    status, output = _run(setup, interactive=False)
    assert status == 0, output
    calls = _calls(setup)
    assert calls.index('auth-migrate-resume') < calls.index('auth-upgrade')
    assert json.loads(state_path.read_text())['targetProject'] == 'recorded-auth'
    assert (setup['target'] / 'onto-agent/deploy/deploy_support.py').is_file()


def test_install_starts_stopped_independent_auth_before_consumer(tmp_path):
    setup, _ = independent_setup(tmp_path, 'install')
    status, output = _run(setup, interactive=False, STUB_AUTH_STOPPED='1')
    assert status == 0, output
    calls = _calls(setup)
    assert calls.index('auth-start') < calls.index('consumer-install')
    assert calls.count('auth-start') == 1


def test_upgrade_starts_stopped_independent_auth_before_consumer(tmp_path):
    setup, _ = independent_setup(tmp_path, 'upgrade')
    status, output = _run(setup, interactive=False, STUB_AUTH_STOPPED='1')
    assert status == 0, output
    calls = _calls(setup)
    assert calls.index('auth-start') < calls.index('consumer-upgrade')
    assert calls.count('auth-start') == 1
    assert calls.index('auth-upgrade') < calls.index('consumer-upgrade')


def test_missing_auth_owner_container_does_not_install_services(tmp_path):
    setup, _ = independent_setup(tmp_path, 'upgrade')
    status, output = _run(setup, interactive=False, STUB_AUTH_MISSING='1')
    assert status != 0
    assert '独立 Auth 容器不存在' in output
    assert 'consumer-upgrade' not in _calls(setup)
    assert 'auth-start' not in _calls(setup)


def test_auth_start_failure_blocks_consumer_install(tmp_path):
    setup, _ = independent_setup(tmp_path, 'install')
    status, output = _run(setup, interactive=False, STUB_AUTH_STOPPED='1', STUB_AUTH_START_FAIL='1')
    assert status != 0
    assert '独立 Auth 容器启动失败' in output
    assert 'consumer-install' not in _calls(setup)


def test_auth_package_upgrade_failure_blocks_consumer_upgrade(tmp_path):
    setup, _ = independent_setup(tmp_path, 'upgrade')
    status, output = _run(setup, interactive=False, STUB_AUTH_UPGRADE_RC='1')
    assert status != 0
    assert '独立 Auth 升级失败' in output
    assert 'auth-upgrade' in _calls(setup)
    assert 'consumer-upgrade' not in _calls(setup)


def test_auth_same_image_skips_upgrade(tmp_path):
    setup, _ = independent_setup(tmp_path, 'upgrade')
    status, output = _run(setup, interactive=False, STUB_AUTH_CURRENT_IMAGE=AUTH_IMAGE)
    assert status == 0, output
    assert 'auth-upgrade' not in _calls(setup)
    assert 'consumer-upgrade' in _calls(setup)


def test_tampered_auth_package_blocks_all_service_changes(tmp_path):
    setup, _ = independent_setup(tmp_path, 'upgrade')
    package = setup['auth_package']
    stage = tmp_path / 'auth-stage/onto-auth-test-amd64'
    (stage / 'images/auth-server.tar').write_text('changed after manifest')
    with tarfile.open(package, 'w:gz') as archive:
        archive.add(stage, arcname=stage.name)
    status, output = _run(setup, interactive=False)
    assert status != 0
    assert '发布包哈希或元数据校验失败' in output
    assert _calls(setup) == []


def test_legacy_auth_package_is_rejected_before_owner_check(tmp_path):
    setup, _ = independent_setup(tmp_path, 'upgrade')
    package = setup['packages'][0]
    stage = package.parent / (package.name + '.stage') / 'onto-agent'
    (stage / 'deploy/manifest.json').write_text(json.dumps({'auth_deployment_mode': 'internal'}))
    _repack(package)
    args = [str(DEPLOY_ROOT / 'install-consumer-modeling.sh'), 'upgrade',
            '--consumer-package', str(package), '--auth-package', str(setup['auth_package']),
            '--onto-modeling-package', str(setup['packages'][2]),
            '--knowledge-hub-package', str(setup['hub_package']),
            '--evaluation-package', str(setup['evaluation_package'])]
    result = run_script(args, setup['env'])
    assert result.returncode != 0
    assert 'external Consumer' in result.stderr
    assert _calls(setup) == []


def test_onto_deploy_checks_auth_before_creating_runtime_configuration(tmp_path):
    release = tmp_path / 'onto-modeling'
    release.mkdir()
    shutil.copy2(REPO_ROOT / 'modeling/onto-modeling/deploy/deploy.sh', release / 'deploy.sh')
    _write_executable(release / 'preflight.sh', 'exit 0')
    (release / '.env.example').write_text(
        'ONTO_MODELING_EXTERNAL_AUTH_NETWORK=synthetic-network\n'
        'ONTO_MODELING_AUTH_URL=http://auth-server:5056\n'
        'ONTO_WEB_AUTH_ISSUER=https://auth.example.com\n'
        'ONTO_WEB_AUTH_AUDIENCE=synthetic\n')
    (release / 'images.env').write_text('ONTO_WEB_IMAGE=onto-modeling:synthetic\n')
    fake_bin = tmp_path / 'bin'
    _write_executable(fake_bin / 'docker', '''
if [ "$1 $2 $3" = 'container inspect onto-modeling-auth-server' ]; then exit 1; fi
if [ "$1 $2" = 'container inspect' ]; then exit 1; fi
if [ "$1 $2" = 'volume inspect' ]; then exit 1; fi
if [ "$1 $2" = 'network inspect' ] && [ "$4" = '--format' ]; then echo true; exit 0; fi
if [ "$1" = run ]; then exit 1; fi
exit 0
''')
    result = subprocess.run(['bash', str(release / 'deploy.sh')],
                            env={**os.environ, 'PATH': f"{fake_bin}:{os.environ['PATH']}"},
                            text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert 'Auth 或 Consumer 依赖配置无效' in result.stderr
    assert not (release / '.env').exists()
    assert not (release / 'docker-compose.external-auth.yml').exists()


def test_onto_deploy_fills_missing_auth_defaults_before_compose(tmp_path):
    release = tmp_path / 'onto-modeling'
    release.mkdir()
    shutil.copy2(REPO_ROOT / 'modeling/onto-modeling/deploy/deploy.sh', release / 'deploy.sh')
    _write_executable(release / 'preflight.sh', 'exit 0')
    (release / '.env').write_text('ONTO_MODELING_AUTH_URL=\nONTO_WEB_AUTH_ISSUER=\n')
    (release / 'images.env').write_text('ONTO_WEB_IMAGE=onto-modeling:synthetic\n')
    (release / 'dependency_probe.py').write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv
data = {name: args[args.index(name) + 1] for name in
        ('--network', '--auth-url', '--issuer', '--audience')}
Path(args[args.index('--state-file') + 1]).write_text(
    json.dumps({'phase': 'waiting-dependencies', 'probe': data}))
''')
    fake_bin = tmp_path / 'bin'
    _write_executable(fake_bin / 'docker', '''
if [ "$1 $2" = 'container inspect' ] || [ "$1 $2" = 'volume inspect' ]; then exit 1; fi
if [ "$1 $2" = 'network inspect' ]; then
    if [ "${4:-}" = '--format' ]; then echo true; exit 0; fi
    [ -f "$DOCKER_TEST_NETWORK_MARKER" ]; exit $?
fi
if [ "$1 $2" = 'network create' ]; then touch "$DOCKER_TEST_NETWORK_MARKER"; exit 0; fi
exit 0
''')
    marker = tmp_path / 'network-created'
    result = subprocess.run(['bash', str(release / 'deploy.sh')],
                            env={**os.environ, 'PATH': f"{fake_bin}:{os.environ['PATH']}",
                                 'DOCKER_TEST_NETWORK_MARKER': str(marker)},
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert marker.exists()
    values = dict(line.split('=', 1) for line in (release / '.env').read_text().splitlines()
                  if '=' in line and not line.startswith('#'))
    expected = {'ONTO_MODELING_EXTERNAL_AUTH_NETWORK': 'onto-auth-network',
                'ONTO_MODELING_AUTH_URL': 'http://auth-server:5056',
                'ONTO_WEB_AUTH_ISSUER': 'http://auth-server:5056',
                'ONTO_WEB_AUTH_AUDIENCE': 'knowledge-scaffold'}
    assert {key: values[key] for key in expected} == expected
    assert json.loads((release / '.dependencies.json').read_text())['probe'] == {
        '--network': expected['ONTO_MODELING_EXTERNAL_AUTH_NETWORK'],
        '--auth-url': expected['ONTO_MODELING_AUTH_URL'],
        '--issuer': expected['ONTO_WEB_AUTH_ISSUER'],
        '--audience': expected['ONTO_WEB_AUTH_AUDIENCE']}
