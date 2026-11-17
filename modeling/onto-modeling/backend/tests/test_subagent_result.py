"""子代理交回的 JSON → 给人看的说法：规则检查的 `[]` 不再原样显示，检查、修复、影响分析按条目字段换算。"""

import json

from app.services import subagent_result as sr


def _issue(severity, message, file="operation/门店补货.md"):
    return {"severity": severity, "category": "链接失效", "file": file, "message": message}


def test_empty_check_result_reads_as_no_problem():
    assert sr.is_machine("[]") and sr.summarize("[]") == "没有发现问题"
    assert sr.summarize("```json\n[]\n```") == "没有发现问题"
    # 影响分析无需改动时在 `[]` 后跟一句说明：用那句说明
    assert sr.summarize("[]\n参考内容仅复述了现有规则，无需更新。") == "参考内容仅复述了现有规则，无需更新。"


def test_issues_count_by_severity_and_list_details_with_business_names():
    issues = [_issue("建议", "描述可以更具体"), _issue("错误", "Wiki 链接 [[补货单]] 无对应文件"),
              _issue("警告", "字段「营业状态」缺少取值说明", file="ontology/object/门店.md")]
    text = sr.summarize(json.dumps(issues, ensure_ascii=False))
    assert text.split("\n") == [
        "发现 3 个问题：错误 1 · 警告 1 · 建议 1",
        "· 错误 · 操作「门店补货」：Wiki 链接 [[补货单]] 无对应文件",
        "· 警告 · 对象「门店」：字段「营业状态」缺少取值说明",
        "· 建议 · 操作「门店补货」：描述可以更具体",
    ]
    many = sr.summarize(json.dumps([_issue("错误", f"问题 {i}") for i in range(8)], ensure_ascii=False))
    assert many.split("\n")[0] == "发现 8 个问题：错误 8" and many.split("\n")[-1] == "· …另有 3 条"
    assert len(many.split("\n")) == 1 + sr.DETAIL_MAX + 1


def test_fix_impact_and_extraction_results():
    fixes = [{"issue_id": 1, "applied": True, "change_summary": "移除失效链接"},
             {"issue_id": 2, "applied": False, "reason": "needs_human_judgment"}]
    assert sr.summarize(json.dumps(fixes, ensure_ascii=False)).split("\n") == [
        "改好 1 条，1 条没改", "· 没改：要人工判断", "· 已改：移除失效链接"]
    impact = [{"file": "ontology/object/门店.md", "operation": "modify", "change_summary": "新增字段「营业状态」"},
              {"file": "ontology/object/补货单.md", "operation": "create", "change_summary": "新建对象"}]
    assert sr.summarize(json.dumps(impact, ensure_ascii=False)).split("\n") == [
        "涉及 2 处：新建 1 · 修改 1", "· 修改对象「门店」：新增字段「营业状态」", "· 新建对象「补货单」：新建对象"]
    extracted = {"rules": ["补货时限"], "metrics": [], "backlinks_added": 2, "skipped": ["步骤 2 表述含糊"]}
    assert sr.summarize(json.dumps(extracted, ensure_ascii=False)) == "抽出规则 1 条 · 指标 0 个\n· 跳过：步骤 2 表述含糊"
    assert sr.summarize('[{"name": "甲"}, {"name": "乙"}]') == "交回 2 条结果"
    assert sr.summarize('{"status": "ok"}') == ""


def test_prose_and_truncated_json_are_told_apart():
    # 截断了的 JSON 认得出、换不出（界面用默认说法）；`[[链接]]` 开头的话、普通的话不算
    cut = '[{"severity": "错误", "message": "Wiki 链接'
    assert sr.is_machine(cut) and sr.summarize(cut) == "" and sr.display(cut) is None
    for prose in ("[[补货单]] 链接失效，已改", "检查完了：3 个问题", ""):
        assert not sr.is_machine(prose)
    assert sr.display("检查完了") == "检查完了" and sr.display(None) is None
    # 换好的说法再过一遍不变（读历史时新旧记录都过）
    text = sr.summarize(json.dumps([_issue("错误", "缺字段")], ensure_ascii=False))
    assert sr.display(text) == text and sr.display("[]") == "没有发现问题"
