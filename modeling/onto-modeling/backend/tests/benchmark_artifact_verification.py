"""Local synthetic benchmark; no server/model calls. Run from onto-modeling/.

Compares actual base-branch checkers with new cold checks and cached receipt reads.
All generated workspaces and receipts live in an automatically removed temp directory.
"""
from __future__ import annotations

import io
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

from test_artifact_verification import diagram_xml, object_doc, operation_doc, rule_doc, metric_doc, put

SCRIPTS = Path(__file__).resolve().parents[2] / 'assets/workspace-toolkit/scripts'


def measure(fn, count=15):
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {'median_ms': round(statistics.median(samples), 2),
            'p95_ms': round(sorted(samples)[math.ceil(len(samples)*.95)-1], 2), 'runs': count}


def cli(script, args, root):
    result = subprocess.run([sys.executable, '-B', str(script), *args], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[:500] + result.stdout[:500])
    return result.stdout


def baseline_scripts(tmp, repo):
    prefix = 'modeling/onto-modeling/assets/workspace-toolkit/scripts'
    data = subprocess.check_output(['git', 'archive', '0.26.10.1', prefix], cwd=repo)
    target = tmp / 'baseline'
    target.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            if member.isfile() and Path(member.name).parent.as_posix() == prefix:
                (target / Path(member.name).name).write_bytes(archive.extractfile(member).read())
    return target


def run():
    repo = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
    result = {'python': sys.version.split()[0], 'platform': sys.platform,
              'base_commit': subprocess.check_output(['git', 'rev-parse', '0.26.10.1'], cwd=repo, text=True).strip(),
              'model_calls': 0, 'datasets': []}
    with tempfile.TemporaryDirectory(prefix='artifact-benchmark-') as temporary:
        tmp = Path(temporary)
        baseline = baseline_scripts(tmp, repo)
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
            datum['baseline_ontology_cli'] = measure(lambda: cli(baseline / 'ontology_checker.py',
                ['ontology', '--json', '--include-mock', '--files',
                 *(f'Entity{i}' for i in range(objects))], root))
            # Unique revision per run defeats the cache without deleting evidence.
            sequence = iter(range(100))
            def cold_objects():
                put(root, 'ontology/object/Entity0.md', object_doc('Entity0', 'ID',
                    '- 当前对象 → links（ID → ID） → [[Entity1]]') + f'\n<!-- {next(sequence)} -->\n')
                return cli(SCRIPTS / 'verify_artifacts.py', ['objects'], root)
            datum['cold_ontology_cli'] = measure(cold_objects)
            datum['cached_ontology_cli'] = measure(lambda: cli(SCRIPTS / 'verify_artifacts.py', ['objects'], root))
            datum['receipt_status_cli'] = measure(lambda: cli(SCRIPTS / 'verify_artifacts.py', ['objects', '--status'], root))
            def phase_sequence():
                cold_objects()
                cli(SCRIPTS / 'verify_artifacts.py', ['objects'], root)
                cli(SCRIPTS / 'verify_artifacts.py', ['objects', '--status'], root)
            datum['phase_check_cache_status_sequence'] = measure(phase_sequence)
            datum['baseline_diagram_cli'] = measure(lambda: cli(baseline / 'check_diagrams.py', ['ontology/diagrams'], root))
            sequence = iter(range(100))
            def cold_diagrams():
                put(root, 'ontology/diagrams/Flow0.drawio', diagram_xml() + f'<!-- {next(sequence)} -->')
                return cli(SCRIPTS / 'verify_artifacts.py', ['diagrams'], root)
            datum['cold_diagram_cli'] = measure(cold_diagrams)
            datum['cached_diagram_cli'] = measure(lambda: cli(SCRIPTS / 'verify_artifacts.py', ['diagrams'], root))
            result['datasets'].append(datum)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    run()
