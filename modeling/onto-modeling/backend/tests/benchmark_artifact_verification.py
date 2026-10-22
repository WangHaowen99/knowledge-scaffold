"""Local synthetic benchmark; no server/model calls. Run from onto-modeling/.

Compares actual base-branch checkers with new cold checks and cached receipt reads.
All generated workspaces and receipts live in an automatically removed temp directory.
"""
from __future__ import annotations

import argparse
from functools import partial
import json
import math
from pathlib import Path
import statistics
import shutil
import subprocess
import sys
import tempfile
import time

from test_artifact_verification import diagram_xml, object_doc, operation_doc, rule_doc, metric_doc, put

SCRIPTS = Path(__file__).resolve().parents[2] / 'assets/workspace-toolkit/scripts'
PREFIX = SCRIPTS.relative_to(Path(__file__).resolve().parents[4]).as_posix()
MAX_BASELINE_FILES = 500
MAX_BASELINE_BYTES = 16 * 1024 * 1024


def measure(fn, count=15):
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {'median_ms': round(statistics.median(samples), 2),
            'p95_ms': round(sorted(samples)[math.ceil(len(samples)*.95)-1], 2), 'runs': count}


def cli(script, args, root):
    if not script.is_absolute() or not script.is_file():
        raise ValueError('Checker must be an existing absolute file path')
    # Local checker path and argv are separate; no shell or command-string parsing.
    result = subprocess.run([sys.executable, '-B', str(script), *args], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            shell=False, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr[:500] + result.stdout[:500])
    return result.stdout


def git_output(repo, *args):
    executable = shutil.which('git')
    if not executable:
        raise RuntimeError('Git is required for the baseline comparison')
    # Only the fixed read-only Git operations below call this helper.
    return subprocess.check_output([str(Path(executable).resolve()), *args], cwd=repo,
                                   shell=False, timeout=60)


def baseline_scripts(tmp, repo, commit):
    # Read bounded, immutable blobs directly; no archive expansion or symlink extraction.
    listing = git_output(repo, 'ls-tree', '-rlz', commit, '--', PREFIX)
    files, total = [], 0
    for entry in listing.split(b'\0'):
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        mode, kind, blob, size = metadata.decode('ascii').split()
        path = Path(raw_path.decode('utf-8'))
        if path.parent.as_posix() != PREFIX:
            continue
        if mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError('Baseline may contain only regular script files')
        total += int(size)
        if len(files) >= MAX_BASELINE_FILES or total > MAX_BASELINE_BYTES:
            raise ValueError('Baseline scripts exceed the resource limit')
        files.append((path.name, blob, int(size)))
    if not files:
        raise ValueError('Baseline contains no scripts')
    target = tmp / 'baseline'
    target.mkdir()
    for name, blob, size in files:
        data = git_output(repo, 'cat-file', 'blob', blob)
        if len(data) != size:
            raise ValueError('Baseline blob size changed')
        (target / name).write_bytes(data)
    return target


def cold_objects(root, sequence):
    put(root, 'ontology/object/Entity0.md', object_doc('Entity0', 'ID',
        '- 当前对象 → links（ID → ID） → [[Entity1]]') + f'\n<!-- {next(sequence)} -->\n')
    return cli(SCRIPTS / 'verify_artifacts.py', ['objects'], root)


def phase_sequence(root, sequence):
    cold_objects(root, sequence)
    cli(SCRIPTS / 'verify_artifacts.py', ['objects'], root)
    cli(SCRIPTS / 'verify_artifacts.py', ['objects', '--status'], root)


def cold_diagrams(root, sequence):
    put(root, 'ontology/diagrams/Flow0.drawio', diagram_xml() + f'<!-- {next(sequence)} -->')
    return cli(SCRIPTS / 'verify_artifacts.py', ['diagrams'], root)


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True, help='Explicit baseline branch or commit')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[4]
    commit = git_output(repo, 'rev-parse', '--verify', '--end-of-options',
                        args.baseline_ref + '^{commit}').decode('ascii').strip()
    result = {'python': sys.version.split()[0], 'platform': sys.platform,
              'base_commit': commit,
              'model_calls': 0, 'datasets': []}
    with tempfile.TemporaryDirectory(prefix='artifact-benchmark-') as temporary:
        tmp = Path(temporary)
        baseline = baseline_scripts(tmp, repo, commit)
        for objects, diagrams, schema_rows in ((50, 20, 0), (500, 100, 0), (500, 100, 100000)):
            root = tmp / f'synthetic-{objects}-{schema_rows}'
            for index in range(objects):
                put(root, f'ontology/object/Entity{index}.md', object_doc(f'Entity{index}', 'ID',
                    f'- 当前对象 → links（ID → ID） → [[Entity{(index+1) % objects}]]'))
            for index in range(diagrams):
                put(root, f'ontology/diagrams/Flow{index}.drawio', diagram_xml())
            for index in range(objects // 5):
                put(root, f'ontology/operation/Task{index}.md', operation_doc())
                put(root, f'ontology/rule/Rule{index}.md', rule_doc())
                put(root, f'ontology/metric/Metric{index}.md', metric_doc())
            if schema_rows:
                put(root, 'resource/table.csv', 'ID\n' + ''.join(f'{i:08}\n' for i in range(schema_rows)))
                put(root, 'resource/materials.json', json.dumps({'resource/table.csv': {'purpose': 'schema'}}))
            datum = {'objects': objects, 'diagrams': diagrams, 'operations_rules_metrics_each': objects // 5,
                     'schema_rows': schema_rows}
            datum['baseline_ontology_cli'] = measure(partial(cli, baseline / 'ontology_checker.py',
                ['ontology', '--json', '--include-mock', '--files',
                 *(f'Entity{i}' for i in range(objects))], root))
            # Unique revision per run defeats the cache without deleting evidence.
            sequence = iter(range(100))
            datum['cold_ontology_cli'] = measure(partial(cold_objects, root, sequence))
            datum['cached_ontology_cli'] = measure(partial(cli, SCRIPTS / 'verify_artifacts.py', ['objects'], root))
            datum['receipt_status_cli'] = measure(partial(cli, SCRIPTS / 'verify_artifacts.py', ['objects', '--status'], root))
            datum['phase_check_cache_status_sequence'] = measure(partial(phase_sequence, root, sequence))
            datum['baseline_diagram_cli'] = measure(partial(cli, baseline / 'check_diagrams.py', ['ontology/diagrams'], root))
            sequence = iter(range(100))
            datum['cold_diagram_cli'] = measure(partial(cold_diagrams, root, sequence))
            datum['cached_diagram_cli'] = measure(partial(cli, SCRIPTS / 'verify_artifacts.py', ['diagrams'], root))
            result['datasets'].append(datum)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write('\n')


if __name__ == '__main__':
    run()
