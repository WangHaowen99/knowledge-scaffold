"""ADR-0104 数据源热加载 · 工具箱：table_profile 把数据源资料当表格读（表头、外键），source_extract 按工作表锚点取出处。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.connector_gateway.source import render
from app.evidence import convert, sources

SCRIPTS = Path(__file__).resolve().parents[2] / "assets" / "workspace-toolkit" / "scripts"


def _workspace(tmp_path: Path, *, purpose="schema") -> Path:
    root = tmp_path / "ws"
    rel = "resource/excel/订单库.datasource"
    data = sources.dump_document("db-mysql", {"host": "db.example.invalid"}, "ds-0123456789ab", ["password"])
    (root / "resource" / "excel").mkdir(parents=True)
    (root / rel).write_bytes(data)
    collected = render.Collected(
        tables=[{"name": "orders", "comment": "", "columns": [{"name": "id", "type": "bigint", "key": "pk"},
                                                              {"name": "customer_id", "type": "bigint", "key": "fk"}],
                 "rows": [[1, 7], [2, 8]]},
                {"name": "customer", "comment": "", "columns": [{"name": "id", "type": "bigint", "key": "pk"},
                                                                {"name": "grade", "type": "varchar(4)"}],
                 "rows": [[7, "A"], [8, "B"]]}],
        facts=[{"kind": "foreign_key", "table": "orders", "columns": ["customer_id"], "ref_table": "customer",
                "ref_columns": ["id"], "name": "fk_c"},
               {"kind": "reference", "table": "orders", "columns": ["id"], "ref_table": "customer",
                "ref_columns": ["id"], "basis": "命名"}])
    markdown, _, _ = render.render(collected, "s")
    revision = __import__("app.services.workspace.layout", fromlist=["_content_revision"])._content_revision(data)
    text = convert.header("订单库.datasource", revision, "db-mysql@0.1.0") + "\n\n" + markdown
    (root / "resource" / "text" / "excel").mkdir(parents=True)
    (root / "resource" / "text" / "excel" / "订单库.datasource.md").write_text(text, encoding="utf-8")
    (root / "resource" / "materials.json").write_text(json.dumps({rel: {"purpose": purpose}}), encoding="utf-8")
    return root


def _run(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / args[0]), *args[1:]], cwd=root, capture_output=True,
                          text=True, timeout=60)


def test_table_profile_headers_with_foreign_keys(tmp_path):
    root = _workspace(tmp_path)
    result = _run(root, "table_profile.py", "--headers")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "ok" and summary["tables"] == {"orders": 2, "customer": 2}
    assert summary["foreign_keys"] == 2
    written = json.loads((root / ".ontology-state" / "table-headers.json").read_text(encoding="utf-8"))
    assert written["tables"]["orders"] == ["id", "customer_id"]
    declared = [item for item in written["foreign_keys"] if item["basis"] == "declared"]
    assert declared == [{"table": "orders", "columns": ["customer_id"], "ref_table": "customer", "ref_columns": ["id"],
                         "name": "fk_c", "basis": "declared", "source": "resource/excel/订单库.datasource"}]


def test_table_profile_ignores_facts_outside_datasources(tmp_path):
    """同样的注释块出现在别的资料（如 PDF 转出的文本）里不认。"""
    root = _workspace(tmp_path, purpose="evidence")
    result = _run(root, "table_profile.py", "--headers")
    assert json.loads(result.stdout)["status"] == "no_schema"
    assert "foreign_keys" not in json.loads((root / ".ontology-state" / "table-headers.json").read_text("utf-8"))


def test_source_extract_reads_sheet_anchors(tmp_path):
    root = _workspace(tmp_path)
    result = _run(root, "source_extract.py", "resource/excel/订单库.datasource", "--limit", "50")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    anchors = [segment["anchor"] for segment in data.get("segments") or []]
    assert "sheet:orders!A1" in anchors and "sheet:customer!B2" in anchors
