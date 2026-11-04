"""属性类型取值证据、标识混合与跨对象字段等价关联的规则检查（全部为合成数据）。

覆盖对象抽取把数值列无依据写成字符串、类型填多个候选、整列为空被当作通过，
以及操作抽取臆造「credit_code 与 customer_id 无法交叉核对则报资料冲突」这类拦截条件。
"""
import importlib
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / 'assets/workspace-toolkit/scripts'


@pytest.fixture
def semantics(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module('field_semantics')


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module('ontology_checker')


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module('verify_artifacts')


@pytest.fixture
def table_profile(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module('table_profile')


OBJECT_DOC = '''# 对象描述
合成对象。
# 对象别名
无
# 英文名称
{english}
# 数据来源
{table}
# 查询边界
只查询已声明字段。
# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
{rows}
# 关联对象
{relations}
'''

OPERATION_DOC = '''# 业务描述
合成操作。
# 与operation的关联
无
# 与object的关联
[[企业客户]]
# 输入
无
# 输出
无
# 逻辑描述
{logic}
'''


def put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def object_doc(rows: list, table: str = 'items', english: str = 'Item', relations: str = '无') -> str:
    return OBJECT_DOC.format(english=english, table=table, rows='\n'.join(rows), relations=relations)


def item_workspace(root: Path, csv_text: str, rows: list) -> None:
    """一个对象「物品」绑定登记为 schema 的 resource/items.csv。"""
    put(root, 'resource/items.csv', csv_text)
    put(root, 'resource/materials.json', json.dumps({'resource/items.csv': {'purpose': 'schema'}}))
    put(root, 'ontology/object/物品.md', object_doc(rows))


def column(semantics, rows: list, name: str) -> dict:
    return semantics.profile_table(rows)['columns'][name]


# ---------------------------------------------------------------------------
# 单元格分类与列画像
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value, kind', [
    (None, 'empty'), ('  ', 'empty'),
    (0, 'integer'), ('0', 'integer'), (12.0, 'integer'), ('12.00', 'integer'),
    ('3.5', 'decimal'), (3.5, 'decimal'), ('1e3', 'decimal'), ('0.5', 'decimal'),
    ('007', 'zero_padded'), ('0123', 'zero_padded'),
    (True, 'boolean'), ('true', 'boolean'), ('否', 'boolean'),
    ('t', 'boolean_short'), ('N', 'boolean_short'),
    ('2024-01-02', 'date'), ('2024/1/2 10:00', 'date'),
    ('ABC-1', 'text'),
])
def test_classify_value(semantics, value, kind):
    assert semantics.classify_value(value)[0] == kind


def test_zero_padded_value_keeps_its_normalized_form_for_collision_checks(semantics):
    assert semantics.classify_value('007') == ('zero_padded', '7')


def test_explicit_zero_counts_as_a_value_and_empty_cells_do_not(semantics):
    rows = [['QTY', 'RATE', 'MEMO'], ['0', '1.5', ''], ['12', '0', ''], [3, 2.25, None]]
    profile = semantics.profile_table(rows)
    assert profile['rows'] == 3 and profile['skipped_rows'] == []
    qty, rate, memo = (profile['columns'][name] for name in ('QTY', 'RATE', 'MEMO'))
    assert (qty['filled'], qty['inferred']) == (3, '整数')
    assert rate['inferred'] == '小数'
    assert (memo['filled'], memo['empty'], memo['inferred']) == (0, 3, '')


def test_numeric_column_declared_as_text_is_pending_without_evidence(semantics):
    rows = [['QTY', 'RATE', 'MEMO'], ['0', '1.5', ''], ['12', '0', ''], [3, 2.25, None]]
    qty, rate, memo = (column(semantics, rows, name) for name in ('QTY', 'RATE', 'MEMO'))
    canonical, verdict, reason = semantics.assess_type('字符串', '', False, qty)
    assert (canonical, verdict) == ('字符串', '待确认') and '整数' in reason
    assert semantics.assess_type('整数', '', False, qty)[1] == '一致'
    assert semantics.assess_type('整数', '', False, rate)[1] == '冲突'
    assert semantics.assess_type('小数', '', False, rate)[1] == '一致'
    assert semantics.assess_type('数字', '', False, rate)[1] == '一致'
    # 整列为空：没有取值证据，既不是通过，也不能据此定为字符串
    assert semantics.assess_type('字符串', '', False, memo)[1] == '未验证'


def test_text_kept_on_purpose_or_identity_is_not_pending(semantics):
    qty = column(semantics, [['QTY'], ['5'], ['0']], 'QTY')
    assert semantics.assess_type('字符串', '文本保留：上游按文本下发', False, qty)[1] == '一致'
    assert semantics.assess_type('字符串', '', True, qty)[1] == '一致'


def test_leading_zeros_conflict_with_integer_and_need_identity_confirmation(semantics):
    code = column(semantics, [['ITEM_CODE'], ['007'], ['123'], ['0123']], 'ITEM_CODE')
    canonical, verdict, reason = semantics.assess_type('整数', '', True, code)
    assert verdict == '冲突' and '前导零' in reason
    assert semantics.assess_type('字符串', '', True, code)[1] == '一致'
    assert semantics.identity_mixing(code, '')
    assert semantics.identity_mixing(code, '混合标识：去掉前导零后比对') == ''


def test_identity_mixing_numbers_and_text(semantics):
    party = column(semantics, [['PARTY_NO'], ['1001'], ['A1002']], 'PARTY_NO')
    assert '数字 1 个与文本 1 个混用' in semantics.identity_mixing(party, '')


def test_long_integers_keep_text_type(semantics):
    ref = column(semantics, [['ACCOUNT_REF'], ['123456789012345678'], ['223456789012345678']], 'ACCOUNT_REF')
    assert ref['long_integers'] == 2
    assert semantics.assess_type('整数', '', False, ref)[1] == '待确认'
    assert semantics.assess_type('字符串', '', False, ref)[1] == '一致'


@pytest.mark.parametrize('values, rule, verdict', [
    (['t', 'f', 't'], '', '待确认'),
    (['t', 'f', 't'], '取值：t=是 / f=否', '一致'),
    (['true', 'false'], '', '一致'),
    (['是', '否'], '', '一致'),
    (['0', '1', '1'], '', '待确认'),
    (['0', '1', '1'], '取值：1=是 / 0=否', '一致'),
    (['2', '1'], '', '冲突'),
    (['true', 'maybe'], '', '冲突'),
])
def test_boolean_values(semantics, values, rule, verdict):
    flag = column(semantics, [['FLAG'], *([v] for v in values)], 'FLAG')
    assert semantics.assess_type('布尔', rule, False, flag)[1] == verdict


@pytest.mark.parametrize('values, rule, verdict', [
    (['2024-01-02', '20240103'], '', '待确认'),
    (['2024-01-02', '20240103'], '格式：yyyymmdd', '一致'),
    (['2024-01-02', 'soon'], '', '冲突'),
])
def test_date_values(semantics, values, rule, verdict):
    day = column(semantics, [['DAY'], *([v] for v in values)], 'DAY')
    assert semantics.assess_type('日期', rule, False, day)[1] == verdict


@pytest.mark.parametrize('cell, expected', [
    ('字符串/整型/布尔', ('', 'multiple')),
    ('{待填写：只填一个类型}', ('', 'empty')),
    ('', ('', 'empty')),
    ('整型', ('整数', '')),
    ('varchar(32)', ('字符串', '')),
    ('整数/int', ('整数', '')),
    ('金额', ('', 'unknown')),
])
def test_parse_declared_type(semantics, cell, expected):
    assert semantics.parse_declared_type(cell) == expected


def test_illegal_or_unknown_type_is_never_accepted(semantics):
    qty = column(semantics, [['QTY'], ['5']], 'QTY')
    assert semantics.assess_type('字符串/整型/布尔', '', False, qty)[1] == '非法类型'
    assert semantics.assess_type('{待填写：只填一个类型}', '', False, qty)[1] == '非法类型'
    assert semantics.assess_type('', '', False, qty)[1] == '非法类型'
    assert semantics.assess_type('金额', '', False, qty)[1] == '待确认'
    assert semantics.assess_type('整数', '', False, None) == ('整数', '未验证', '')


@pytest.mark.parametrize('row, identity', [
    ({'主键': '是', '英文名称': 'X'}, True),
    ({'英文名称': 'credit_code'}, True),
    ({'英文名称': 'customerId'}, True),
    ({'中文名称': '订单编号', '英文名称': 'ORDER_REF'}, True),
    ({'中文名称': '数量', '英文名称': 'QTY'}, False),
])
def test_identity_field(semantics, row, identity):
    assert semantics.is_identity_field(row) is identity


def test_type_row_after_header_is_type_evidence_not_a_note(semantics):
    rows = [['ORDER_NO', 'AMOUNT', 'REMARK'], ['字符串', '小数', '字符串'],
            ['1001', '12.5', 'ab-1'], ['1002', '8', 'cd-2']]
    profile = semantics.profile_table(rows)
    assert profile['skipped_rows'] == ['type'] and profile['rows'] == 2
    order = profile['columns']['ORDER_NO']
    assert order['source_type'] == '字符串'
    assert semantics.assess_type('整数', '', False, order)[1] == '待确认'
    assert semantics.assess_type('字符串', '', False, order)[1] == '一致'
    assert semantics.assess_type('小数', '', False, profile['columns']['AMOUNT'])[1] == '一致'


def test_note_row_after_header_is_skipped(semantics):
    rows = [['ORDER_NO', 'AMOUNT', 'REMARK'], ['订单编号（唯一）', '金额：元', '备注说明'],
            ['1001', '12.5', 'ab-1'], ['1002', '8', 'cd-2']]
    profile = semantics.profile_table(rows)
    assert profile['skipped_rows'] == ['note'] and profile['rows'] == 2
    assert profile['columns']['ORDER_NO']['kinds'] == {'integer': 2}


def test_profile_keeps_no_cell_values(semantics):
    profile = semantics.profile_table([['ITEM_ID', 'NOTE'], ['1001', 'SYNTH-VALUE-7731'], ['1002', 'x']])
    assert 'SYNTH-VALUE-7731' not in json.dumps(profile, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 规则检查、逐字段验收报告与核验回执
# ---------------------------------------------------------------------------

ITEMS_CSV = 'ITEM_ID,QTY,PRICE,STATUS\n1001,5,12.5,open\n1002,0,8,closed\n'
ITEM_ROWS = [
    '| 物品编号 | ITEM_ID | 合成标识 | 是 | 字符串 | 无 | 无 |',
    '| 数量 | QTY | 合成数量 | 否 | 字符串 | 无 | 无 |',
    '| 单价 | PRICE | 合成单价 | 否 | 整数 | 无 | 无 |',
    '| 状态 | STATUS | 合成状态 | 否 | 字符串/整型/布尔 | 无 | 无 |',
]
TYPE_CATEGORIES = ('类型非法', '类型冲突', '类型待确认', '标识混合')


def type_issues(checker, root: Path) -> dict:
    issues = checker.run_checks(root / 'ontology')
    return {(i.category, i.severity): i.message for i in issues if i.category in TYPE_CATEGORIES}


def test_checker_reports_type_evidence_per_field(checker, tmp_path):
    item_workspace(tmp_path, ITEMS_CSV, ITEM_ROWS)
    found = type_issues(checker, tmp_path)
    assert set(found) == {('类型待确认', '警告'), ('类型冲突', '错误'), ('类型非法', '错误')}
    assert 'QTY' in found[('类型待确认', '警告')] and '表「items」列 QTY' in found[('类型待确认', '警告')]
    assert 'PRICE' in found[('类型冲突', '错误')]
    assert 'STATUS' in found[('类型非法', '错误')]
    assert not any('ITEM_ID' in message for message in found.values())


def test_checker_reports_identity_mixing(checker, tmp_path):
    item_workspace(tmp_path, 'PARTY_NO\n1001\nA1002\n', ['| 参与方编号 | PARTY_NO | 合成标识 | 是 | 字符串 | 无 | 无 |'])
    found = type_issues(checker, tmp_path)
    assert set(found) == {('标识混合', '警告')}
    put(tmp_path, 'ontology/object/物品.md', object_doc(
        ['| 参与方编号 | PARTY_NO | 合成标识 | 是 | 字符串 | 无 | 混合标识：按原样比对 |']))
    assert type_issues(checker, tmp_path) == {}


def test_types_report_separates_type_acceptance_coverage_and_import(semantics, tmp_path):
    item_workspace(tmp_path, ITEMS_CSV, ITEM_ROWS)
    report, code = semantics.types_report(tmp_path)
    assert (report['status'], code) == ('failed', 1)
    assert report['import_validation']['status'] == 'not_run'
    assert {k: report['counts'][k] for k in ('一致', '待确认', '冲突', '非法类型')} == {
        '一致': 1, '待确认': 1, '冲突': 1, '非法类型': 1}
    by_field = {item['field']: item for item in report['items']}
    assert (by_field['QTY']['declared'], by_field['QTY']['inferred']) == ('字符串', '整数')
    # 类型写错的属性仍映射到列，字段覆盖不因类型结论而变
    assert by_field['STATUS']['column'] == 'STATUS'
    coverage = report['field_coverage']['tables']
    assert [(t['table'], t['columns'], t['mapped']) for t in coverage] == [('items', 4, 4)]
    assert report['field_coverage']['attributes_without_column'] == []


def test_types_report_all_empty_column_is_unverified_not_passed(semantics, tmp_path):
    item_workspace(tmp_path, 'ITEM_ID,MEMO\n1001,\n1002,\n', [
        '| 物品编号 | ITEM_ID | 合成标识 | 是 | 字符串 | 无 | 无 |',
        '| 备注 | MEMO | 合成备注 | 否 | 字符串 | 无 | 无 |',
    ])
    report, code = semantics.types_report(tmp_path)
    assert (report['status'], code) == ('unverified', 0)
    memo = next(item for item in report['items'] if item['field'] == 'MEMO')
    assert memo['verdict'] == '未验证' and '整列为空' in memo['reason']


def test_types_report_unbound_object_is_unverified_with_reason(semantics, tmp_path):
    put(tmp_path, 'ontology/object/物品.md', object_doc(['| 数量 | QTY | 合成数量 | 否 | 整数 | 无 | 无 |'], table='无'))
    report, code = semantics.types_report(tmp_path)
    assert (report['status'], code) == ('unverified', 0)
    assert '未绑定' in report['items'][0]['reason']
    assert report['field_coverage']['objects_without_table'] == ['物品']


def test_types_report_is_unverifiable_when_registered_table_is_missing(semantics, tmp_path):
    item_workspace(tmp_path, ITEMS_CSV, ITEM_ROWS)
    (tmp_path / 'resource/items.csv').unlink()
    report, code = semantics.types_report(tmp_path)
    assert (report['status'], code) == ('unverifiable', 2)
    assert '不存在' in report['reason'] and report['import_validation']['status'] == 'not_run'


def test_types_report_named_object_must_exist(semantics, tmp_path):
    item_workspace(tmp_path, ITEMS_CSV, ITEM_ROWS)
    report, code = semantics.types_report(tmp_path, ['不存在的对象'])
    assert (report['status'], code) == ('unverifiable', 2)


def test_profile_cli_outputs_counts_without_cell_values(table_profile, tmp_path, monkeypatch, capsys):
    item_workspace(tmp_path, 'ITEM_ID,NOTE\n1001,SYNTH-VALUE-7731\n1002,x\n', [])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['table_profile.py', '--profile'])
    assert table_profile.main() == 0
    out = capsys.readouterr().out
    assert 'SYNTH-VALUE-7731' not in out
    table = json.loads(out)['tables']['items']
    assert table['rows'] == 2 and table['columns']['ITEM_ID']['inferred'] == '整数'
    assert json.loads(out)['status'] == 'ok' and json.loads(out)['missing_text'] == []
    monkeypatch.setattr('sys.argv', ['table_profile.py', '--headers'])
    assert table_profile.main() == 0
    assert json.loads(capsys.readouterr().out)['tables'] == {'items': 2}
    saved = json.loads((tmp_path / '.ontology-state/table-headers.json').read_text(encoding='utf-8'))
    assert saved['tables'] == {'items': ['ITEM_ID', 'NOTE']}


def test_verification_fails_on_type_conflict(verifier, tmp_path):
    item_workspace(tmp_path, ITEMS_CSV, ITEM_ROWS)
    result = verifier.verify(tmp_path, 'objects', ['物品'])
    assert result['status'] == 'failed'
    assert {'类型冲突', '类型非法'} <= {i['category'] for i in result['issues'] if i['severity'] == '错误'}


def test_receipt_is_invalidated_when_table_changes(verifier, tmp_path):
    item_workspace(tmp_path, 'ITEM_ID,QTY\n1001,5\n1002,0\n', [
        '| 物品编号 | ITEM_ID | 合成标识 | 是 | 字符串 | 无 | 无 |',
        '| 数量 | QTY | 合成数量 | 否 | 整数 | 无 | 无 |',
    ])
    assert verifier.verify(tmp_path, 'objects', ['物品'])['status'] == 'passed'
    cached = verifier.verify(tmp_path, 'objects', ['物品'], status_only=True)
    assert (cached['status'], cached['cached']) == ('passed', True)
    put(tmp_path, 'resource/items.csv', 'ITEM_ID,QTY\n1001,5\n1002,2.5\n')
    stale = verifier.verify(tmp_path, 'objects', ['物品'], status_only=True)
    assert (stale['status'], stale['reason']) == ('pending', 'no_current_receipt')
    assert verifier.verify(tmp_path, 'objects', ['物品'])['status'] == 'failed'


# ---------------------------------------------------------------------------
# 跨对象字段等价关联
# ---------------------------------------------------------------------------

CUSTOMER_OBJECTS = {
    '企业客户': ('EnterpriseCustomer', [
        '| 客户编号 | customer_id | 合成客户标识 | 是 | 字符串 | 无 | 无 |',
        '| 信用代码 | credit_code | 合成信用代码 | 否 | 字符串 | 无 | 无 |',
    ], '无'),
    '经营画像': ('OperatingProfile', [
        '| 画像编号 | profile_id | 合成画像标识 | 是 | 字符串 | 无 | 无 |',
        '| 客户编号 | customer_id | 合成客户标识 | 否 | 字符串 | 无 | 无 |',
    ], '- 当前对象 → 归属（customer_id → customer_id） → [[企业客户]]'),
    '线索事件': ('LeadEvent', [
        '| 事件编号 | event_id | 合成事件标识 | 是 | 字符串 | 无 | 无 |',
        '| 客户编号 | customer_id | 合成客户标识 | 否 | 字符串 | 无 | 无 |',
    ], '无'),
    '需求': ('Demand', [
        '| 需求编号 | demand_id | 合成需求标识 | 是 | 字符串 | 无 | 无 |',
        '| 来源事件编号 | source_event_id | 合成来源事件 | 否 | 字符串 | 无 | 无 |',
    ], '- 当前对象 → 来源于（source_event_id → event_id） → [[线索事件]]'),
}
ISSUE_LINE = '企业客户的 credit_code 与经营画像或线索事件的 customer_id 无法交叉核对则报资料冲突'


def customer_workspace(root: Path, logic: str) -> None:
    for stem, (english, rows, relations) in CUSTOMER_OBJECTS.items():
        put(root, f'ontology/object/{stem}.md', object_doc(rows, table='无', english=english, relations=relations))
    put(root, 'ontology/operation/客户资料核对.md', OPERATION_DOC.format(logic=logic))


def equivalence_issues(checker, root: Path) -> list:
    return [i for i in checker.run_checks(root / 'ontology') if i.category == '字段关联']


@pytest.mark.parametrize('logic', [
    ISSUE_LINE,
    'credit_code 与 customer_id 无法交叉核对则报资料冲突',
    'if [[企业客户]]的credit_code 与 [[经营画像]]的customer_id 无法交叉核对 then 报资料冲突并终止流程',
    'where a.credit_code = b.customer_id',
])
def test_unproven_equivalence_is_an_error(checker, tmp_path, logic):
    customer_workspace(tmp_path, logic)
    issues = equivalence_issues(checker, tmp_path)
    assert [i.severity for i in issues] == ['错误']
    assert 'credit_code' in issues[0].message and 'customer_id' in issues[0].message


def test_pending_equivalence_is_a_warning(checker, tmp_path):
    customer_workspace(tmp_path, 'credit_code 与 customer_id 是否等价待确认，确认前不作为拦截条件')
    assert [i.severity for i in equivalence_issues(checker, tmp_path)] == ['警告']


@pytest.mark.parametrize('logic', [
    '按 credit_code 和 customer_id 匹配 [[企业客户]]',
    'if [[需求]]的source_event_id == [[线索事件]]的event_id then 关联需求与事件',
    '[[经营画像]]的customer_id 与 [[企业客户]]的customer_id 一致时合并画像',
    '按 customer_id 读取 [[企业客户]] 与 [[经营画像]]。',
])
def test_declared_or_same_name_joins_are_kept(checker, tmp_path, logic):
    customer_workspace(tmp_path, logic)
    assert equivalence_issues(checker, tmp_path) == []


def test_issue_scenario_flags_only_the_invented_check(checker, tmp_path):
    customer_workspace(tmp_path, '\n'.join([
        '1. 按 customer_id 读取 [[企业客户]] 与 [[经营画像]]。',
        '2. [[需求]]的source_event_id 与 [[线索事件]]的event_id 一致时关联需求与事件。',
        f'3. {ISSUE_LINE}。',
    ]))
    issues = equivalence_issues(checker, tmp_path)
    assert len(issues) == 1 and issues[0].severity == '错误'
    assert '企业客户.credit_code' in issues[0].message and '第' in issues[0].message


def test_joins_report_lists_declared_pairs_and_identity_fields(semantics, tmp_path):
    customer_workspace(tmp_path, '读取已声明属性。')
    report = semantics.joins_report(tmp_path)
    pairs = {(j['source'], j['source_field'], j['target'], j['target_field']) for j in report['joins']}
    assert pairs == {('经营画像', 'customer_id', '企业客户', 'customer_id'),
                     ('需求', 'source_event_id', '线索事件', 'event_id')}
    assert report['identity_fields']['企业客户'] == ['customer_id', 'credit_code']
    narrowed = semantics.joins_report(tmp_path, ['需求'])
    assert [j['target'] for j in narrowed['joins']] == ['线索事件']
