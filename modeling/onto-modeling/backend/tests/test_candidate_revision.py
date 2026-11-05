"""候选修订：修正不覆盖确认版本，校验结论绑定候选与全部输入（全部为合成数据）。"""
import importlib
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / 'assets/workspace-toolkit/scripts'

OBJECT_PLAN = 'ontology/object/物品.md'
OPERATION_PLAN = 'ontology/operation/客户资料核对.md'


@pytest.fixture
def revision(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module('candidate_revision')


def put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def object_doc(rows: list, table: str = '无', english: str = 'Item', relations: str = '无') -> str:
    return f'''# 对象描述
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
{chr(10).join(rows)}
# 关联对象
{relations}
'''


def operation_doc(logic: str) -> str:
    return f'''# 业务描述
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


PRICE_ROW = '| 单价 | PRICE | 合成单价 | 否 | 整数 | 无 | 无 |'


def item_workspace(root: Path) -> Path:
    """物品绑定 resource/items.csv；单价声明为整数但有小数取值（确认版本带一条类型冲突）。"""
    root = root.resolve()
    put(root, 'resource/items.csv', 'ITEM_ID,PRICE\n1001,12.5\n1002,8\n')
    put(root, 'resource/materials.json', json.dumps({'resource/items.csv': {'purpose': 'schema'}}))
    put(root, OBJECT_PLAN, object_doc(['| 物品编号 | ITEM_ID | 合成标识 | 是 | 字符串 | 无 | 无 |', PRICE_ROW],
                                      table='items'))
    return root


def customer_workspace(root: Path) -> Path:
    root = root.resolve()
    put(root, 'ontology/object/企业客户.md', object_doc([
        '| 客户编号 | customer_id | 合成客户标识 | 是 | 字符串 | 无 | 无 |',
        '| 信用代码 | credit_code | 合成信用代码 | 否 | 字符串 | 无 | 无 |',
    ], english='EnterpriseCustomer'))
    put(root, 'ontology/object/经营画像.md', object_doc([
        '| 画像编号 | profile_id | 合成画像标识 | 是 | 字符串 | 无 | 无 |',
        '| 客户编号 | customer_id | 合成客户标识 | 否 | 字符串 | 无 | 无 |',
    ], english='OperatingProfile', relations='- 当前对象 → 归属（customer_id → customer_id） → [[企业客户]]'))
    put(root, OPERATION_PLAN, operation_doc('按 customer_id 读取 [[企业客户]] 与 [[经营画像]]。'))
    return root


def candidate(root: Path, started: dict, rel: str) -> Path:
    entry = next(item for item in started['files'] if item['path'] == rel)
    return root / entry['candidate']


def fix_price_type(root: Path, started: dict) -> Path:
    path = candidate(root, started, OBJECT_PLAN)
    path.write_text(path.read_text(encoding='utf-8').replace(PRICE_ROW, PRICE_ROW.replace('整数', '小数')),
                    encoding='utf-8')
    return path


def test_correction_goes_through_candidate_diff_check_and_accept(revision, tmp_path):
    root = item_workspace(tmp_path)
    confirmed = (root / OBJECT_PLAN).read_bytes()
    started, code = revision.start(root, [OBJECT_PLAN], note='单价按取值改为小数')
    assert (started['status'], code) == ('open', 0)
    fix_price_type(root, started)
    assert (root / OBJECT_PLAN).read_bytes() == confirmed  # 接受前确认版本不动

    changes, _ = revision.diff(root, started['id'])
    assert [(f['change'], f['added_lines'], f['removed_lines']) for f in changes['files']] == [('modified', 1, 1)]

    checked, code = revision.check(root, started['id'])
    assert (checked['status'], code) == ('passed', 0)
    assert '类型冲突' in {issue['category'] for issue in checked['resolved_errors']}

    accepted, code = revision.accept(root, started['id'])
    assert (accepted['status'], accepted['written'], code) == ('accepted', [OBJECT_PLAN], 0)
    assert '| 小数 |' in (root / OBJECT_PLAN).read_text(encoding='utf-8')
    folder = root / revision.REVISIONS_DIR / started['id']
    assert (folder / 'base' / OBJECT_PLAN).read_bytes() == confirmed  # 确认版本留在修订记录里
    assert json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))['status'] == 'accepted'
    assert revision.accept(root, started['id'])[0]['status'] == 'refused'


def test_accept_refuses_candidate_edited_after_check(revision, tmp_path):
    root = item_workspace(tmp_path)
    started, _ = revision.start(root, [OBJECT_PLAN])
    path = fix_price_type(root, started)
    assert revision.check(root, started['id'])[0]['status'] == 'passed'
    path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    refused, code = revision.accept(root, started['id'])
    assert (refused['status'], code) == ('refused', 1) and '候选在校验后被修改' in refused['reason']


@pytest.mark.parametrize('changed', ['resource/items.csv', 'resource/materials.json'])
def test_accept_refuses_when_source_changes_after_check(revision, tmp_path, changed):
    root = item_workspace(tmp_path)
    confirmed = (root / OBJECT_PLAN).read_bytes()
    started, _ = revision.start(root, [OBJECT_PLAN])
    fix_price_type(root, started)
    assert revision.check(root, started['id'])[0]['status'] == 'passed'
    path = root / changed
    path.write_text(path.read_text(encoding='utf-8') + ('1003,9\n' if changed.endswith('.csv') else ' '),
                    encoding='utf-8')
    refused, code = revision.accept(root, started['id'])
    assert (refused['status'], code) == ('refused', 1) and '资料' in refused['reason']
    assert (root / OBJECT_PLAN).read_bytes() == confirmed


def test_check_is_stale_when_confirmed_version_changes(revision, tmp_path):
    root = item_workspace(tmp_path)
    started, _ = revision.start(root, [OBJECT_PLAN])
    fix_price_type(root, started)
    put(root, OBJECT_PLAN, (root / OBJECT_PLAN).read_text(encoding='utf-8') + '\n')
    checked, code = revision.check(root, started['id'])
    assert (checked['status'], code) == ('stale', 1)
    assert revision.accept(root, started['id'])[0]['status'] == 'refused'


def test_reject_leaves_workspace_untouched(revision, tmp_path):
    root = item_workspace(tmp_path)
    confirmed = (root / OBJECT_PLAN).read_bytes()
    started, _ = revision.start(root, [OBJECT_PLAN])
    fix_price_type(root, started)
    rejected, code = revision.reject(root, started['id'], reason='业务未确认')
    assert (rejected['status'], code) == ('rejected', 0)
    assert (root / OBJECT_PLAN).read_bytes() == confirmed
    assert revision.accept(root, started['id'])[0]['status'] == 'refused'


def test_injected_equivalence_in_candidate_is_rejected_by_check(revision, tmp_path):
    root = customer_workspace(tmp_path)
    started, _ = revision.start(root, [OPERATION_PLAN])
    path = candidate(root, started, OPERATION_PLAN)
    path.write_text(path.read_text(encoding='utf-8') + 'credit_code 与 customer_id 无法交叉核对则报资料冲突\n',
                    encoding='utf-8')
    checked, code = revision.check(root, started['id'])
    assert (checked['status'], code) == ('failed', 1)
    assert [issue['category'] for issue in checked['new_errors']] == ['字段关联']
    assert revision.accept(root, started['id'])[0]['status'] == 'refused'


def test_pending_equivalence_in_candidate_is_only_a_warning(revision, tmp_path):
    root = customer_workspace(tmp_path)
    started, _ = revision.start(root, [OPERATION_PLAN])
    path = candidate(root, started, OPERATION_PLAN)
    path.write_text(path.read_text(encoding='utf-8')
                    + '注意：credit_code 与 customer_id 是否等价待确认，确认前不作为拦截条件\n', encoding='utf-8')
    checked, code = revision.check(root, started['id'])
    assert (checked['status'], code) == ('passed', 0)
    assert [issue['category'] for issue in checked['new_warnings']] == ['字段关联']


def test_unreadable_table_is_unverifiable_and_cannot_be_accepted(revision, tmp_path):
    root = item_workspace(tmp_path)
    started, _ = revision.start(root, [OBJECT_PLAN])
    fix_price_type(root, started)
    (root / 'resource/items.csv').unlink()
    checked, code = revision.check(root, started['id'])
    assert (checked['status'], code) == ('unverifiable', 2) and checked['reason'].startswith('无法核验')
    refused, code = revision.accept(root, started['id'])
    assert (refused['status'], code) == ('refused', 1)


def test_unchanged_candidate_has_nothing_to_accept(revision, tmp_path):
    root = item_workspace(tmp_path)
    started, _ = revision.start(root, [OBJECT_PLAN])
    checked, code = revision.check(root, started['id'])
    assert (checked['status'], code) == ('no_changes', 1)


def test_overlapping_open_revision_is_refused(revision, tmp_path):
    root = item_workspace(tmp_path)
    revision.start(root, [OBJECT_PLAN])
    refused, code = revision.start(root, [OBJECT_PLAN])
    assert (refused['status'], code) == ('refused', 1)


@pytest.mark.parametrize('value', [
    'ontology/object/index.md',
    '../x.md',
    'ontology/object/../operation/a.md',
    'resource/a.md',
    'ontology\\object\\a.md',
])
def test_plan_only_accepts_ontology_documents(revision, tmp_path, value):
    with pytest.raises(revision.RevisionError):
        revision.plan_path(tmp_path.resolve(), value)
