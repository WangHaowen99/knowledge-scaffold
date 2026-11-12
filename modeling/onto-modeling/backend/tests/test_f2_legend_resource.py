"""资料区不再自带「图示说明定义.drawio」（F2 第 3 条；V 报告需要改设计第 6 条）。

原来建项目时从资料区模板复制一份图例到 `resource/diagrams/`：资料分析每次都把它当成用户的资料去读、出决策卡，
小结卡还把它算成一份资料。图例是流程图脚本（结构检查、流程摘要）认「虚拟项」用的，现在随 core 包放在
`.claude/reference/`；资料区里有图例的（存量项目）照旧用资料区那份，存量项目不动。测试数据全部为合成内容。
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

from app import config
from app.services import research_view
from app.services.workspace import WorkspaceManager
from tests import proc

LEGEND = "图示说明定义.drawio"
# 开始 → 合成活动 → 结束，外加一个图例里标成虚拟项的蓝色虚线框（不连线）
DIAGRAM = """<mxfile><diagram name="合成流程"><mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="a" value="开始" style="strokeWidth=2;html=1;shape=mxgraph.flowchart.terminator;whiteSpace=wrap;" vertex="1" parent="1"><mxGeometry as="geometry"/></mxCell>
<mxCell id="b" value="合成活动" style="rounded=1;whiteSpace=wrap;html=1;" vertex="1" parent="1"><mxGeometry as="geometry"/></mxCell>
<mxCell id="c" value="结束" style="strokeWidth=2;html=1;shape=mxgraph.flowchart.terminator;whiteSpace=wrap;" vertex="1" parent="1"><mxGeometry as="geometry"/></mxCell>
<mxCell id="v" value="合成分组" style="rounded=1;whiteSpace=wrap;html=1;dashed=1;dashPattern=12 12;fillColor=none;strokeColor=#0000FF;" vertex="1" parent="1"><mxGeometry as="geometry"/></mxCell>
<mxCell id="e1" edge="1" parent="1" source="a" target="b"><mxGeometry as="geometry"/></mxCell>
<mxCell id="e2" edge="1" parent="1" source="b" target="c"><mxGeometry as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>
"""


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    meta = manager.create_project("合成项目", owner="operator_a")
    root = manager.project_root(meta["id"])
    if not (root / ".claude" / "scripts" / "check_diagrams.py").exists():
        pytest.skip("镜像外没有装配目录")
    return root


def _script(root: Path, name: str):
    """从项目的装配目录加载脚本（新命名空间，不污染 sys.modules 里的同名模块）。"""
    scripts = root / ".claude" / "scripts"
    sys.path.append(str(scripts))  # 脚本只用标准库；追加在末尾，不抢先于已有同名模块
    try:
        spec = importlib.util.spec_from_file_location(f"f2_{name}_{uuid.uuid4().hex[:6]}", scripts / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(scripts))


def test_new_project_resource_has_no_legend_and_scripts_use_the_packaged_one(project):
    root = project
    assert not (root / "resource" / "diagrams" / LEGEND).exists()
    packaged = root / ".claude" / "reference" / LEGEND
    assert packaged.is_file()
    check = _script(root, "check_diagrams")
    assert Path(check.find_legend(root)).resolve() == packaged.resolve()
    summarize = _script(root, "summarize_diagram")
    assert summarize.find_legend(root).resolve() == packaged.resolve()
    # 结构检查认得图例里的虚拟项：蓝色虚线框不算孤立节点
    diagram = root / "ontology" / "diagrams" / "合成流程.drawio"
    diagram.parent.mkdir(parents=True, exist_ok=True)
    diagram.write_text(DIAGRAM, encoding="utf-8")
    assert check.check_diagram(diagram, check.find_legend(root))["issues"] == []
    assert check.check_diagram(diagram, root / "nothing.drawio")["issues"]  # 没有图例时会误报
    run = proc.run([sys.executable, ".claude/scripts/check_diagrams.py", "ontology/diagrams/合成流程.drawio"],
                   cwd=root, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0 and "[有问题] 0 个" in run.stdout, run.stdout + run.stderr
    # 流程摘要带上图例的节点类型说明
    text = summarize.summarize(str(diagram), str(summarize.find_legend(root)))
    assert "节点类型说明" in text


def test_existing_project_keeps_its_own_legend(project):
    root = project
    own = root / "resource" / "diagrams" / LEGEND
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text((root / ".claude" / "reference" / LEGEND).read_text(encoding="utf-8"), encoding="utf-8")
    assert Path(_script(root, "check_diagrams").find_legend(root)) == own
    assert _script(root, "summarize_diagram").find_legend(root) == own
    # 存量项目里的那份仍算资源模板，不进资料地图
    assert research_view.is_template(f"resource/diagrams/{LEGEND}") is True
    assert research_view.is_template("resource/diagrams/合成流程.drawio") is False
