"""Synthetic regressions for the CodeCheck hardening, without remote services."""
from functools import partial
import importlib
import json
import logging
import sys

import pytest

from test_artifact_verification import hook, object_doc, put, SCRIPTS
from test_review_integrity import scenario_file
import benchmark_artifact_verification as benchmark


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
