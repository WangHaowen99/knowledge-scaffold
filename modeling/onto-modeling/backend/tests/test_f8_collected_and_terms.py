"""F8 新加的两处（ADR-0078；契约变更 F8）。

- 平台代存这一轮（助手没提交完：打断、出错、提交被检查拦下后没修完）：合入时记 collected；检查有错误时改动卡多一行提示，
  待处理出一条「检查未过」，「交给助手处理」发 /ontology-validate 并收起这一条；助手自己提交完的不出；
- 统一叫法由平台随消息注入（不分段以后不在段说明里）：读助手所在的工作区，副本里刚写的蓝图也算。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import time

from app.kernel.changesets import service, store
from app.services.session_host import injection
from tests.test_blueprint import DRAFT, _write_mainline, bp, env  # noqa: F401 - env 是夹具
from tests.test_changesets import _pending
from tests.test_session_host import FakeEngine, result

OWNER = "employee_0001"
BROKEN = "# 坏对象\n\n## 对象描述\n\n缺了必需章节，提到 [[不存在的东西]]\n"
COLLECTED_HINT = {"tone": "warn", "text": "助手没提交完，平台代存了这一轮，检查没过的在待处理里"}


def _check_items(client, project_id: str) -> list[dict]:
    items = client.get(f"/api/v1/projects/{project_id}/inbox").json()["items"]
    return [item for item in items if item["kind"] == "check_findings"]


def test_round_the_assistant_committed_is_not_marked_collected(env):
    client, workspaces, _manager, project_id, root = env
    _copy, record = _pending(workspaces, project_id, root, rel="ontology/object/坏对象.md", text=BROKEN)
    service.settle(workspaces, project_id, actor=OWNER)
    merged = store.get(root, record["id"])
    assert merged["state"] == "merged" and merged["collected"] is False and merged["checks"]["errors"] >= 1
    assert COLLECTED_HINT not in service.round_card(workspaces, project_id, merged, "rc-x")["hints"]
    assert _check_items(client, project_id) == []


def test_collected_round_without_errors_stays_quiet(env):
    client, workspaces, _manager, project_id, root = env
    _copy, record = _pending(workspaces, project_id, root, commit=False)
    service.settle(workspaces, project_id, actor=OWNER)
    merged = store.get(root, record["id"])
    assert merged["collected"] is True and not merged["checks"]["errors"]
    assert COLLECTED_HINT not in service.round_card(workspaces, project_id, merged, "rc-x")["hints"]
    assert _check_items(client, project_id) == []


def test_collected_round_with_errors_shows_on_the_card_and_in_the_inbox(env):
    client, workspaces, _manager, project_id, root = env
    _copy, record = _pending(workspaces, project_id, root, rel="ontology/object/坏对象.md", text=BROKEN, commit=False)
    service.settle(workspaces, project_id, actor=OWNER)
    merged = store.get(root, record["id"])
    assert merged["state"] == "merged" and merged["collected"] is True and merged["checks"]["errors"] >= 1
    assert (root / "ontology/object/坏对象.md").is_file()  # 只提示不拦：代存的改动照样进主线
    assert service.round_card(workspaces, project_id, merged, "rc-x")["hints"][0] == COLLECTED_HINT
    items = _check_items(client, project_id)
    assert len(items) == 1 and items[0]["id"] == f"check_findings:{record['id']}"
    assert items[0]["kind_label"] == "检查未过" and items[0]["actions"] == ["send_to_assistant", "dismiss"]
    assert items[0]["impact"] and items[0]["impact"][0]["items"]
    # 交给助手处理：发 /ontology-validate、带上检查没过的问题，这一条收起
    FakeEngine.scripts = [[result()]]
    done = client.post(f"/api/v1/projects/{project_id}/inbox/{items[0]['id']}/actions",
                       json={"action": "send_to_assistant"})
    assert done.status_code == 200, done.text
    for _ in range(300):
        if FakeEngine.sent:
            break
        time.sleep(0.01)
    assert FakeEngine.sent and FakeEngine.sent[0].startswith("/ontology-validate")
    assert "修正上一轮平台代存版本里检查没过的问题" in FakeEngine.sent[0]
    assert _check_items(client, project_id) == []


def test_terms_are_injected_from_the_workspace_the_assistant_is_in(env, monkeypatch):
    _client, workspaces, _manager, project_id, root = env
    assert injection.terms_hint(root) == ""  # 没有蓝图
    doc = bp().from_draft(DRAFT)
    _write_mainline(root, doc)
    assert injection.terms_hint(root) == ""  # 有歧义的叫法还没定
    chosen = bp().apply_edit(doc, {"mode": "draft", "term_choices": [{"term_id": "t1", "chosen": "临期"}]})
    _write_mainline(root, chosen)
    assert injection.terms_hint(root) == "统一叫法：临期（不用 近效期）（资料分析定的，条目命名和正文照用）"
    monkeypatch.setattr(injection, "stage_hint", lambda project_root: "本体项目当前阶段：未开始。")
    context = injection.build_context(root, user_text="/ontology-extract-object", session_start=True,
                                      last_injected=None)
    assert "统一叫法：临期（不用 近效期）" in context.text
    # 资料分析那一轮在副本里刚改了叫法：助手在副本里，读副本的
    copy = service.copy_for_turn(workspaces, project_id, root)
    again = bp().apply_edit(chosen, {"mode": "draft", "term_choices": [{"term_id": "t1", "chosen": "近效期"}]})
    (copy / "research" / "blueprint.json").write_text(bp().dumps(again), encoding="utf-8")
    assert injection.terms_hint(copy).startswith("统一叫法：近效期（不用 临期）")
    assert injection.terms_hint(root).startswith("统一叫法：临期（不用 近效期）")
