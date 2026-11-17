"""I1：PL-12 界面入口的位置 `entries[].where`（设计调整 R22，契约补充 UI 文案第 22 条）。"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.packs import extensions
from tests.test_pack_extensions import env  # noqa: F401  （夹具按名字注入）


def _view(*pages: str):
    return SimpleNamespace(manifest=SimpleNamespace(pages=lambda: [{"id": page} for page in pages]))


def test_where_values():
    assert extensions._entry_where(_view("scenarios"), "quick_action", "wb-l2") == "composer"
    assert extensions._entry_where(_view(), "extra_action", "scenarios") == "scenarios"
    # 没写或写的不是插件页：取本包自己的第一个插件页；没有插件页时没有位置
    assert extensions._entry_where(_view("scripts"), "extra_action", "") == "scripts"
    assert extensions._entry_where(_view("scripts"), "extra_action", "wb-l2") == "scripts"
    assert extensions._entry_where(_view(), "extra_action", None) is None


def test_entries_in_extensions_carry_where(env):
    client, _manager, project_id, _tmp = env
    ext = client.get(f"/api/v1/projects/{project_id}/extensions").json()
    assert [(item["id"], item["where"]) for item in ext["entries"]] == [("quick-run", "composer"), ("extra-run", "scenarios")]
