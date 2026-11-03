"""资料分析报告的内容验收（issue #362）：确定性检查、回执失效与阶段门禁。

覆盖"报告存在即算完成"的三处放行点：research_checker 的检查结论、state_helper
的阶段前置、WorkspaceManager 的项目进度。数据全部合成，不含任何真实表结构。
"""

import importlib
import json
from pathlib import Path
from typing import NamedTuple

import pytest

from app import config
from app.services.workspace import WorkspaceManager

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"

NO_CHANGE_ROW = "| 无 | 无 | 无 | 无 |"
SOURCE = "resource/others/说明.md"


@pytest.fixture
def toolkit(monkeypatch):
    """工具箱脚本按同目录互相 import，在进程内直接调用。"""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return (
        importlib.import_module("research_checker"),
        importlib.import_module("research_review"),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """最小合成工作区：一份 schema 资料（CSV，免 openpyxl）+ 一份带规范清单的业务依据文档。"""
    root = tmp_path / "ws"
    (root / "research").mkdir(parents=True)
    (root / "resource" / "others").mkdir(parents=True)
    (root / "resource" / "others" / "订单表.csv").write_text(
        "ORDER_ID,AMOUNT\n1,2\n", encoding="utf-8"
    )
    # 规范业务项/规则 ID 清单由用户随资料上传（issue #362 现场即如此），ID 溯源以它为准
    (root / "resource" / "others" / "说明.md").write_text(
        "合成业务说明\n\n| 业务项 | 规则 |\n| --- | --- |\n"
        "| ITEM-01 | RULE-01 |\n| ITEM-02 | RULE-02 |\n"
        "| ITEM-03 | RULE-03 |\n| ITEM-04 | RULE-04 |\n| ITEM-05 | RULE-05 |\n",
        encoding="utf-8",
    )
    (root / "resource" / "materials.json").write_text(
        json.dumps(
            {"resource/others/订单表.csv": {"purpose": "schema", "reading_mode": "full"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


# 候选段按分类分家：提取阶段按「分类」列把操作/规则/指标写进各自目录
CANDIDATE_SECTIONS = {
    "对象": "核心业务对象候选",
    "操作": "核心业务逻辑候选",
    "规则": "核心规则候选",
    "指标": "核心指标候选",
}


class ReportRow(NamedTuple):
    """「业务项清单」的一行。"""

    item_id: str
    rule_id: str
    field: str
    location: str
    name: str
    category: str


def make_report(rows, total=None, subtotal=None, changes=NO_CHANGE_ROW, gaps="（无）",
                candidates=True, category_column=True) -> str:
    """按 research-agent.md 的 Step 4 模板拼一份报告。rows 是 ReportRow 列表。"""
    lines = ["# 分析主题", "合成领域。", ""]
    # 下游（coverage-check / extract-operation / rule-metric-extraction）按加粗名称取候选，
    # 候选所在段落必须与清单「分类」一致，否则实体会落错目录
    for category, section in CANDIDATE_SECTIONS.items():
        names = [entry.name for entry in rows if entry.category == category]
        lines.append(f"# {section}")
        if not names:
            lines.append("无")
        elif candidates:
            lines += [f"- **{name}**：合成说明（resource/others/说明.md）" for name in names]
        else:
            lines += [f"{name}：合成说明" for name in names]
        lines.append("")
    lines += ["# 详细信息", f"## {rows[0].name if rows else '合成条目'}", "合成属性说明。", "",
              "# 业务项清单"]
    if category_column:
        lines += ["| 业务项ID | 业务项名称 | 分类 | 规则ID | 引用字段 | 来源位置 |",
                  "| --- | --- | --- | --- | --- | --- |"]
    else:  # 旧契约报告：没有分类列，提取无从分流
        lines += ["| 业务项ID | 业务项名称 | 规则ID | 引用字段 | 来源位置 |",
                  "| --- | --- | --- | --- | --- |"]
    for entry in rows:
        cells = [entry.item_id, entry.name, entry.category,
                 entry.rule_id, entry.field, entry.location]
        if not category_column:
            cells.pop(2)
        lines.append("| " + " | ".join(cells) + " |")

    counted, seen = {}, set()
    for entry in rows:
        if not entry.item_id or entry.item_id in seen:
            continue
        seen.add(entry.item_id)
        counted[entry.category] = counted.get(entry.category, 0) + 1
    count = len(seen)
    lines += ["", "# 分类小计", "| 分类 | 条数 |", "| --- | --- |"]
    if subtotal is None:
        lines += [f"| {category} | {n} |" for category, n in counted.items()]
    else:  # 用例显式给值：制造与清单不符的小计
        lines.append(f"| {next(iter(counted), '对象')} | {subtotal} |")
    lines += ["", "# 合计", str(count if total is None else total),
              "", "# ID变更对照", "| 原ID | 新ID | 变更类型 | 依据 |",
              "| --- | --- | --- | --- |", changes,
              "", "# 数据与术语来源", "- resource/others/订单表.csv（表 订单表）",
              "", "# 信息来源", "**本地文档**", "- resource/others/说明.md：合成内容",
              "", "# 缺口与待决", gaps]
    return "\n".join(lines) + "\n"


def row(item_id, rule_id, field="订单表.ORDER_ID", location=SOURCE, name=None,
        category="对象") -> ReportRow:
    return ReportRow(item_id, rule_id, field, location, name or f"名称{item_id}", category)


GOOD_ROWS = [row("ITEM-01", "RULE-01"), row("ITEM-02", "RULE-02", "订单表.AMOUNT")]
FOUR_ROWS = GOOD_ROWS + [row("ITEM-03", "RULE-03", "订单表.AMOUNT"),
                         row("ITEM-04", "RULE-04", "订单表.AMOUNT")]
# 四类各一条：提取按分类把操作/规则/指标分流到 ontology/operation|rule|metric
MIXED_ROWS = [
    row("ITEM-01", "RULE-01", name="合成订单", category="对象"),
    row("ITEM-02", "无", "订单表.AMOUNT", name="创建合成订单", category="操作"),
    row("ITEM-03", "RULE-03", "订单表.AMOUNT", name="订单金额校验规则", category="规则"),
    row("ITEM-04", "RULE-04", "订单表.AMOUNT", name="订单均价", category="指标"),
]
CANDIDATE_LINE = "- **{}**：合成说明（resource/others/说明.md）"


def _fill_emptied_sections(text: str) -> str:
    """候选项被挪走后段落会空掉，按模板补「无」，只留下要测的那一类错误。"""
    for section in CANDIDATE_SECTIONS.values():
        text = text.replace(f"# {section}\n\n", f"# {section}\n无\n\n")
    return text


def misplace_candidate(text: str, name: str) -> str:
    """把候选名从它自己的段落挪进「核心业务逻辑候选」——规则被当成操作提取的现场形态。"""
    line = CANDIDATE_LINE.format(name)
    moved = _fill_emptied_sections(text.replace(line + "\n", ""))
    return moved.replace("# 核心业务逻辑候选\n", f"# 核心业务逻辑候选\n{line}\n", 1)


def drop_candidate(text: str, name: str) -> str:
    """清单里有、候选段里漏列：下游按候选名提取，漏列就会漏提取。"""
    return _fill_emptied_sections(text.replace(CANDIDATE_LINE.format(name) + "\n", ""))


def write_report(root: Path, text: str, name: str = "报告.md") -> Path:
    path = root / "research" / name
    path.write_text(text, encoding="utf-8")
    return path


def categories(result: dict) -> set:
    return {issue["category"] for issue in result["issues"] if issue["severity"] == "错误"}


# ---------------------------------------------------------------------------
# research_checker：确定性检查项
# ---------------------------------------------------------------------------

def test_correct_report_passes(workspace, toolkit):
    checker, review = toolkit
    write_report(workspace, make_report(GOOD_ROWS))

    result = checker.run_check(workspace)

    assert result["status"] == review.PASSED, result["issues"]
    assert checker.check_exit_code(result) == 0
    assert review.review_status(workspace)["status"] == review.PASSED


@pytest.mark.parametrize("report,expected", [
    # 声称 3 项、实列 4 项——issue #362 的现场现象
    pytest.param(make_report(FOUR_ROWS, total=3, subtotal=3), {"计数"}, id="合计与清单不符"),
    pytest.param(make_report(GOOD_ROWS, subtotal=5), {"计数"}, id="分类小计与合计不符"),
    # 报告把源表字段写成表头中不存在的名称
    pytest.param(make_report([row("ITEM-01", "RULE-01", "订单表.NOT_A_COLUMN")]),
                 {"字段对账"}, id="字段不在真实表头"),
    pytest.param(make_report([row("ITEM-01", "RULE-01"),
                              row("ITEM-01", "RULE-02", "订单表.AMOUNT")], total=1, subtotal=1),
                 {"ID重复"}, id="业务项ID重复"),
    pytest.param(make_report([row("ITEM-01", "RULE-01",
                                  location="resource/others/不存在.md#1")]),
                 {"来源缺失"}, id="来源位置定位不到"),
    pytest.param("# 分析主题\n合成领域。\n\n合计26项。资料分析完成。\n",
                 {"结构缺失"}, id="自由格式无必需段落"),
    # 下游按加粗名称取对象/操作候选，不加粗会静默漏项
    pytest.param(make_report(GOOD_ROWS, candidates=False), {"候选缺失"}, id="候选名未加粗"),
])
def test_deterministic_errors_block_acceptance(workspace, toolkit, report, expected):
    checker, review = toolkit
    write_report(workspace, report)

    result = checker.run_check(workspace)

    assert result["status"] == review.FAILED
    assert categories(result) == expected
    assert review.review_status(workspace)["status"] == review.FAILED


# ---------------------------------------------------------------------------
# research_checker：分类分流（规则/指标不得混进逻辑候选）
# ---------------------------------------------------------------------------

def test_mixed_categories_report_passes(workspace, toolkit):
    """四类齐全、候选段与分类逐项一致的报告应当通过。"""
    checker, review = toolkit
    write_report(workspace, make_report(MIXED_ROWS))

    result = checker.run_check(workspace)

    assert result["status"] == review.PASSED, result["issues"]


@pytest.mark.parametrize("report,expected", [
    # 现场形态：规则被列进「核心业务逻辑候选」，提取照单全收写成 operation 文档。
    # 两条错误同时成立：列错了段落，而规则候选段里又少了它。
    pytest.param(misplace_candidate(make_report(MIXED_ROWS), "订单金额校验规则"),
                 {"分类越界", "候选缺失"}, id="规则列进逻辑候选"),
    pytest.param(misplace_candidate(make_report(MIXED_ROWS), "订单均价"),
                 {"分类越界", "候选缺失"}, id="指标列进逻辑候选"),
    pytest.param(drop_candidate(make_report(MIXED_ROWS), "订单金额校验规则"),
                 {"候选缺失"}, id="清单有规则但候选段漏列"),
    pytest.param(make_report(MIXED_ROWS + [row("ITEM-05", "无", "订单表.AMOUNT",
                                               name="合成流程", category="流程")]),
                 {"分类非法"}, id="分类取值不在枚举内"),
    # 总数对得上但分布错了：规则那一条被算到操作头上
    pytest.param(make_report(MIXED_ROWS).replace("| 规则 | 1 |", "| 操作 | 1 |"),
                 {"计数"}, id="分类小计分布与清单不符"),
    # 旧契约报告没有分类列，提取无从分流
    pytest.param(make_report(MIXED_ROWS, category_column=False),
                 {"结构缺失"}, id="清单缺少分类列"),
    # 加粗只出现在正文句子里，不是 `- **名称**：说明` 列表项，下游提取取不到
    pytest.param(make_report(MIXED_ROWS).replace(CANDIDATE_LINE.format("合成订单"),
                                                 "本领域的核心是 **合成订单**。"),
                 {"候选缺失"}, id="加粗不在列表项开头"),
])
def test_category_routing_errors_block_acceptance(workspace, toolkit, report, expected):
    checker, review = toolkit
    write_report(workspace, report)

    result = checker.run_check(workspace)

    assert result["status"] == review.FAILED
    assert categories(result) == expected


def test_bold_emphasis_in_description_is_not_a_candidate(workspace, toolkit):
    """候选名只取列表项开头的加粗；说明里强调用的加粗词不能被当成候选去对账。"""
    checker, review = toolkit
    write_report(workspace, make_report(MIXED_ROWS).replace(
        CANDIDATE_LINE.format("合成订单"),
        "- **合成订单**：主键为 **订单号**，金额见 **订单表**（resource/others/说明.md）"))

    result = checker.run_check(workspace)

    assert result["status"] == review.PASSED, result["issues"]


def test_misplaced_rule_message_names_the_target_section(workspace, toolkit):
    """报错要指出该挪到哪个段落，否则模型只会原地重试。"""
    checker, _review = toolkit
    write_report(workspace, misplace_candidate(make_report(MIXED_ROWS), "订单金额校验规则"))

    messages = " ".join(i["message"] for i in checker.run_check(workspace)["issues"])

    assert "核心规则候选" in messages
    assert "订单金额校验规则" in messages


def test_silent_id_rename_fails_until_declared(workspace, toolkit):
    """既有 ID 被改名且无变更对照 → 未通过；补上对照与依据后放行。"""
    checker, review = toolkit
    write_report(workspace, make_report(GOOD_ROWS))
    assert checker.run_check(workspace)["status"] == review.PASSED

    assert checker.capture(workspace)["rule_ids"] == ["RULE-01", "RULE-02"]
    renamed = [row("ITEM-01", "RULE-01"), row("ITEM-02", "RULE-02-NEW", "订单表.AMOUNT")]
    write_report(workspace, make_report(renamed))
    result = checker.run_check(workspace)
    assert result["status"] == review.FAILED
    assert categories(result) == {"ID变更", "ID溯源"}

    # 声明了改名也不行：RULE-02-NEW 写得像资料里的编号，资料里却没有（F4b 起不转人工确认，直接算错）
    write_report(workspace, make_report(
        renamed, changes=f"| RULE-02 | RULE-02-NEW | 改名 | {SOURCE} |"))
    result = checker.run_check(workspace)
    assert categories(result) == {"ID溯源"} and not result["manual_pending"]
    assert not review.accepted(workspace)


def test_first_round_id_rename_fails_without_baseline(workspace, toolkit):
    """issue #362 现场：首轮资料分析，规范清单只在上传资料里，没有报告基线。

    此时 check_id_stability 无从比对，必须由 ID 溯源捉住改名。
    """
    checker, review = toolkit
    assert checker.capture(workspace)["rule_ids"] == []  # 确认基线为空
    write_report(workspace, make_report(
        [row("ITEM-01", "RULE-01"), row("ITEM-02", "RULE-02-NEW", "订单表.AMOUNT")]))

    result = checker.run_check(workspace)

    assert result["status"] == review.FAILED
    assert categories(result) == {"ID溯源"}
    assert "RULE-02-NEW" in " ".join(i["message"] for i in result["issues"])


def test_failed_report_ids_never_become_baseline(workspace, toolkit):
    """未通过报告里编造的 ID 不能经下一轮 capture 变成基线。

    失败后重新做资料分析 / 追加资料分析都会再跑一次 capture；基线若取自当前报告，编造的
    ID 就成了"已验收的合法出处"，报告一字不改第二轮即可通过。
    """
    checker, review = toolkit
    checker.capture(workspace)
    write_report(workspace, make_report(
        [row("ITEM-01", "RULE-01"), row("ITEM-02", "RULE-02-NEW", "订单表.AMOUNT")]))
    assert checker.run_check(workspace)["status"] == review.FAILED

    assert checker.capture(workspace)["rule_ids"] == []
    result = checker.run_check(workspace)

    assert result["status"] == review.FAILED
    assert categories(result) == {"ID溯源"}


def test_accepted_baseline_survives_later_failure(workspace, toolkit):
    """基线只随完整验收更新：后一轮失败既不清空、也不改写上一轮已验收的 ID。"""
    checker, review = toolkit
    write_report(workspace, make_report(GOOD_ROWS))
    assert checker.run_check(workspace)["status"] == review.PASSED

    write_report(workspace, make_report(
        [row("ITEM-01", "RULE-01"), row("ITEM-02", "RULE-02-NEW", "订单表.AMOUNT")]))
    assert checker.run_check(workspace)["status"] == review.FAILED

    snapshot = checker.capture(workspace)
    assert snapshot["rule_ids"] == ["RULE-01", "RULE-02"]
    assert snapshot["captured_from"] == ["research/报告.md"]


def test_self_numbered_new_ids_pass_without_manual_confirmation(workspace, toolkit):
    """资料里没有的 ITEM-/RULE- 编号是助手自编的：首轮直接通过，不转人工确认（F4b：编号不问人）。"""
    checker, review = toolkit
    rows = [row("ITEM-01", "RULE-01"), row("ITEM-99", "RULE-99", "订单表.AMOUNT")]
    write_report(workspace, make_report(rows))
    result = checker.run_check(workspace)
    assert checker.check_exit_code(result) == 0, result
    assert result['manual_pending'] == []
    evidence = review.load_review(workspace)['source_evidence'][SOURCE]['ids']
    assert {e['id'] for e in evidence if e['kind'] == 'assigned'} == {'ITEM-99', 'RULE-99'}
    assert {e['id'] for e in evidence if e['kind'] == 'source_id'} == {'ITEM-01', 'RULE-01'}


def test_manual_pending_blocks_acceptance(workspace, toolkit):
    """确定性检查通过但有未勾选的待决项 → 不算已验收。"""
    checker, review = toolkit
    write_report(workspace, make_report(GOOD_ROWS, gaps="- [ ] 折扣系数缺失，待业务确认"))

    result = checker.run_check(workspace)

    assert result["status"] == review.PASSED
    assert len(result["manual_pending"]) == 1
    assert checker.check_exit_code(result) == 1
    status = review.review_status(workspace)
    assert status["status"] == review.PENDING
    assert status["reason"] == review.REASON_MANUAL_PENDING
    # 人工待决未确认就不算完整验收，ID 不进下一轮基线
    assert checker.capture(workspace)["rule_ids"] == []


@pytest.mark.parametrize('suffix', ['md', 'docx', 'xlsx'])
def test_same_source_ids_across_formats(workspace, toolkit, suffix):
    checker, review = toolkit
    path = workspace / 'resource' / 'others' / f'规范.{suffix}'
    if suffix == 'md':
        path.write_text('合成规范 ITEM-01 RULE-01', encoding='utf-8')
    else:
        # F4c：Word、Excel 原件不解析，按数据接入转出的文本版核对（ADR-0076）
        path.write_bytes(b'PK synthetic')
        text = workspace / 'resource' / 'text' / 'others' / f'规范.{suffix}.md'
        text.parent.mkdir(parents=True, exist_ok=True)
        text.write_text('合成规范 ITEM-01 RULE-01\n', encoding='utf-8')
    location = path.relative_to(workspace).as_posix()
    write_report(workspace, make_report([row('ITEM-01', 'RULE-01', location=location)]))
    assert checker.check_exit_code(checker.run_check(workspace)) == 0
    evidence = review.load_review(workspace)['source_evidence'][location]
    assert len(evidence['source_sha256']) == 64
    assert evidence['extractor_version'] == 1
    assert evidence['anchors']


@pytest.mark.parametrize('mode', ['corrupt', 'unsupported', 'wrong_anchor', 'wrong_id_anchor'])
def test_source_failure_cannot_be_exempted_by_new_id_or_baseline(workspace, toolkit, mode):
    checker, review = toolkit
    path = workspace / 'resource' / 'others' / ('source.pdf' if mode == 'unsupported' else 'source.docx')
    from test_review_integrity import docx
    docx(path)
    if mode in {'corrupt', 'unsupported'}:
        path.write_bytes(b'not supported')
    location = path.relative_to(workspace).as_posix()
    if mode == 'wrong_anchor':
        location += '#p:99'
    if mode == 'wrong_id_anchor':
        location += '#p:1'  # RULE-01 exists here, ITEM-01 is in a table, not here.
    review.write_inventory(workspace, {'item_ids': ['ITEM-01'], 'rule_ids': ['RULE-01']})
    write_report(workspace, make_report([row('ITEM-01', 'RULE-01', location=location)],
        changes=f'| 无 | ITEM-01、RULE-01 | 新增 | {location} |',
        gaps='- [x] 新增ID确认：ITEM-01、RULE-01'))
    result = checker.run_check(workspace)
    assert checker.check_exit_code(result) != 0
    assert not review.accepted(workspace)
    assert review.load_baseline(workspace) == {}
    if mode != 'wrong_id_anchor':
        assert result['status'] == review.UNVERIFIABLE
        assert WorkspaceManager._project_progress(workspace)['research_review']['blocking']


def test_source_existing_id_cannot_be_registered_as_invented(workspace, toolkit):
    checker, _ = toolkit
    write_report(workspace, make_report(GOOD_ROWS,
        changes=f'| 无 | RULE-01 | 新增 | {SOURCE} |'))
    assert 'ID变更类型' in categories(checker.run_check(workspace))


def test_arbitrary_declaration_does_not_bypass_source(workspace, toolkit):
    checker, review = toolkit
    write_report(workspace, make_report([row('ITEM-99', 'RULE-99')],
        changes='| 无 | ITEM-99、RULE-99 | 新增 | 已确认正确 |'))
    result = checker.run_check(workspace)
    assert result['status'] == review.FAILED
    assert 'ID变更依据' in categories(result)


def test_ambiguous_basename_requires_full_path(workspace, toolkit):
    checker, _ = toolkit
    other = workspace / 'resource' / 'second'
    other.mkdir()
    (other / '说明.md').write_text('ITEM-01 RULE-01', encoding='utf-8')
    write_report(workspace, make_report([row('ITEM-01', 'RULE-01', location='说明.md')]))
    assert '来源缺失' in categories(checker.run_check(workspace))


def test_same_size_source_change_invalidates_receipt(workspace, toolkit, monkeypatch):
    checker, review = toolkit
    monkeypatch.setattr(review, 'MAX_HASH_BYTES', 1)
    write_report(workspace, make_report(GOOD_ROWS))
    checker.run_check(workspace)
    path = workspace / SOURCE
    path.write_text(path.read_text(encoding='utf-8').replace('RULE-01', 'RULE-99'), encoding='utf-8')
    assert review.review_status(workspace)['reason'] == review.REASON_INPUTS_CHANGED


def test_source_changed_during_check_cannot_get_valid_receipt(workspace, toolkit, monkeypatch):
    checker, review = toolkit
    write_report(workspace, make_report(GOOD_ROWS))
    original = checker.source_extract.extract
    def changing_source(path):
        result = original(path)
        if path.name == '说明.md':
            path.write_text(path.read_text(encoding='utf-8').replace('RULE-01', 'RULE-99'), encoding='utf-8')
        return result
    monkeypatch.setattr(checker.source_extract, 'extract', changing_source)
    assert checker.run_check(workspace)['status'] == review.UNVERIFIABLE
    assert not review.accepted(workspace)


def test_no_report_cannot_be_verified(workspace, toolkit):
    checker, review = toolkit

    result = checker.run_check(workspace)

    assert result["status"] == "no_report"
    assert checker.check_exit_code(result) == 2
    assert review.review_status(workspace)["status"] == review.NONE


# ---------------------------------------------------------------------------
# 回执失效：任一输入改动都退回待核验
# ---------------------------------------------------------------------------

def test_legacy_project_without_receipt_is_pending(workspace, toolkit):
    """旧项目只有报告、没有回执 → 待核验，不是已完成。"""
    _, review = toolkit
    write_report(workspace, make_report(GOOD_ROWS))

    status = review.review_status(workspace)

    assert status["status"] == review.PENDING
    assert status["reason"] == review.REASON_NO_RECEIPT
    assert review.hint(status)


def _edit_report(root, _review):
    write_report(root, make_report(GOOD_ROWS) + "\n补一句话。\n")


def _change_source_table(root, _review):
    (root / "resource" / "others" / "订单表.csv").write_text(
        "ORDER_ID,AMOUNT,EXTRA\n1,2,3\n", encoding="utf-8")


def _change_cited_document(root, _review):
    """报告「来源位置」引用的资料文档：规范 ID 就取自它，它改了旧结论就不能再用。"""
    (root / "resource" / "others" / "说明.md").write_text(
        "合成业务说明（规范 ID 已调整）\n\n| ITEM-01 | RULE-91 |\n", encoding="utf-8")


def _add_report(root, _review):
    write_report(root, make_report(GOOD_ROWS), name="报告-补充.md")


def _bump_checker_version(root, review):
    path = root / review.REVIEW_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checker_version"] = review.CHECKER_VERSION + 1
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _receipt_from_v1_checker(root, review):
    """分类契约之前（版本 1）的检查器写的回执：没查过分类列与四段候选，不能沿用。

    回执指纹只覆盖报告与源资料，检查项变化只能靠版本号让旧回执失效。
    """
    path = root / review.REVIEW_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checker_version"] = 1
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("mutate,reason", [
    pytest.param(_edit_report, "inputs_changed", id="编辑报告"),
    pytest.param(_change_source_table, "inputs_changed", id="改源表字段"),
    pytest.param(_change_cited_document, "inputs_changed", id="改报告引用的资料文档"),
    pytest.param(_add_report, "reports_changed", id="新增报告"),
    pytest.param(_bump_checker_version, "checker_upgraded", id="检查器升级"),
    pytest.param(_receipt_from_v1_checker, "checker_upgraded", id="分类契约前的旧回执"),
])
def test_receipt_invalidated_by_input_change(workspace, toolkit, mutate, reason):
    checker, review = toolkit
    write_report(workspace, make_report(GOOD_ROWS))
    assert checker.run_check(workspace)["status"] == review.PASSED

    mutate(workspace, review)

    status = review.review_status(workspace)
    assert status["status"] == review.PENDING
    assert status["reason"] == reason


# ---------------------------------------------------------------------------
# CLI 阶段前置（state_helper）
# ---------------------------------------------------------------------------

def test_stage_blocks_extract_object_until_accepted(workspace, toolkit, monkeypatch, capsys):
    checker, review = toolkit
    monkeypatch.chdir(workspace)
    state_helper = importlib.import_module("state_helper")

    assert state_helper._compute_stage() == "未开始"

    # 错误报告存在 → 资料分析待核验，对象提取不放行
    write_report(workspace, make_report(GOOD_ROWS, total=9, subtotal=9))
    checker.run_check(workspace)
    assert state_helper._compute_stage() == "资料分析待核验"
    state_helper.check_prereq("extract-object")
    assert capsys.readouterr().out.strip() == "missing:资料分析完成"

    # 修正并通过验收 → 资料分析完成，前置放行
    write_report(workspace, make_report(GOOD_ROWS))
    assert checker.run_check(workspace)["status"] == review.PASSED
    assert state_helper._compute_stage() == "资料分析完成"
    state_helper.check_prereq("extract-object")
    assert capsys.readouterr().out.strip() == "ok"


def test_flowchart_branch_still_opens_extract_object(workspace, toolkit, monkeypatch, capsys):
    """流程图来源保留原边界：研究门禁不扩大为所有入口强制研究。"""
    checker, _ = toolkit
    monkeypatch.chdir(workspace)
    state_helper = importlib.import_module("state_helper")

    write_report(workspace, make_report(GOOD_ROWS, total=9, subtotal=9))
    checker.run_check(workspace)
    diagrams = workspace / "ontology" / "diagrams"
    diagrams.mkdir(parents=True)
    (diagrams / "主流程.drawio").write_text("<mxfile/>", encoding="utf-8")

    assert state_helper._compute_stage() == "流程图就绪"
    state_helper.check_prereq("extract-object")
    assert capsys.readouterr().out.strip() == "ok"


def test_status_exit_code_tells_extraction_whether_report_is_usable(
        workspace, toolkit, monkeypatch, capsys):
    """流程图与未验收报告并存时阶段前置照样通过，报告能否作为来源由提取命令的
    Step 1 按 `research_checker.py status` 的退出码决定：只有已验收才是 0。"""
    checker, review = toolkit
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("sys.argv", ["research_checker.py", "status"])

    write_report(workspace, make_report(GOOD_ROWS, total=9, subtotal=9))
    checker.run_check(workspace)
    assert checker.main() == 1  # 未通过

    write_report(workspace, make_report(GOOD_ROWS))
    assert checker.main() == 1  # 报告改了但还没重新核验，旧回执失效

    assert checker.run_check(workspace)["status"] == review.PASSED
    assert checker.main() == 0
    capsys.readouterr()


# ---------------------------------------------------------------------------
# 后端项目进度：资料分析阶段改读回执
# ---------------------------------------------------------------------------

def _no_report(_root, _checker):
    pass


def _failed_report(root, checker):
    write_report(root, make_report(GOOD_ROWS, total=9, subtotal=9))
    checker.run_check(root)


def _accepted_report(root, checker):
    write_report(root, make_report(GOOD_ROWS))
    checker.run_check(root)


def _legacy_report(root, _checker):
    """旧项目：有报告、无回执。保留历史但显示待核验，不自动算完成。"""
    write_report(root, make_report(GOOD_ROWS))


@pytest.mark.parametrize("setup,stages_done,status,reason,blocking", [
    pytest.param(_no_report, 0, "none", "no_report", False, id="无报告"),
    pytest.param(_failed_report, 0, "failed", "ok", True, id="报告未通过"),
    pytest.param(_accepted_report, 1, "passed", "ok", False, id="报告已验收"),
    pytest.param(_legacy_report, 0, "pending", "no_receipt", True, id="旧项目无回执"),
])
def test_project_progress_follows_receipt(
        workspace, toolkit, setup, stages_done, status, reason, blocking):
    """对象提取之前：报告没通过验收就停在资料分析，不放行后续阶段。"""
    checker, _ = toolkit
    setup(workspace, checker)

    progress = WorkspaceManager._project_progress(workspace)

    assert progress["stages_done"] == stages_done
    assert progress["research_review"]["status"] == status
    assert progress["research_review"]["reason"] == reason
    assert progress["research_review"]["blocking"] is blocking
    if status == "failed":
        assert progress["research_review"]["errors"] >= 1
        assert progress["research_review"]["hint"]


@pytest.mark.parametrize("setup,status", [
    pytest.param(_legacy_report, "pending", id="旧项目无回执"),
    pytest.param(_failed_report, "failed", id="报告未通过"),
])
def test_unaccepted_report_stops_blocking_once_objects_exist(workspace, toolkit, setup, status):
    """验收只管进入对象提取之前。对象一旦提取，资料分析按已越过处理：进度照常往后
    计，只留待核验的状态供界面提醒——否则旧项目升级后进度整体归零、导出入口消失，
    界面还会与命令行"已到达的最高阶段优先"的口径相反。"""
    checker, _ = toolkit
    setup(workspace, checker)
    objects = workspace / "ontology" / "object"
    objects.mkdir(parents=True)
    (objects / "合成订单.md").write_text("# 对象描述\n合成。\n", encoding="utf-8")

    progress = WorkspaceManager._project_progress(workspace)

    assert progress["research_review"]["status"] == status  # 提醒仍在
    assert progress["research_review"]["blocking"] is False
    assert progress["stages_done"] == 1  # 资料分析；建模还差操作（0.3.0 起对象与操作合成「建模」），流程图是可选阶段


def test_objects_alone_do_not_count_research_as_done(workspace, toolkit):
    """没有任何报告时资料分析仍是未完成：放开的只是"有报告但未验收"这一种情况。"""
    objects = workspace / "ontology" / "object"
    objects.mkdir(parents=True)
    (objects / "合成订单.md").write_text("# 对象描述\n合成。\n", encoding="utf-8")

    progress = WorkspaceManager._project_progress(workspace)

    assert progress["research_review"]["status"] == "none"
    assert progress["research_review"]["blocking"] is False
    # 各阶段独立计数：资料分析不计完成；只有对象时建模也还没做完
    assert progress["stages_done"] == 0
