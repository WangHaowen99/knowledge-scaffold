"""ontology_checker 对「# 数据来源」首行的格式提示：与 package_mapping 的解析同一口径。"""

import json
import subprocess
from pathlib import Path

import pytest

from app import config

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"

OBJECT_DOC = """# 对象描述
合成订单。

# 对象别名
无

# 英文名称
Order

# 数据来源
{source}

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
| 订单号 | ORDER_ID | 合成订单号 | 是 | 字符串 | 无 | 无 |
"""


def data_source_issues(tmp_path: Path, source: str) -> list[dict]:
    ontology = tmp_path / "ontology"
    (ontology / "object").mkdir(parents=True)
    (ontology / "object" / "订单.md").write_text(OBJECT_DOC.format(source=source), encoding="utf-8")
    result = subprocess.run(
        ["python3", str(SCRIPTS / "ontology_checker.py"), str(ontology), "--json"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return [i for i in json.loads(result.stdout) if i["category"] == "数据来源格式"]


@pytest.mark.parametrize("source", [
    "- 数据库表 `T_ORDER` 的 `ORDER_ID` 列（按月汇总）。",
    "期初余额由上月末结转。",
])
def test_sentence_first_line_is_flagged(tmp_path: Path, source: str):
    issues = data_source_issues(tmp_path, source)
    assert len(issues) == 1 and issues[0]["severity"] == "警告"
    assert "首行应只写表名" in issues[0]["message"]


@pytest.mark.parametrize("source", [
    "T_ORDER\n\n> 数据库表 `T_ORDER` 的 `ORDER_ID` 列。",
    "`T_ORDER`",
    "订单 明细(2024)",
    "无\n\n> 由上游流程写入，不落库。",
    "无（派生对象，不落库）",
])
def test_bare_name_or_none_passes(tmp_path: Path, source: str):
    assert data_source_issues(tmp_path, source) == []
