"""F4b-research：蓝图目标只有一份（项目目标）、去掉「不在范围」、资料分析只读文本版、编号不再问人。

用户 2026-11-03 定：
- 蓝图「要解决的问题」参考建项目时写的建模目标；资料和目标对不上就在蓝图上提问，不由策略代答；
- 「不在范围」不需要；
- PDF 这类资料转成 Markdown 交给数据接入（F4a 已做），资料分析只读文本；
- 编号这类内部机制的问题不再问人。

测试数据全部为合成内容。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from app.services.flow import blueprint as blueprint_service
from app.services.packs import contracts
from app.services.session_host import injection
from tests.test_blueprint import DRAFT, _write_mainline, env  # noqa: F401 - env 是夹具
from tests.test_research_review import make_report, row, toolkit, workspace, write_report  # noqa: F401 - 夹具

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"
GOAL_QUESTION = {"kind": "goal", "title": "资料讲的是门店盘点，和目标里的补货单对不上，以哪个为准？",
                 "background": "资料里没有补货单的内容", "suggested_goal": "店长按盘点结果生成补货单"}


def bp():
    return contracts.default().load("blueprint")


# ---------------------------------------------------------------------------
# 蓝图脚本：没有「不在范围」，目标问题
# ---------------------------------------------------------------------------


def test_draft_has_no_out_of_scope_and_old_files_still_load():
    doc = bp().from_draft({**DRAFT, "out_of_scope": ["采购价格"]})
    assert "out_of_scope" not in doc
    assert "out_of_scope" not in bp().dumps(doc)
    assert "out_of_scope" not in bp().view(doc)
    assert "范围外" not in bp().brief(doc, doc["slices"][0]["id"])
    # 旧文件里有这一项：照常能读、能改，写回时去掉
    old = {**doc, "out_of_scope": ["采购价格"]}
    assert not bp().validate(old)
    edited = bp().apply_edit(old, {"mode": "draft", "out_of_scope": ["别的"]})
    assert "out_of_scope" not in bp().dumps(edited)
    assert "out_of_scope" not in bp().view(old)


def test_goal_question_gets_fixed_options_and_only_one_is_kept():
    doc = bp().from_draft({**DRAFT, "questions": [GOAL_QUESTION, {**GOAL_QUESTION, "title": "第二个目标问题"},
                                                  *DRAFT["questions"]]})
    goal = [item for item in doc["questions"] if item.get("kind") == "goal"]
    assert len(goal) == 1
    assert goal[0]["suggested_goal"] == "店长按盘点结果生成补货单"
    assert [option["label"] for option in goal[0]["options"]] == ["改成：店长按盘点结果生成补货单", "保持原目标"]
    plain = bp().from_draft({**DRAFT, "questions": [{**GOAL_QUESTION, "suggested_goal": ""}]})
    assert [option["label"] for option in plain["questions"][0]["options"]] == ["保持原目标"]


# ---------------------------------------------------------------------------
# 接口：目标取项目目标；目标问题挡确认、回答后改项目目标
# ---------------------------------------------------------------------------


def test_blueprint_goal_is_the_project_goal(env):
    client, workspaces, _manager, project_id, root = env
    workspaces._update_meta(project_id, goal="合成目标：按规则生成补货单")
    _write_mainline(root, {**bp().from_draft(DRAFT), "goal_md": "助手另写的一份目标",
                           "goal_anchor": "resource/others/说明.md#L1"})
    got = client.get(f"/api/v1/projects/{project_id}/blueprint").json()
    assert got["goal_md"] == "合成目标：按规则生成补货单"
    assert got["goal_anchor"] is None and "out_of_scope" not in got
    assert client.patch(f"/api/v1/projects/{project_id}", json={"goal": "改过的合成目标"}).status_code == 200
    assert client.get(f"/api/v1/projects/{project_id}/blueprint").json()["goal_md"] == "改过的合成目标"
    card = blueprint_service.blueprint_card(bp(), bp().from_draft(DRAFT), "c1", research_verified=True,
                                            goal="改过的合成目标")
    assert card["goal_md"] == "改过的合成目标" and "out_of_scope" not in card and card["goal_question"] is None


def test_goal_question_blocks_confirm_until_answered_and_adopting_updates_the_goal(env):
    client, workspaces, _manager, project_id, root = env
    workspaces._update_meta(project_id, goal="合成目标：按规则生成补货单")
    _write_mainline(root, bp().from_draft({**DRAFT, "questions": [GOAL_QUESTION]}))
    base = f"/api/v1/projects/{project_id}/blueprint"
    got = client.get(base).json()
    question = got["questions"][0]
    assert question["kind"] == "goal"
    blocked = client.post(f"{base}/confirm", json={"base_revision": got["revision"], "next": "L2"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "blueprint_question_pending"
    assert blocked.json()["details"]["question_id"] == question["id"]
    card = blueprint_service.blueprint_card(bp(), bp().load(root), "c1", research_verified=True, goal="x")
    assert card["goal_question"]["id"] == question["id"] and card["goal_question"]["answered"] is False
    saved = client.put(base, json={"base_revision": got["revision"], "mode": "draft",
                                   "answers": [{"question_id": question["id"], "option_id": question["options"][0]["id"]}]})
    assert saved.status_code == 200, saved.text
    assert workspaces.get_project(project_id)["goal"] == "店长按盘点结果生成补货单"
    assert saved.json()["goal_md"] == "店长按盘点结果生成补货单"
    blueprint_service.prepare_confirm(workspaces, project_id, {"base_revision": saved.json()["revision"], "next": "L2"})


def test_keeping_the_goal_leaves_it_unchanged(env):
    client, workspaces, _manager, project_id, root = env
    workspaces._update_meta(project_id, goal="合成目标：按规则生成补货单")
    _write_mainline(root, bp().from_draft({**DRAFT, "questions": [GOAL_QUESTION]}))
    base = f"/api/v1/projects/{project_id}/blueprint"
    got = client.get(base).json()
    question = got["questions"][0]
    keep = question["options"][-1]["id"]
    saved = client.put(base, json={"base_revision": got["revision"], "mode": "draft",
                                   "answers": [{"question_id": question["id"], "option_id": keep}]})
    assert saved.status_code == 200, saved.text
    assert workspaces.get_project(project_id)["goal"] == "合成目标：按规则生成补货单"


# ---------------------------------------------------------------------------
# 注入：资料分析时把项目目标交给助手
# ---------------------------------------------------------------------------


def test_injection_carries_the_project_goal(env, monkeypatch):
    _client, workspaces, _manager, project_id, root = env
    workspaces._update_meta(project_id, goal="合成目标：按规则生成补货单")
    monkeypatch.setattr(injection, "stage_hint", lambda project_root: "本体项目当前阶段：未开始。")
    first = injection.build_context(root, user_text="/ontology-research", session_start=True, last_injected=None)
    assert "合成目标：按规则生成补货单" in first.text and "建模目标" in first.text
    again = injection.build_context(root, user_text="继续", session_start=False, last_injected=first.digest)
    assert again.text == ""
    workspaces._update_meta(project_id, goal="改过的合成目标")
    changed = injection.build_context(root, user_text="继续", session_start=False, last_injected=first.digest)
    assert "改过的合成目标" in changed.text


# ---------------------------------------------------------------------------
# 资料分析只读文本版
# ---------------------------------------------------------------------------


def _text_version(root: Path, original: str, body: str) -> str:
    rel = f"resource/text/{original.removeprefix('resource/')}.md"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<!-- 文本版 原文件修订 0000 转换器 convert-pdf@1.0.0 -->\n<!-- 第 1 页 -->\n" + body,
                    encoding="utf-8")
    return rel


def _cli(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "source_extract.py"), *args], cwd=root,
                          capture_output=True, text=True)


def test_source_list_and_cli_point_binaries_to_their_text_version(workspace):
    (workspace / "resource" / "others" / "通知.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (workspace / "resource" / "others" / "导图.png").write_bytes(b"\x89PNG synthetic")
    text = _text_version(workspace, "resource/others/通知.pdf", "合成条款：补货单当天生成。\n")
    listed = _cli(workspace, "--sources")
    assert listed.returncode == 0, listed.stderr
    sources = {item["material"]: item for item in json.loads(listed.stdout)["materials"]}
    assert sources["resource/others/通知.pdf"] == {"material": "resource/others/通知.pdf", "read": text,
                                                     "kind": "converted"}
    assert sources["resource/others/说明.md"]["read"] == "resource/others/说明.md"
    assert sources["resource/others/导图.png"]["read"] is None and sources["resource/others/导图.png"]["kind"] == "missing"
    assert "resource/text/others/通知.pdf.md" not in sources and "resource/materials.json" not in sources
    pdf = _cli(workspace, "resource/others/通知.pdf")
    assert pdf.returncode == 2 and json.loads(pdf.stdout) == {"status": "use_text_version", "text": text}
    png = _cli(workspace, "resource/others/导图.png")
    assert png.returncode == 2 and json.loads(png.stdout)["status"] == "no_text_version"
    ok = _cli(workspace, text)
    assert ok.returncode == 0 and json.loads(ok.stdout)["segments"][0]["anchor"] == "L1"


def test_report_anchored_to_a_text_version_verifies(workspace, toolkit):
    checker, review = toolkit
    (workspace / "resource" / "others" / "通知.pdf").write_bytes(b"%PDF-1.4 synthetic")
    text = _text_version(workspace, "resource/others/通知.pdf", "合成条款：补货单当天生成。\n")
    # 夹具的另一份资料里列着 ITEM-01～05：那几个算资料里的编号，这里用资料里没有的自编编号
    write_report(workspace, make_report([row("ITEM-11", "RULE-11", location=f"{text}#L3")]))
    result = checker.run_check(workspace)
    assert checker.check_exit_code(result) == 0, result["issues"]


def test_anchor_on_an_original_with_a_text_version_is_a_warning_and_corpus_skips_it(workspace, toolkit, monkeypatch):
    checker, _review = toolkit
    from test_review_integrity import docx

    docx(workspace / "resource" / "others" / "规范.docx")
    _text_version(workspace, "resource/others/规范.docx", "合成规范 ITEM-01 RULE-01\n")
    read = []
    original = checker.source_extract.extract
    monkeypatch.setattr(checker.source_extract, "extract", lambda path: (read.append(Path(path).name), original(path))[1])
    write_report(workspace, make_report([row("ITEM-01", "RULE-01", location="resource/others/规范.docx")]))
    result = checker.run_check(workspace)
    warnings = [issue for issue in result["issues"] if issue["severity"] == "警告"]
    assert any(issue["category"] == "出处" and "文本版" in issue["message"] for issue in warnings), result["issues"]
    checker.CheckContext(workspace, {}).corpus()
    assert read.count("规范.docx") == 1  # 只有报告指名的那一次；语料按文本版读


# ---------------------------------------------------------------------------
# 编号不问人
# ---------------------------------------------------------------------------


def _plain_source(root: Path) -> str:
    path = root / "resource" / "others" / "资料.md"
    path.write_text("合成业务说明：门店每天提交补货申请，超过上限要审批。\n", encoding="utf-8")
    return "resource/others/资料.md"


def test_self_numbered_ids_need_no_confirmation(workspace, toolkit):
    checker, review = toolkit
    source = _plain_source(workspace)
    rows = [row("ITEM-11", "RULE-11", location=f"{source}#L1"), row("ITEM-12", "无", "订单表.AMOUNT", location=source)]
    write_report(workspace, make_report(rows))
    result = checker.run_check(workspace)
    assert checker.check_exit_code(result) == 0, result
    assert result["manual_pending"] == []
    evidence = review.load_review(workspace)["source_evidence"][source]["ids"]
    assert {item["kind"] for item in evidence} == {"assigned"}


def test_an_id_that_claims_to_come_from_the_material_must_be_there(workspace, toolkit):
    checker, review = toolkit
    source = _plain_source(workspace)
    write_report(workspace, make_report([row("GB-3.2", "无", location=source)]))
    result = checker.run_check(workspace)
    assert result["status"] == review.FAILED
    assert any(issue["category"] == "ID溯源" for issue in result["issues"])


def test_new_self_numbered_ids_after_a_baseline_need_a_change_row_not_a_confirmation(workspace, toolkit):
    checker, review = toolkit
    source = _plain_source(workspace)
    review.write_inventory(workspace, {"item_ids": ["ITEM-11"], "rule_ids": []})
    rows = [row("ITEM-11", "无", location=source), row("ITEM-12", "无", "订单表.AMOUNT", location=source)]
    write_report(workspace, make_report(rows))
    assert any(issue["category"] == "ID变更" for issue in checker.run_check(workspace)["issues"])
    write_report(workspace, make_report(rows, changes=f"| 无 | ITEM-12 | 新增 | {source} |"))
    result = checker.run_check(workspace)
    assert checker.check_exit_code(result) == 0, result
    assert result["manual_pending"] == []


def test_no_toolkit_text_asks_to_confirm_ids():
    toolkit_dir = Path(config.ASSETS_DIR) / "workspace-toolkit"
    for rel in ("agents/research-agent.md", "commands/ontology-research.md", "scripts/research_checker.py"):
        assert "新增ID确认" not in (toolkit_dir / rel).read_text(encoding="utf-8"), rel
