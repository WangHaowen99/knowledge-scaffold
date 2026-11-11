"""Solver contract validation runs. Packages, events, identities and connections are all synthetic."""
import io
import json
import zipfile

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
import yaml

from evaluation import solver_validation
from evaluation.api import create_app
from evaluation.core import Config, digest
from test_evaluation import Dependencies, wait

PACKAGE = 'knowledge_package_demo'
CARD = 'solve_plan_route_0a1b2c3d'
ARGUMENTS = {'plan_month': '2026-01', 'items': [{'item_id': 'item_0001'}, {'item_id': 'item_0002'}]}
EVIDENCE = {'job_id': 'job_demo', 'feasibility': 'feasible', 'result_artifact_ref': 'artifact://result_demo',
            'verification_evidence_ref': 'artifact://verification_demo', 'knowledge_revision_id': 'revision_demo',
            'fact_snapshot_digest': 'a' * 64, 'problem_digest': 'b' * 64,
            'candidate_results_artifact_ref': 'artifact://candidates_demo', 'candidate_results_artifact_digest': 'c' * 64}


def _front(document):
    return '---\n' + yaml.safe_dump(document, allow_unicode=True, sort_keys=False) + '---\n\n'


def solver_package():
    model = {'id': 'route_plan', 'operation_key': 'plan_route', 'entry': 'operations/plan_route.model.yaml',
             'input_schema': 'operations/plan_route.input.schema.json'}
    manifest = dict(id=PACKAGE, name='Synthetic solver package', description='Synthetic data', kind='ontology-package',
                    schema_version='0.2', version='0.1.2', solver_layout='operation-centric.v1',
                    capabilities={'decision_models': [model, {**model, 'id': 'spare_plan', 'operation_key': 'plan_spare'}]})
    files = {
        'manifest.yaml': yaml.safe_dump(manifest, allow_unicode=True),
        'index.md': '# Synthetic package', 'objects/index.md': '# Objects', 'operations/index.md': '# Operations',
        'operations/plan_route.md': _front({'resource': 'operation', 'key': 'plan_route', 'title': '规划路线',
                                            'execution': {'mode': 'solver_workflow', 'decision_model': 'route_plan'}}) + '# 规划路线\n',
        'operations/plan_spare.md': _front({'resource': 'operation', 'key': 'plan_spare', 'title': '备用方案',
                                            'execution': {'mode': 'solver_workflow', 'decision_model': 'spare_plan',
                                                          'enabled': False}}) + '# 备用方案\n',
        'operations/lookup_item.md': _front({'resource': 'operation', 'key': 'lookup_item', 'title': '查询'}) + '# 查询\n',
        'operations/plan_route.model.yaml': 'id: route_plan\nprofile_id: ordered_binary_route.v1\n',
        'operations/plan_route.input.schema.json': json.dumps({'type': 'object', 'additionalProperties': False,
                                                               'properties': {'items': {'type': 'array'}}}),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, value in files.items():
            archive.writestr(f'{PACKAGE}/{name}', value)
    return output.getvalue()


def sse(*events):
    return ''.join(f'data: {json.dumps(event, ensure_ascii=False)}\n\n' for event in events)


def started(arguments=None, call='call_1', name=CARD, **extra):
    preview = ARGUMENTS if arguments is None else arguments
    return {'event_type': 'tool.started', 'payload': {'name': name, 'tool_call_id': call, 'input_preview': preview, **extra}}


def finished(output, call='call_1', status='completed'):
    preview = output if isinstance(output, str) else json.dumps(output)
    return {'event_type': 'tool.finished', 'payload': {'tool_call_id': call, 'status': status, 'output_preview': preview}}


def authoritative(call='call_1', **fields):
    return {'event_type': 'solver.result.authoritative', 'payload': {'tool_call_id': call, 'authoritative': True,
                                                                      **EVIDENCE, **fields}}


def passing_events(call='call_1', arguments=None):
    return [started(arguments, call), authoritative(call),
            finished({'status': 'completed', 'authoritative': True, **EVIDENCE}, call)]


class SolverDependencies(Dependencies):
    def __init__(self):
        super().__init__()
        raw = solver_package()
        self.releases['release_solver'] = (raw, dict(release_id='release_solver', ontology_id=PACKAGE, version='0.1.2',
                                                     kind='release', package_digest=digest(raw), status='committed'))
        self.events = sse(*passing_events())
        self.candidates = 4
        self.imported = {'success': True}
        self.run_bodies = []

    def json(self, method, base, path, **kwargs):
        if path.startswith('/api/solver/history/'):
            assert kwargs.get('workspace')
            return {'candidate_count': self.candidates}
        if path.endswith('/import-zip'):
            return self.imported
        if path == '/agentic/runs':
            self.run_bodies.append(kwargs['json'])
        return super().json(method, base, path, **kwargs)


def _client(tmp_path, judge=True):
    deps = SolverDependencies()
    judge_fields = dict(judge_url='https://judge.example.com', judge_key='synthetic-judge', judge_model='synthetic',
                        judge_revision='1') if judge else {}
    cfg = Config(tmp_path, auth_url='https://auth.example.com', issuer='synthetic', hub_url='https://hub.example.com',
                 hub_token='synthetic-service', consumer_url='https://consumer.example.com',
                 consumer_token='synthetic-evaluation-service', encryption_key=Fernet.generate_key().decode(), **judge_fields)
    return deps, create_app(cfg, deps)


def five_nodes(source, ontology_id):
    """A read-only graph scope that already holds data."""
    return 5


def no_nodes(source, ontology_id):
    return 0


@pytest.fixture
def solver_env(tmp_path):
    deps, app = _client(tmp_path)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer synthetic-user-a'
        app.state.engine.data.graph.existing_nodes = five_nodes
        yield client, app, deps


def graph_source(client, import_data=False):
    connection = {'uri': 'bolt://service.example.net:7687', 'database': 'neo4j', 'username': 'operator_a',
                  'password': 'synthetic-invalid-password', 'workspace_property': 'evaluation_scope',
                  'workspace_value': 'workspace_demo', 'ontology_property': 'knowledge_package_id'}
    response = client.post('/evaluation/api/data-sources', json={'name': 'Synthetic graph', 'mode': 'connection',
                                                                 'provider': 'neo4j', 'connection': connection,
                                                                 'import_data': import_data})
    assert response.status_code == 200, response.text
    return response.json()['id']


def prepare(client, source_id='', release='release_solver', name='prepare_solver'):
    response = client.post('/evaluation/api/package-preparations', json={
        'request_id': name, 'package_ref': {'ontology_id': PACKAGE, 'release_id': release}, 'data_source_id': source_id})
    assert response.status_code == 200, response.text
    return response.json()['id']


def solver_set(client, cases=None):
    cases = cases or [{'id': 'case_1', 'name': '两个项目', 'solver_key': 'plan_route', 'arguments': ARGUMENTS}]
    response = client.post('/evaluation/api/solver-sets', json={'name': 'Synthetic solver set', 'cases': cases})
    assert response.status_code == 200, response.text
    return response.json()['id']


def start(client, preparation_id, set_id, request_id='run_solver', **extra):
    return client.post('/evaluation/api/runs', json={'request_id': request_id, 'preparation_id': preparation_id,
                                                     'run_type': 'solver', 'solver_set_id': set_id, **extra})


def settled_case(client, run_id):
    return client.get(f'/evaluation/api/runs/{run_id}/results/0/answer').json()


# ---- 求解验证集与包内声明 ----


@pytest.mark.parametrize('cases,code', [
    ([{'id': 'a', 'name': 'x', 'solver_key': 'Plan', 'arguments': {}}], 'case_invalid'),
    ([{'id': 'a', 'name': 'x', 'solver_key': 'plan', 'arguments': {}}] * 2, 'case_invalid'),
    ([{'id': 'a', 'name': 'x', 'solver_key': 'plan', 'arguments': []}], 'case_invalid'),
    ([{'id': 'a', 'name': 'x', 'solver_key': 'plan', 'arguments': {'v': 'x' * 70000}}], 'case_invalid'),
    ([{'id': 'a', 'name': 'x', 'solver_key': 'plan', 'arguments': {}, 'expected_candidate_count': -1}], 'case_invalid'),
    ([{'id': 'a', 'name': 'x', 'solver_key': 'plan', 'arguments': {}, 'reference': 'x'}], 'case_invalid'),
    ([{'id': f'c{i}', 'name': 'x', 'solver_key': 'plan', 'arguments': {}} for i in range(21)], 'case_limit'),
])
def test_solver_set_definition_rejects_invalid_cases(solver_env, cases, code):
    client, _, _ = solver_env
    response = client.post('/evaluation/api/solver-sets', json={'name': 'Synthetic', 'cases': cases})
    assert response.status_code == 422 and response.json()['code'] == code


def test_solver_set_keeps_optional_fields(solver_env):
    client, _, _ = solver_env
    set_id = solver_set(client, [{'id': 'a', 'name': ' 名称 ', 'solver_key': 'plan_route', 'arguments': {'k': 1},
                                  'question': '自定义提问', 'expected_candidate_count': 0}])
    stored = client.get(f'/evaluation/api/solver-sets/{set_id}').json()
    assert stored['cases'] == [{'id': 'a', 'name': '名称', 'solver_key': 'plan_route', 'arguments': {'k': 1},
                                'question': '自定义提问', 'expected_candidate_count': 0}]


def test_solver_operations_lists_enabled_solver_operations(solver_env):
    client, _, _ = solver_env
    value = client.get(f'/evaluation/api/packages/{PACKAGE}/releases/release_solver/solver-operations').json()
    assert [item['solver_key'] for item in value['operations']] == ['plan_route']
    operation = value['operations'][0]
    assert operation['title'] == '规划路线' and operation['profile_id'] == solver_validation.ROUTE_PROFILE
    assert operation['input_schema']['type'] == 'object'


def test_cases_are_planned_against_declared_operations():
    declared = solver_validation.operations(solver_package())
    planned = solver_validation.plan_cases([{'id': 'a', 'name': 'x', 'solver_key': 'plan_route', 'arguments': ARGUMENTS}], declared)
    assert planned[0]['expected_candidate_count'] == 4 and planned[0]['question_generated'] is True
    assert planned[0]['id'] == '0' and planned[0]['case_id'] == 'a'
    assert 'operation_key：plan_route' in planned[0]['question'] and '"item_0001"' in planned[0]['question']
    explicit = solver_validation.plan_cases([{'id': 'a', 'name': 'x', 'solver_key': 'plan_route', 'arguments': {},
                                              'question': '自定义', 'expected_candidate_count': 7}], declared)[0]
    assert explicit['expected_candidate_count'] == 7 and explicit['question'] == '自定义'
    with pytest.raises(Exception) as caught:
        solver_validation.plan_cases([{'id': 'a', 'name': 'x', 'solver_key': 'plan_other', 'arguments': {}}], declared)
    assert caught.value.code == 'solver_cases_invalid'
    with pytest.raises(Exception) as caught:
        solver_validation.plan_cases([], declared)
    assert caught.value.code == 'solver_cases_incomplete'


# ---- 取证与断言 ----


def verdict(events, case=None, remote=None, candidates=None):
    case = case or {'solver_key': 'plan_route', 'arguments': ARGUMENTS, 'expected_candidate_count': 4}
    result = solver_validation.case_result(case, solver_validation.observe(sse(*events).encode()), remote or {'status': 'completed'})
    if candidates is not None and result.get('status') == 'completed':
        result['candidate_count'] = candidates
    return result, solver_validation.assess(result, case['expected_candidate_count'])


def test_passing_call_satisfies_every_assertion():
    result, (assertions, code, _) = verdict(passing_events(), candidates=4)
    assert code == '' and all(item['passed'] for item in assertions)
    assert [item['name'] for item in assertions] == ['solver_tool_called', 'arguments_match', 'authoritative',
                                                      'evidence', 'candidate_count']
    assert result['solver_tool'] == CARD and result['job_id'] == 'job_demo'


@pytest.mark.parametrize('events,code', [
    ([], 'solver_tool_not_called'),
    ([started(name='solve_other_0a1b2c3d')], 'solver_tool_not_called'),
    ([started({**ARGUMENTS, 'plan_month': '2026-02'}), authoritative(),
      finished({'status': 'completed', 'authoritative': True, **EVIDENCE})], 'solver_arguments_mismatch'),
    ([started(truncated=True), authoritative(), finished({'status': 'completed', 'authoritative': True, **EVIDENCE})],
     'solver_arguments_unverifiable'),
    ([started(), finished({'status': 'completed', 'authoritative': False, **EVIDENCE})], 'solver_result_not_authoritative'),
    ([started(), finished({'status': 'completed', 'authoritative': True, **EVIDENCE})], 'solver_authoritative_event_missing'),
    ([started(), authoritative(job_id='job_other'), finished({'status': 'completed', 'authoritative': True, **EVIDENCE})],
     'solver_authoritative_event_mismatch'),
    ([started(), authoritative(verification_evidence_ref=''),
      finished({'status': 'completed', 'authoritative': True, **{**EVIDENCE, 'verification_evidence_ref': ''}})],
     'solver_evidence_incomplete'),
    ([started(), finished({'status': 'awaiting_input', 'missing': ['plan_month']})], 'awaiting_input'),
    ([started(), finished({'status': 'failed', 'code': 'fact_snapshot_invalid', 'phase': 'facts'}, status='error')],
     'fact_snapshot_invalid'),
    ([started()], 'solver_timeout'),
], ids=['no_call', 'other_operation', 'arguments_differ', 'arguments_truncated', 'not_authoritative',
        'authoritative_event_missing', 'authoritative_event_mismatch', 'evidence_missing', 'awaiting_input',
        'solver_failed', 'no_result'])
def test_failures_report_the_first_failed_assertion(events, code):
    _, (_, found, reason) = verdict(events, candidates=4)
    assert found == code and reason


def test_candidate_count_mismatch_and_unreadable_candidates():
    assert verdict(passing_events(), candidates=3)[1][1] == 'solver_candidate_count_mismatch'
    result = solver_validation.case_result({'solver_key': 'plan_route', 'arguments': ARGUMENTS},
                                           solver_validation.observe(sse(*passing_events()).encode()), {'status': 'completed'})
    result.update(candidate_count=None, candidate_results_error='candidate_not_found')
    assert solver_validation.assess(result, 4)[1] == 'candidate_not_found'


def test_infeasible_result_needs_no_candidates():
    evidence = {k: v for k, v in EVIDENCE.items() if not k.startswith('candidate_')} | {'feasibility': 'infeasible'}
    events = [started(), {'event_type': 'solver.result.authoritative',
                          'payload': {'tool_call_id': 'call_1', 'authoritative': True, **evidence}},
              finished({'status': 'completed', 'authoritative': True, **evidence})]
    assert verdict(events)[1][1] == ''


def test_truncated_output_uses_event_fields_and_last_call_wins():
    events = [started({'items': []}, 'call_1'), finished({'status': 'failed'}, 'call_1', 'error'),
              started(ARGUMENTS, 'call_2'), {'event_type': 'solver.progress', 'payload': {'tool_call_id': 'call_2', 'status': 'completed', 'authoritative': True}},
              authoritative('call_2'), finished('{"status": "completed", "authoritative": tr…', 'call_2')]
    assert verdict(events, candidates=4)[1][1] == ''


def test_compatible_card_names_the_operation_in_its_input():
    events = [started({'operation_key': 'plan_route', 'arguments': ARGUMENTS}, name='solve'), authoritative(),
              finished({'status': 'completed', 'authoritative': True, **EVIDENCE})]
    assert verdict(events, candidates=4)[1][1] == ''


def test_numeric_strings_match_and_redacted_values_are_skipped():
    assert solver_validation.argument_diff({'a': 1, 'b': [1, 2]}, {'a': '1', 'b': [1, 2]}) == []
    assert solver_validation.argument_diff({'secret': 'x'}, {'secret': '[REDACTED]'}) == []
    assert solver_validation.argument_diff({'a': {'b': 1}}, {'a': {'b': 2}, 'c': None}) == ['a.b']


# ---- 端到端：评测服务里的求解验证轮次 ----


@pytest.mark.parametrize('model_profile_id', ['', 'user:synthetic_personal'], ids=['default_model', 'personal_model'])
def test_solver_run_passes_without_judge(tmp_path, model_profile_id):
    deps, app = _client(tmp_path, judge=False)
    if model_profile_id:
        deps.profiles.append({'id': model_profile_id, 'scope': 'user', 'name': 'Synthetic personal',
                              'model': 'synthetic-personal', 'provider': 'openai', 'enabled': True})
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer synthetic-user-a'
        app.state.engine.data.graph.existing_nodes = five_nodes
        response = start(client, prepare(client, graph_source(client)), solver_set(client), model_profile_id=model_profile_id)
        assert response.status_code == 202, response.text
        record = response.json()
        assert record['run_type'] == 'solver' and record['concurrency'] == 1 and record['judge'] is None
        result = wait(client, record['id'])
        assert result['status'] == 'completed', result
        assert deps.submitted_models == [model_profile_id or 'global:synthetic']
        case = settled_case(client, record['id'])
        assert case['judgement']['verdict'] == 'pass' and case['solver']['candidate_count'] == 4
        assert deps.judge_calls == []
        assert deps.run_bodies[0]['arguments'] == {'selected_ontology_id': PACKAGE, 'selected_ontology_version': '0.1.2'}
        rows = client.get(f"/evaluation/api/runs/{record['id']}/results").json()['items']
        assert 'arguments' not in rows[0] and rows[0]['solver']['assertions']
        report = client.get(f"/evaluation/api/runs/{record['id']}/artifacts/report.md")
        assert report.status_code == 200 and '求解验证报告' in report.text
        guidance = client.get(f"/evaluation/api/runs/{record['id']}/artifacts/suggestions.md").text
        assert '无需修复' in guidance
        view = client.get('/evaluation/api/package-evaluations', params={'ontology_id': PACKAGE}).json()
        assert view['releases'] == []
        assert view['solver_releases'][0]['run_type'] == 'solver'
        assert view['solver_releases'][0]['solver_summary']['failed'] == 0
        # Nothing for modeling to optimize: no handling state is shown, sent or accepted.
        assert result['optimization'] is None and view['solver_releases'][0]['optimization'] is None
        writeback = client.post(f"/evaluation/api/runs/{record['id']}/modeling-result",
                                json={'request_id': 'writeback_demo', 'outcome': 'failed', 'note': 'synthetic'})
        assert writeback.status_code == 409 and writeback.json()['code'] == 'not_applicable'


def test_solver_run_reports_failure_and_guidance(solver_env):
    client, _, deps = solver_env
    deps.events = sse(started({**ARGUMENTS, 'plan_month': '2026-02'}), authoritative(),
                      finished({'status': 'completed', 'authoritative': True, **EVIDENCE}))
    record = start(client, prepare(client, graph_source(client)), solver_set(client)).json()
    assert wait(client, record['id'])['status'] == 'completed'
    case = settled_case(client, record['id'])
    assert case['judgement']['code'] == 'solver_arguments_mismatch'
    assert case['solver']['argument_diff'] == ['plan_month']
    guidance = client.get(f"/evaluation/api/runs/{record['id']}/artifacts/suggestions.md").text
    assert 'solver_arguments_mismatch' in guidance
    meta = client.get(f"/evaluation/api/runs/{record['id']}/artifacts").json()['artifacts']
    assert meta['suggestions'] == 'ready' and meta['suggestions_model'] is None


def test_operation_unusable_in_consumer_fails_without_asking(solver_env):
    client, _, deps = solver_env
    deps.imported = {'success': True, 'warnings': {'operations': ['plan_route (solver_contract_invalid): input schema is not strict']}}
    record = start(client, prepare(client, graph_source(client)), solver_set(client)).json()
    assert wait(client, record['id'])['status'] == 'completed'
    case = settled_case(client, record['id'])
    assert case['judgement']['code'] == 'solver_contract_invalid' and deps.run_bodies == []


def test_plugin_not_registered_fails_every_case():
    found = solver_validation.unavailable({'plugin_status': {'extensions': [
        {'execution_mode': 'solver_workflow', 'registered': False, 'reason_code': 'capability_not_registered',
         'reason': '1 个操作所需的插件能力未启用'}]}})
    assert found['*']['code'] == 'capability_not_registered'


@pytest.mark.parametrize('setup,extra,code', [
    (lambda client: '', {}, 'solver_data_source_unsupported'),
    (lambda client: graph_source(client), {'concurrency': 5}, 'concurrency_invalid'),
], ids=['no_graph_source', 'concurrency_over_limit'])
def test_start_rejects_invalid_solver_runs(solver_env, setup, extra, code):
    client, _, _ = solver_env
    response = start(client, prepare(client, setup(client)), solver_set(client), **extra)
    assert response.status_code == 422 and response.json()['code'] == code


def test_start_requires_declared_operations_and_graph_data(solver_env):
    client, app, _ = solver_env
    source = graph_source(client)
    response = start(client, prepare(client, source, release='release_demo', name='prepare_plain'), solver_set(client))
    assert response.json()['code'] == 'solver_validation_not_required'
    uncovered = solver_set(client, [{'id': 'a', 'name': 'x', 'solver_key': 'plan_other', 'arguments': {}}])
    assert start(client, prepare(client, source), uncovered, request_id='run_unknown').json()['code'] == 'solver_cases_invalid'
    app.state.engine.data.graph.existing_nodes = no_nodes
    response = start(client, prepare(client, source), solver_set(client), request_id='run_empty')
    assert response.status_code == 422 and response.json()['code'] == 'solver_graph_data_empty'


def test_solver_verdicts_cannot_be_overridden(solver_env):
    client, _, _ = solver_env
    record = start(client, prepare(client, graph_source(client)), solver_set(client)).json()
    finished_run = wait(client, record['id'])
    response = client.post(f"/evaluation/api/runs/{record['id']}/results/0/override",
                           json={'verdict': 'pass', 'reason': '人工', 'revision': finished_run['revision']})
    assert response.status_code == 409 and response.json()['code'] == 'override_unsupported'


def test_data_runs_keep_their_defaults(solver_env):
    client, _, _ = solver_env
    cases = client.post('/evaluation/api/eval-sets', json={'name': 'Synthetic set', 'cases': [{'question': 'Q', 'reference': '2'}]}).json()
    response = client.post('/evaluation/api/runs', json={'request_id': 'run_data', 'preparation_id': prepare(client, '', 'release_demo', 'prepare_data'),
                                                         'eval_set_id': cases['id']})
    assert response.status_code == 202 and response.json()['concurrency'] == 4 and response.json()['run_type'] == 'data'
    assert client.post('/evaluation/api/runs', json={'request_id': 'run_data_missing', 'preparation_id': 'x'}).json()['code'] in (
        'not_found', 'eval_set_required')
