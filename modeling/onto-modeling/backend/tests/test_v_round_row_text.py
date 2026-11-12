"""「这一轮改了什么」每行前面已有「新增」「修改」标签，行文不再重复写动词。

原来行文又写一遍动词，界面上成了「修改 修改「补货申请单」」（V 阶段截图）。
行文按契约示例写类型和链接：「对象 [[门店]]」；不是条目的文件按业务名写（F4b-paths：资料分析报告、
建模蓝图、资料名），认不出的合成一行「另有 N 处改动」，不出文件名和路径。
"""
from __future__ import annotations

from app.kernel.changesets import service
from app.services.session_host import timeline
from tests.test_changesets import project  # noqa: F401 - 夹具


def _texts(card):
    return [item["text"] for group in card["groups"] for item in group["items"]]


def test_timeline_round_card_rows_do_not_repeat_the_verb():
    card = timeline.round_changes_card({"added": ["ontology/object/门店.md"], "modified": ["research/index.md"]})
    assert _texts(card) == ["对象 [[门店]]", "资料分析报告"]


def test_round_card_names_files_by_business_name_and_folds_unknown_ones():
    card = timeline.round_changes_card({
        "added": ["resource/others/业务通知.md", "research/20261103-资料分析.md", "notes/草稿.md"],
        "modified": ["research/blueprint.json", "resource/text/others/业务通知.md.md", ".onto/x.json"],
    })
    texts = _texts(card)
    assert texts == ["资料「业务通知」", "资料分析报告", "建模蓝图", "另有 3 处改动"]
    assert card["title"] == "改了 6 处"
    more = card["groups"][-1]["items"][-1]
    assert more["change"] is None and more["target"] is None and more["count"] == 3
    assert not any(".md" in text or "/" in text for text in texts)


def test_changeset_round_card_rows_do_not_repeat_the_verb(project, monkeypatch):
    workspaces, project_id, _root = project
    monkeypatch.setattr(service, "_changes_of", lambda manager, root, record: service.describe(
        manager, root, [("A", "ontology/operation/临期识别.md"), ("M", "research/index.md"), ("M", "x/y.txt")])["items"])
    card = service.round_card(workspaces, project_id, {"id": "cs-x", "state": "pending_confirm", "checks": {}}, "c1")
    assert _texts(card) == ["操作 [[临期识别]]", "资料分析报告", "另有 1 处改动"]
