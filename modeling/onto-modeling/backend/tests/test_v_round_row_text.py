"""「这一轮改了什么」每行前面已经有「新增」「修改」标签；行文原来又写一遍动词，界面上成了「修改 修改「补货申请单」」
（V 阶段截图）。行文按契约示例写类型和链接：「对象 [[门店]]」，文件写「「index.md」」。"""
from __future__ import annotations

from app.kernel.changesets import service
from app.services.session_host import timeline
from tests.test_changesets import project  # noqa: F401 - 夹具


def _texts(card):
    return [item["text"] for group in card["groups"] for item in group["items"]]


def test_timeline_round_card_rows_do_not_repeat_the_verb():
    card = timeline.round_changes_card({"added": ["ontology/object/门店.md"], "modified": ["research/index.md"]})
    assert _texts(card) == ["对象 [[门店]]", "「index.md」"]


def test_changeset_round_card_rows_do_not_repeat_the_verb(project, monkeypatch):
    workspaces, project_id, _root = project
    monkeypatch.setattr(service, "_changes_of", lambda manager, root, record: [
        {"group": "operation", "change": "add",
         "target": {"kind": "operation", "id": "operation:临期识别", "key": "临期识别", "name": "临期识别"}},
        {"group": "other", "change": "mod", "target": {"type": "file", "key": "research/index.md", "title": "index.md"}},
    ])
    card = service.round_card(workspaces, project_id, {"id": "cs-x", "state": "pending_confirm", "checks": {}}, "c1")
    assert _texts(card) == ["操作 [[临期识别]]", "「index.md」"]
