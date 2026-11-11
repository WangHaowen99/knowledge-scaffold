"""Batched optimization suggestions against a synthetic judge gateway. All identities and data are synthetic."""
import io
import json
import zipfile

import httpx
import pytest
import yaml

from evaluation import core, suggestions
from evaluation.core import Failure, Transport
from evaluation.suggestions import MERGE_SYSTEM, SYSTEM, Suggestions

STEM = 'eval-20261102-101010-aaaaaa'
DOCS = {'objects/index.md': '# 对象索引', 'objects/order.md': '# 订单\n\n订单金额与订单状态的口径。\n',
        'objects/stock.md': '# 库存\n\n库存数量按仓库汇总。\n'}


def package(docs):
    files = {'manifest.yaml': yaml.safe_dump(dict(id='knowledge_package_demo', name='Synthetic package', description='Synthetic data',
                                                 kind='ontology-package', schema_version='0.1', version='0.1.1')), **docs}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, text in files.items():
            archive.writestr('knowledge_package_demo/' + name, text)
    return output.getvalue()


class Settings:
    @staticmethod
    def effective(owner):
        return {'url': 'https://judge.example.com', 'key': 'synthetic-judge'}


class Judge:
    settings = Settings()

    def profile(self, current):
        return {'model': 'synthetic-model'}


class Packages:
    def __init__(self, raw):
        self.raw = raw

    def fetch(self, ref, token):
        return self.raw, {}


class Clock:
    """Synthetic monotonic clock the gateway advances, so time limits are tested without patching shared time."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Gateway:
    """Synthetic judge gateway standing in for a small or slow model.

    It rejects requests above ``capacity`` characters, as a small model would, and does not answer requests
    above ``patience`` characters in time, as a slow reasoning model would.
    """

    def __init__(self, capacity=10 ** 9, fail=(), merge_drops=False, patience=10 ** 9, clock=None, cost=0):
        self.capacity, self.fail, self.merge_drops, self.requests = capacity, list(fail), merge_drops, []
        self.patience, self.clock, self.cost, self.timeouts = patience, clock or Clock(), cost, []

    def json(self, method, base, path, **kwargs):
        system, content = (message['content'] for message in kwargs['json']['messages'])
        self.requests.append((system, len(content), json.loads(content)))
        self.timeouts.append(kwargs['timeout'])
        self.clock.now += self.cost
        error = self.fail.pop(0) if self.fail else None
        if error:
            raise error
        if len(content) > self.patience:
            self.clock.now += kwargs['timeout']
            raise Failure('dependency_timeout', '依赖服务响应超时，请稍后重试', 504, 'dependency')
        if len(content) > self.capacity:
            raise Failure('dependency_rejected', '依赖服务拒绝请求', 503, 'dependency', upstream_status=400, context_overflow=True)
        payload = json.loads(content)
        if system == MERGE_SYSTEM:
            numbers = sorted({n for item in payload['items'] for n in item['cases']})
            value = {'summary': 'Synthetic merged summary', 'items': [{
                'cases': numbers[:1] if self.merge_drops else numbers, 'category': 'C',
                'evidence': 'Synthetic merged evidence', 'suggestion': 'Synthetic merged revision', 'targets': ['objects/order.md']}]}
        else:
            value = {'summary': 'Synthetic batch summary', 'items': [{
                'cases': [case['number']], 'category': 'C', 'evidence': f"Synthetic evidence {case['number']}",
                'suggestion': 'Synthetic revision', 'targets': ['objects/order.md']} for case in payload['cases']]}
        return {'choices': [{'message': {'tool_calls': [{'function': {'name': 'submit_suggestions', 'arguments': json.dumps(value)}}]}}]}

    def batches(self):
        return [[case['number'] for case in payload['cases']] for system, _, payload in self.requests if system == SYSTEM]


def run_of(count, answer_chars=600):
    """Failed cases alternating between a stock topic (odd numbers) and an order topic (even numbers)."""
    cases = []
    for i in range(count):
        topic = '订单金额' if i % 2 else '库存数量'
        cases.append({'question': f'合成问题 {i} {topic}', 'reference': '合成参考', 'answer': '合' * answer_chars,
                      'status': 'settled', 'judgement': {'verdict': 'fail', 'reason': f'合成理由 {topic}'}})
    return {'id': 'a' * 32, 'status': 'completed', 'base_ref': {'version': '0.1.1'}, 'package_ref': {'version': '0.1.1'}, 'cases': cases}


def generate(gateway, run, request_chars=suggestions.REQUEST_CHARS, evidence=None, docs=None, progress=None):
    service = Suggestions(Judge(), Packages(package(DOCS if docs is None else docs)), gateway, request_chars, gateway.clock)
    text, profile, _ = service.generate('owner_a', run, 'synthetic-user-a', STEM, 1793500000, {}, evidence, progress)
    assert profile['model'] == 'synthetic-model'
    return text


def test_cases_that_fit_go_in_one_request_with_the_run_overview():
    gateway = Gateway()
    text = generate(gateway, run_of(3))
    assert gateway.batches() == [[1, 3, 2]] and len(gateway.requests) == 1
    overview = gateway.requests[0][2]['overview']
    assert overview['total'] == 3 and overview['fail'] == 3
    assert '> 覆盖用例：3 条失分或异常用例' in text and '分批' not in text and '未纳入' not in text


def test_cases_of_one_document_share_a_batch_and_batches_are_merged():
    gateway = Gateway()
    text = generate(gateway, run_of(12), request_chars=8000)
    batches = gateway.batches()
    assert len(batches) > 1 and sorted(n for batch in batches for n in batch) == list(range(1, 13))
    # Odd numbers concern stock, even numbers orders: the first batch holds stock cases only.
    assert batches[0] == [1, 3, 5, 7]
    assert all(length <= 8000 for system, length, _ in gateway.requests if system == SYSTEM)
    assert [system for system, *_ in gateway.requests].count(MERGE_SYSTEM) == 1
    assert f'分 {len(batches)} 批分析后合并' in text
    assert '| 1 | ' + '、'.join(map(str, range(1, 13))) + ' | C 数据映射 | Synthetic merged evidence |' in text


def test_a_request_too_long_for_the_model_is_planned_again_at_half_size():
    gateway = Gateway(capacity=7000)
    text = generate(gateway, run_of(12))
    overflows = [length for system, length, _ in gateway.requests if system == SYSTEM and length > 7000]
    # One rejection teaches the smaller size; every later batch is planned under it.
    assert len(overflows) == 1
    assert sorted(n for batch in gateway.batches()[1:] for n in batch) == list(range(1, 13))
    assert '> 覆盖用例：12 条失分或异常用例，分' in text and '未纳入' not in text


def test_a_merge_that_loses_cases_falls_back_to_the_batches():
    gateway = Gateway(merge_drops=True)
    text = generate(gateway, run_of(12), request_chars=8000)
    assert 'Synthetic merged evidence' not in text
    assert all(f'Synthetic evidence {n} |' in text for n in range(1, 13))


def test_cases_beyond_the_batch_limit_and_of_failed_batches_are_listed(monkeypatch):
    monkeypatch.setattr(suggestions, 'MAX_BATCHES', 2)
    rejected = Failure('dependency_rejected', '依赖服务拒绝请求', 422, 'dependency', upstream_status=422)
    gateway = Gateway(fail=[None, rejected])
    text = generate(gateway, run_of(12), request_chars=8000)
    first, second = gateway.batches()[:2]
    skipped = sorted(set(range(1, 13)) - set(first) - set(second))
    assert skipped and f"> 未纳入：用例 {'、'.join(map(str, skipped))} 超出单次生成上限" in text
    assert f"> 未纳入：用例 {'、'.join(map(str, sorted(second)))} 所在批次生成失败" in text
    assert f'> 覆盖用例：{len(first)} 条失分或异常用例' in text


def test_an_unavailable_gateway_is_retried_once():
    gateway = Gateway(fail=[Failure('dependency_unavailable', '依赖服务不可用', 503, 'dependency')])
    text = generate(gateway, run_of(2))
    assert gateway.batches() == [[1, 2], [1, 2]] and '未纳入' not in text


def test_a_model_too_small_for_one_case_fails_with_a_clear_reason():
    with pytest.raises(Failure) as raised:
        generate(Gateway(capacity=100), run_of(1))
    assert raised.value.code == 'suggestions_context' and '上下文更大的判分模型' in raised.value.message


def test_transport_classifies_rejections_without_relaying_the_body(monkeypatch):
    bodies = {'/long': (400, b'{"error":{"message":"This model\'s maximum context length is 32768 tokens"}}'),
              '/other': (400, b'{"error":{"message":"invalid tool schema"}}'), '/large': (413, b''),
              '/conflict': (409, '{"error":"知识包名称「合成包」已存在","error_code":"ontology_name_conflict"}'.encode())}
    client = httpx.Client

    def respond(request):
        status, content = bodies.get(request.url.path)
        return httpx.Response(status, content=content)

    handler = httpx.MockTransport(respond)
    monkeypatch.setattr(core.httpx, 'Client', lambda **kwargs: client(transport=handler, **kwargs))
    flags, codes = {}, {}
    for path in bodies:
        with pytest.raises(Failure) as raised:
            Transport().request('POST', 'https://judge.example.com', path)
        public = json.dumps(raised.value.payload(), ensure_ascii=False)
        assert raised.value.message == '依赖服务拒绝请求' and 'context' not in public and '合成包' not in public
        flags[path], codes[path] = raised.value.context_overflow, raised.value.upstream_code
    assert flags == {'/long': True, '/other': False, '/large': True, '/conflict': False}
    assert codes == {'/long': '', '/other': '', '/large': '', '/conflict': 'ontology_name_conflict'}


def test_documents_follow_tool_calls_and_long_documents_keep_matching_sections():
    filler = ''.join(f'## 合成章节 {i}\n\n' + '无关说明。' * 200 + '\n\n' for i in range(8))
    docs = {'objects/index.md': '# 对象索引\n\n' + '对象清单。' * 1500, 'operations/index.md': '# 操作索引\n\n' + '操作清单。' * 1500,
            'objects/order.md': '# 订单\n\n订单对象定义。\n\n' + filler + '## 退款口径\n\n退款金额按原路退回计算。\n',
            'objects/refund.md': '# 退款\n\n退款对象。\n', 'operations/refund_calc.md': '# 退款计算\n\n计算脚本说明。\n',
            'objects/stock.md': '# 库存\n\n' + '库存说明。' * 2000,
            'mappings/object-data-mappings.json': json.dumps({'schema_version': '0.1', 'objects': {
                'refund': {'resource': 'refund_table', 'sources': [{'resource': 'refund_table'}]},
                **{f'other_{i}': {'resource': f'other_table_{i}', 'note': 'x' * 80} for i in range(200)}}})}
    run = {'id': 'a' * 32, 'status': 'completed', 'base_ref': {'version': '0.1.1'}, 'package_ref': {'version': '0.1.1'}, 'cases': [{
        'question': '退款金额怎么计算', 'reference': '按原路退回', 'answer': '无法确定', 'status': 'settled',
        'judgement': {'verdict': 'fail', 'reason': '缺少退款金额口径'}}]}
    evidence = {0: {'calls': [
        {'n': 1, 'tool': 'execute_script', 'status': 'failed', 'input': '{"script": "refund_calc"}', 'error': 'Synthetic failure'},
        {'n': 2, 'tool': 'business_data_search', 'status': 'completed', 'input': '{"table": "refund_table"}'}], 'omitted': 0}}
    gateway = Gateway()
    generate(gateway, run, evidence=evidence, docs=docs)
    payload = gateway.requests[0][2]
    sent = {doc['path']: doc['content'] for doc in payload['documents']}
    paths = list(sent)
    # Index documents stay within their share of the document budget.
    budget = int(suggestions.REQUEST_CHARS * suggestions.SHARES['documents'])
    assert sum(len(sent[p]) for p in paths if p.endswith('/index.md')) <= budget * suggestions.INDEX_SHARE + 20
    # The script the agent ran and the object behind the table it queried come first.
    others = [p for p in paths if not p.endswith('/index.md')]
    assert set(others[:2]) == {'objects/refund.md', 'operations/refund_calc.md'}
    # A long document keeps its beginning and the section that matches, not its unrelated sections.
    assert sent['objects/order.md'].startswith('# 订单\n\n订单对象定义。') and '## 退款口径\n\n退款金额按原路退回计算。' in sent['objects/order.md']
    assert '合成章节' not in sent['objects/order.md']
    # Unrelated documents still fill what room remains, after the related ones.
    assert others.index('objects/stock.md') > others.index('objects/order.md')
    # The mapping is cut to the objects of the documents sent.
    mapping = json.loads(payload['data_mapping'])
    assert list(mapping['objects']) == ['refund'] and mapping['omitted_objects'] == 200


def test_answers_count_when_relating_a_case_to_a_document():
    found = suggestions.Package(package(DOCS))
    wanted = suggestions.case_terms({'question': '合成问题', 'reference': '合成参考', 'answer': '库存数量为零'}, '合成理由', '')
    assert found.anchor(wanted, set()) == 'objects/stock.md'
    assert found.anchor(wanted, {'objects/order.md'}) == 'objects/order.md'


def test_a_request_the_model_does_not_finish_in_time_is_split_not_resent():
    gateway = Gateway(patience=7000)
    text = generate(gateway, run_of(12))
    lengths = [length for system, length, _ in gateway.requests if system == SYSTEM]
    # One slow request, then smaller ones: the same request is never sent twice.
    assert sum(1 for length in lengths if length > 7000) == 1 and lengths[1] < lengths[0]
    assert gateway.timeouts[0] == suggestions.TIMEOUT
    assert sorted(n for batch in gateway.batches()[1:] for n in batch) == list(range(1, 13)) and '未纳入' not in text


def test_a_model_that_never_answers_in_time_fails_with_its_reason():
    with pytest.raises(Failure) as raised:
        generate(Gateway(patience=0), run_of(1))
    assert raised.value.code == 'suggestions_timeout' and raised.value.message == suggestions.SLOW_MODEL
    assert '思考时间过长' in raised.value.message and '换用响应更快的模型' in raised.value.message


def test_the_whole_generation_stops_at_its_time_limit():
    gateway = Gateway(cost=700)
    text = generate(gateway, run_of(12), request_chars=6000)
    analysed = [n for batch in gateway.batches() for n in batch]
    # 30 minutes in total: each request waits at most what is left, and none starts with under a minute left.
    assert gateway.timeouts == [600, 600, 400]
    late = sorted(set(range(1, 13)) - set(analysed))
    assert late and f"> 未纳入：用例 {'、'.join(map(str, late))} 所在批次判分模型响应超时（思考时间过长）" in text


def test_progress_names_the_batch_being_waited_for_and_the_merge():
    seen = []
    generate(Gateway(), run_of(12), request_chars=8000, progress=seen.append)
    assert seen == [{'batch': 1, 'batches': 3}, {'batch': 2, 'batches': 3}, {'batch': 3, 'batches': 3},
                    {'batch': 3, 'batches': 3, 'merging': True}]


def test_suggestions_markdown_has_no_html_line_breaks():
    # The viewer renders markdown without raw HTML, so <br> would show as text.
    value = {'summary': 'Synthetic summary<br>second line', 'notes': ['Synthetic note<BR/>more'], 'items': [{
        'cases': [1], 'category': 'C', 'evidence': 'Synthetic evidence<br />detail', 'suggestion': 'Synthetic revision<br>step',
        'targets': ['objects/order.md', 'objects/missing.md']}]}
    text = suggestions.render(run_of(1), STEM, 1793500000, value, [1], {'objects/order.md'}, 'synthetic-model')
    assert '<br' not in text.lower()
    assert 'Synthetic summary second line' in text and '- Synthetic note more' in text
    assert ('| 1 | 1 | C 数据映射 | Synthetic evidence detail | Synthetic revision step | '
            '`ontology/object/order.md`、`ontology/object/missing.md`（新建） |') in text
