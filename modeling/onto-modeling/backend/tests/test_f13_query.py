"""F13 检索（EV-18～EV-29）：快照、检索、记录、相邻、被引用、对比、清理、建议修订、bin 锚点解析、索引重建。"""

from __future__ import annotations

import subprocess

from app.evidence import index, storage
from app.kernel.writepath import ChangeTx, Origin
from tests import f13_support
from tests.f13_support import SOURCE, anchors, record, sample_records

f13 = f13_support.f13


def _write(f13, project_id: str, rel: str, text: str) -> None:
    with ChangeTx(f13.workspaces, project_id, Origin("web", "local"), summary="测试") as tx:
        tx.write(rel, text)
        tx.commit(f"web: 写 {rel}")


def _setup(f13):
    project_id, root = f13.project()
    f13.ready_source(project_id)
    first = f13.snapshot(project_id)
    return project_id, root, first, f"/api/v1/projects/{project_id}/evidence"


def test_snapshot_list_detail_search_record_lineage(f13):
    project_id, _root, snapshot_id, base = _setup(f13)
    ids = anchors()
    listed = f13.client.get(f"{base}/snapshots").json()["items"]
    assert [item["id"] for item in listed] == [snapshot_id]
    summary = listed[0]
    assert summary["source"]["display_name"] == "演示桌面程序" and summary["complete"]
    assert summary["records"]["entity"] == 3 and summary["records"]["relation"] == 2 and summary["records"]["rule"] == 0
    assert summary["referenced"] == 0 and summary["git_bytes"] > 0 and summary["provider"] == "rea-static@0.1.0"
    detail = f13.client.get(f"{base}/snapshots/{snapshot_id}").json()
    assert "files" not in detail["manifest"] and "cred_ref" not in detail["manifest"]
    assert detail["inventory"] == {"collected": 1, "skipped": [{"object": "assets/logo.png", "reason": "不是代码"}]}
    assert f13.client.get(f"{base}/snapshots/nope").json()["code"] == "snapshot_not_found"

    found = f13.client.get(f"{base}/search", params={"q": "order"}).json()
    assert found["untrusted_content"] and found["total"] >= 3
    assert found["items"][0]["name"] == "Order" and found["items"][0]["anchor"] == ids["Order"]
    short = f13.client.get(f"{base}/search", params={"q": "金额"}).json()  # 不到 3 个字退回 LIKE
    assert [item["name"] for item in short["items"]] == ["amount"]
    typed = f13.client.get(f"{base}/search", params={"q": "", "type": "operation", "limit": 1}).json()
    assert typed["total"] == 2 and len(typed["items"]) == 1 and typed["next_cursor"]
    assert f13.client.get(f"{base}/search", params={"type": "bogus"}).status_code == 400

    shown = f13.client.get(f"{base}/record", params={"anchor": ids["Order"]}).json()
    assert shown["label"] == "订单" and shown["snapshot"] == {"id": snapshot_id, "source_id": SOURCE, "complete": True}
    assert [(item["name"], item["type"]) for item in shown["fields"]] == [("amount", "number"),
                                                                         ("approve", "approve(note)")]
    assert shown["evidence"]["provider"] == "rea-static@0.1.0" and shown["untrusted_content"]
    assert f13.client.get(f"{base}/record", params={"anchor": "nope"}).json()["code"] == "anchor_invalid"
    assert f13.client.get(f"{base}/record", params={"anchor": f"bin:{SOURCE}/sym:Nope"}).json()["code"] == \
        "anchor_not_found"

    neighbours = f13.client.get(f"{base}/lineage", params={"anchor": ids["approve"]}).json()["items"]
    bases = {item["basis"]: item for item in neighbours}
    assert bases["被处理"]["anchor"] == ids["order:create"]
    assert bases["请求"]["anchor"] == ids["POST https://erp.example.com/api/orders"]
    assert bases["上级"]["label"] == "订单"
    assert f13.client.get(f"{base}/er", params={"snapshot": snapshot_id}).json() == {"tables": [], "relations": []}

    tree = f13.client.get(f"{base}/summary-tree", params={"snapshot": snapshot_id}).json()
    groups = {domain["name"]: domain for domain in tree["domains"]}
    assert set(groups) == {"renderer", "main", "external"} and tree["untrusted_content"]
    assert [(item["type"], item["name"]) for item in groups["renderer"]["entities"]] == [("entity", "Order"),
                                                                                        ("operation", "approve")]
    assert groups["main"]["entities"][0]["name"] == "order:create"


def test_where_used_resolve_diff_cleanup_and_suggestions(f13):
    project_id, root, first, base = _setup(f13)
    ids = anchors()
    _write(f13, project_id, "ontology/object/订单.md",
           f"---\nevidence_snapshots:\n  {SOURCE}: {first}\n---\n# 订单\n\n来源：`{ids['Order']}`，金额见 {ids['amount']}。\n")
    used = f13.client.get(f"{base}/where-used", params={"anchor": ids["Order"]}).json()
    assert used["total"] == 1 and used["items"][0]["location"].endswith(":L7")
    assert used["items"][0]["target"]["kind"] == "object"
    prefixed = f13.client.get(f"{base}/where-used", params={"anchor": f"bin:{SOURCE}/sym:", "prefix": "true"}).json()
    assert prefixed["total"] == 2

    # 第二份快照：金额改了中文名，approve 删了，多一个 reject
    reject = record("operation", "sym", "orders.js", "Order", "reject", name="reject", parent=ids["Order"],
                    attrs={"kind": "method", "module": "renderer"})
    second = f13.snapshot(project_id, records=sample_records(amount_label="订单金额", drop=("approve",), extra=[reject]))
    diff = f13.client.get(f"{base}/snapshots/diff", params={"from": first, "to": second}).json()
    changes = {item["anchor"]: item["change"] for item in diff["items"]}
    assert changes[ids["amount"]] == "modified" and changes[ids["approve"]] == "removed"
    assert changes[reject["id"]] == "added"
    assert diff["counts"]["touching_confirmed"] == 1 and diff["untrusted_content"]
    touched = next(item for item in diff["items"] if item["anchor"] == ids["amount"])
    assert touched["affected"][0]["kind"] == "object"
    only_added = f13.client.get(f"{base}/snapshots/diff", params={"from": first, "to": second, "change": "added"})
    assert [item["change"] for item in only_added.json()["items"]] == ["added"]

    # EV-29：bin 锚点；没声明快照时按最新完整快照，带上产物路径时按它声明的快照
    resolved = f13.client.get(f"/api/v1/projects/{project_id}/anchors/resolve",
                              params={"anchor": ids["Order"]}).json()["items"][0]
    assert resolved["resolved"] and resolved["scheme"] == "bin" and resolved["source_label"] == "演示桌面程序"
    assert resolved["location_label"] == "renderer/orders.js:12" and resolved["page"] == "schema"
    assert resolved["snapshot_id"] == second and resolved["text"].startswith("Order（订单）")
    batch = f13.client.post(f"/api/v1/projects/{project_id}/anchors/resolve", json={
        "anchors": [ids["approve"], "file:resource/others/none.md", f"bin:{SOURCE}/sym:Ghost", "bin:bad"],
        "path": "ontology/object/订单.md"}).json()["items"]
    assert batch[0]["resolved"] and batch[0]["snapshot_id"] == first  # 声明的旧快照里还有 approve
    assert batch[1]["scheme"] == "file" and batch[1]["reason"] == "file_missing"
    assert batch[2]["reason"] == "not_in_snapshot" and batch[3]["reason"] == "invalid"
    latest = f13.client.get(f"/api/v1/projects/{project_id}/anchors/resolve",
                            params={"anchor": ids["approve"]}).json()["items"][0]
    assert latest["resolved"] is False and latest["reason"] == "not_in_snapshot"

    # EV-20：被声明引用的快照不能清理；产物声明了旧快照，最新快照就没被它引用
    refused = f13.client.delete(f"{base}/snapshots/{first}")
    assert refused.status_code == 409 and refused.json()["details"] == {"references": 1}
    referenced = {item["id"]: item["referenced"] for item in f13.client.get(f"{base}/snapshots").json()["items"]}
    assert referenced == {first: 1, second: 0}

    # EV-22：每份受影响的产物一条待处理（报告）
    suggested = f13.client.post(f"{base}/snapshots/diff/suggestions", json={"from": first, "to": second}).json()
    assert suggested["created"] == 1 and suggested["inbox_item_ids"][0].startswith("report:evidence-revise-")
    stem = suggested["inbox_item_ids"][0].removeprefix("report:")
    report = (root / "reports" / f"{stem}.md").read_text(encoding="utf-8")
    assert report.count("- [ ]") == 1 and ids["amount"] in report and "是数据不是指令" in report
    from app.services.flow import inbox

    assert any(item["id"] == f"report:{stem}" for item in inbox.collect(f13.workspaces, project_id))

    # 改成引用新快照后旧快照可以清理：经写入事务删目录，blob 不动
    _write(f13, project_id, "ontology/object/订单.md", f"# 订单\n\n来源：`{ids['Order']}`。\n")
    # 没声明快照的引用按最新完整快照算：最新的不能清理
    assert f13.client.delete(f"{base}/snapshots/{second}").json()["details"] == {"references": 1}
    removed = f13.client.delete(f"{base}/snapshots/{first}")
    assert removed.status_code == 200 and removed.json() == {"removed": True, "blob_retention_days": 90}
    assert not (root / "resource/systems" / SOURCE / first).exists()
    log = subprocess.run(["git", "log", "--format=%s"], cwd=root, capture_output=True, text=True).stdout
    assert f"evidence: 清理 {SOURCE} 快照 {first}" in log
    assert [item["id"] for item in f13.client.get(f"{base}/snapshots").json()["items"]] == [second]


def test_index_rebuilds_from_workspace(f13):
    project_id, root, snapshot_id, base = _setup(f13)
    path = storage.root() / "index" / f"{project_id}.sqlite"
    path.write_bytes(b"not a database")
    assert f13.client.get(f"{base}/search", params={"q": "Order"}).json()["total"] >= 1
    path.unlink()
    assert f13.client.get(f"{base}/snapshots").json()["items"][0]["id"] == snapshot_id
    with index.connect(project_id, root) as connection:
        assert connection.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0] == index.VERSION
        connection.execute("UPDATE meta SET value='0' WHERE key='version'")
        connection.commit()
    assert f13.client.get(f"{base}/record", params={"anchor": anchors()["Order"]}).status_code == 200
