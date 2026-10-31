"""Synthetic regressions for the CodeCheck hardening, without remote services."""
from functools import partial
import importlib
import json
import logging
import os
from pathlib import Path
import sys

import pytest

from test_artifact_verification import diagram_xml, git_workspace, hook, object_doc, put, SCRIPTS
from test_review_integrity import scenario_file
import benchmark_artifact_verification as benchmark
from artifact_process_policy import child_environment, system_search_path, validate_fixture_git_args


def test_baseline_reads_regular_blobs_without_archive(tmp_path, monkeypatch):
    payload = b'# synthetic checker\n'
    blob = 'a' * 40
    calls = []

    def read(repo, *args):
        calls.append(args)
        if args[0] == 'ls-tree':
            return f'100644 blob {blob} {len(payload)}\t{benchmark.PREFIX}/sample.py\0'.encode()
        assert args == ('cat-file', 'blob', blob)
        return payload

    monkeypatch.setattr(benchmark, 'git_output', read)
    target = benchmark.baseline_scripts(tmp_path, tmp_path, 'b' * 40)
    assert (target / 'sample.py').read_bytes() == payload
    assert [call[0] for call in calls] == ['ls-tree', 'cat-file']


@pytest.mark.parametrize('mode,size,limit', [('120000', 3, 100), ('100644', 101, 100)])
def test_baseline_rejects_symlinks_and_oversize_before_read(tmp_path, monkeypatch, mode, size, limit):
    def read(repo, *args):
        assert args[0] == 'ls-tree', 'Rejected metadata must not cause blob reads'
        return f'{mode} blob {"a" * 40} {size}\t{benchmark.PREFIX}/sample.py\0'.encode()

    monkeypatch.setattr(benchmark, 'git_output', read)
    monkeypatch.setattr(benchmark, 'MAX_BASELINE_BYTES', limit)
    with pytest.raises(ValueError):
        benchmark.baseline_scripts(tmp_path, tmp_path, 'b' * 40)
    assert not (tmp_path / 'baseline').exists()


def test_benchmark_callbacks_keep_each_workspace(tmp_path, monkeypatch):
    roots = [tmp_path / 'first', tmp_path / 'second']
    callbacks = [partial(benchmark.cold_objects, root, iter(range(2))) for root in roots]
    monkeypatch.setattr(benchmark, 'cli', lambda script, args, root: root)
    assert [callback() for callback in callbacks] == roots
    for root in roots:
        assert '<!-- 0 -->' in (root / 'ontology/object/Entity0.md').read_text(encoding='utf-8')


def test_hook_rejects_other_or_changed_executable(tmp_path):
    with pytest.raises(ValueError, match='Only'):
        hook(tmp_path, 'other.sh')
    put(tmp_path, '.claude/scripts/pre_commit_check.sh', '#!/bin/sh\nexit 0\n')
    with pytest.raises(ValueError, match='unchanged copy'):
        hook(tmp_path, 'pre_commit_check.sh')


@pytest.mark.parametrize('kind', ['outside', 'other-name', 'symlink', 'relative', 'missing'])
def test_benchmark_rejects_unapproved_script_before_spawn(tmp_path, monkeypatch, kind):
    script = put(tmp_path, 'verify_artifacts.py', 'raise SystemExit(0)\n')
    if kind == 'other-name':
        script = SCRIPTS / 'state_helper.py'
    elif kind == 'symlink':
        script = tmp_path / 'ontology_checker.py'
        script.symlink_to(SCRIPTS / 'ontology_checker.py')
    elif kind == 'relative':
        script = Path('verify_artifacts.py')
    elif kind == 'missing':
        script = tmp_path / 'missing' / 'verify_artifacts.py'
    monkeypatch.setattr(benchmark.subprocess, 'run', lambda *a, **k: pytest.fail('Must reject before spawn'))
    with pytest.raises(ValueError, match='allowlisted'):
        benchmark.cli(script, ['objects'], tmp_path)


@pytest.mark.parametrize('args', [
    ('fetch', 'origin'), ('-c', 'core.pager=example', 'status'),
    ('cat-file', 'blob', '--batch'), ('cat-file', 'blob', 'HEAD:path'),
    ('ls-tree', '-rlz', 'a' * 40, '--', 'other/path'),
    ('rev-parse', '--verify', '--end-of-options', '--help^{commit}'),
    ('rev-parse', '--verify', '--end-of-options', 'HEAD;other^{commit}'),
])
def test_benchmark_git_rejects_non_read_contract(monkeypatch, args):
    monkeypatch.setattr(benchmark.subprocess, 'check_output',
                        lambda *a, **k: pytest.fail('Must reject before spawn'))
    with pytest.raises(ValueError, match='allowlist'):
        benchmark.git_output(benchmark.PROJECT_ROOT, *args)


def test_benchmark_git_rejects_other_checkout(tmp_path):
    with pytest.raises(ValueError, match='this checkout'):
        benchmark.git_output(tmp_path, 'cat-file', 'blob', 'a' * 40)


@pytest.mark.parametrize('args', [
    ('push', 'origin'), ('config', 'core.hooksPath', 'other'),
    ('add', '../other'), ('add', '--all'), ('commit', '-qm', 'unexpected'),
])
def test_fixture_git_rejects_arbitrary_operations(args):
    with pytest.raises(ValueError, match='allowlist'):
        validate_fixture_git_args(args)


def test_child_environment_does_not_forward_host_settings(tmp_path, monkeypatch):
    poison = ('BASH_ENV', 'ENV', 'PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD',
              'DYLD_INSERT_LIBRARIES', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
              'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_KEY_99', 'SYNTHETIC_SECRET')
    for key in poison:
        monkeypatch.setenv(key, 'synthetic-host-value')
    monkeypatch.setenv('PATH', str(tmp_path / 'untrusted-bin'))
    monkeypatch.setenv('GIT_CONFIG_COUNT', '100')
    env = child_environment(tmp_path)
    assert not set(poison).intersection(env)
    assert 'untrusted-bin' not in env['PATH']
    assert env['GIT_CONFIG_GLOBAL'] == os.devnull
    assert env['GIT_CONFIG_NOSYSTEM'] == '1'
    assert env['GIT_CONFIG_COUNT'] == '6'
    assert Path(env['HOME']).is_relative_to(tmp_path)


@pytest.mark.parametrize('with_absolute', [False, True])
def test_tool_search_path_never_includes_current_or_relative_directory(tmp_path, monkeypatch, with_absolute):
    absolute = str(tmp_path / 'synthetic-system-bin') if with_absolute else ''
    monkeypatch.setattr(os, 'defpath', os.pathsep.join(('.', '', 'relative-bin', absolute)))
    assert system_search_path() == absolute
    assert all(os.path.isabs(part) for part in child_environment(tmp_path)['PATH'].split(os.pathsep))


def test_real_git_and_hooks_ignore_host_config_and_command_text(tmp_path, monkeypatch, request):
    marker = tmp_path / 'unexpected-execution'
    startup = put(tmp_path, 'host-startup.sh', f'#!/bin/sh\ntouch "{marker}"\n')
    startup.chmod(0o700)
    hook_dir = tmp_path / 'host-hooks'
    hook_dir.mkdir()
    (hook_dir / 'pre-commit').symlink_to(startup)
    config = put(tmp_path, 'host.gitconfig', f'[core]\n hooksPath = {hook_dir}\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'not-a-repo'))
    monkeypatch.setenv('GIT_WORK_TREE', str(tmp_path / 'other-worktree'))
    monkeypatch.setenv('BASH_ENV', str(startup))
    monkeypatch.setenv('PATH', str(tmp_path / 'not-a-bin'))
    root, git = request.getfixturevalue('git_workspace')
    put(root, 'ontology/object/EntityA.md', object_doc())
    git('add', 'ontology/object')
    response = hook(root, 'pre_commit_check.sh', f'git commit -m "$(touch {marker})"')
    assert 'block' not in response.stdout
    git('commit', '-qm', 'extract: synthetic objects')
    assert '产物轻量核验通过' in hook(root, 'post_commit_hook.sh').stdout
    assert not marker.exists()


def test_benchmark_real_current_and_baseline_cli(tmp_path):
    repo = benchmark.PROJECT_ROOT
    if not (repo / '.git').exists():
        pytest.skip('standalone onto-modeling source has no Git checkout for baseline comparison')
    commit = benchmark.git_output(repo, 'rev-parse', '--verify', '--end-of-options',
                                  'HEAD^{commit}').decode('ascii').strip()
    baseline = benchmark.baseline_scripts(tmp_path, repo, commit)
    root = tmp_path / 'synthetic'
    put(root, 'ontology/object/EntityA.md', object_doc())
    current = json.loads(benchmark.cli(SCRIPTS / 'verify_artifacts.py', ['objects'], root))
    previous = json.loads(benchmark.cli(baseline / 'verify_artifacts.py', ['objects'], root,
                                        baseline=baseline))
    assert current['status'] == previous['status'] == 'passed'
    assert json.loads(benchmark.cli(SCRIPTS / 'verify_artifacts.py', ['objects', '--status'], root))['cached']


@pytest.mark.parametrize('issues_json', [False, True])
def test_verifier_stdout_remains_json(tmp_path, monkeypatch, capsys, issues_json):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    verifier = importlib.import_module('verify_artifacts')
    put(tmp_path, 'ontology/object/EntityA.md', object_doc())
    monkeypatch.chdir(tmp_path)
    args = ['verify_artifacts.py', 'objects']
    if issues_json:
        args.append('--issues-json')
    monkeypatch.setattr(sys, 'argv', args)
    assert verifier.main() == 0
    output = capsys.readouterr()
    # Existing checker progress stays on stderr; stdout must contain only the protocol.
    assert 'Rule checks done' in output.err
    assert output.out.endswith('\n')
    parsed = json.loads(output.out)
    if issues_json:
        assert isinstance(parsed, list)
    else:
        assert parsed['status'] == 'passed'


def test_verifier_error_is_not_json_stdout(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    verifier = importlib.import_module('verify_artifacts')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['verify_artifacts.py', 'objects', '--files', 'missing'])
    assert verifier.main() == 2
    assert capsys.readouterr().out == ''
    assert '轻量核验未完成' in caplog.text


@pytest.mark.parametrize('module_name', ['source_extract', 'trace_inputs'])
@pytest.mark.parametrize('missing', [False, True])
def test_json_cli_isolated_from_root_handlers_and_repeated_calls(
        tmp_path, monkeypatch, capsys, module_name, missing):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    module = importlib.import_module(module_name)
    source = scenario_file(tmp_path) if module_name == 'trace_inputs' else put(tmp_path, 'source.md', '中文来源')
    if missing:
        source = tmp_path / 'missing.md'
    # Host applications may use restrictive, prefixed root loggers. CLI JSON must
    # still be emitted exactly once per call without inheriting those handlers.
    monkeypatch.setattr(logging.getLogger(), 'level', logging.CRITICAL)
    monkeypatch.setattr(sys, 'argv', [module_name + '.py', str(source)])
    expected_code = {'trace_inputs': 1, 'source_extract': 2}[module_name] if missing else 0
    expected_status = ({'trace_inputs': 'failed', 'source_extract': 'unverifiable'}[module_name]
                       if missing else {'trace_inputs': 'passed', 'source_extract': 'ok'}[module_name])
    for _ in range(2):
        assert module.main() == expected_code
        captured = capsys.readouterr()
        assert captured.out.endswith('\n')
        payload = json.loads(captured.out)
        assert payload['status'] == expected_status
        assert captured.err == ''


@pytest.mark.parametrize('invalid', [False, True])
@pytest.mark.parametrize('issues_json', [False, True])
def test_verifier_json_channel_isolated_from_root_handlers_and_repeated_calls(
        tmp_path, monkeypatch, capsys, invalid, issues_json):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    verifier = importlib.import_module('verify_artifacts')
    diagram = diagram_xml().replace('id="ab"', 'id="a"') if invalid else diagram_xml()
    put(tmp_path, 'ontology/diagrams/FlowA.drawio', diagram)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(logging.getLogger(), 'level', logging.CRITICAL)
    args = ['verify_artifacts.py', 'diagrams']
    if issues_json:
        args.append('--issues-json')
    monkeypatch.setattr(sys, 'argv', args)
    for _ in range(2):
        assert verifier.main() == (1 if invalid else 0)
        captured = capsys.readouterr()
        assert captured.out.endswith('\n')
        payload = json.loads(captured.out)
        if issues_json:
            assert any(issue['severity'] == '错误' for issue in payload) == invalid
        else:
            assert payload['status'] == ('failed' if invalid else 'passed')
