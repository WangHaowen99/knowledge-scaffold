"""内核内置的流程图解析与写出（详设 5.9.4、5.9.5；契约 7.11 FlowchartModel、AR-05、AR-08）。

数据全为合成的「门店补货」场景：标准编辑器的模型写成 draw.io 再读回要一致；agent 或人手画的、不带内核属性
的图按样式推断类型；压缩页面能解出来展示；高级编辑器保存时同 id 节点沿用上一版的描述、引用、待确认标记。
"""

from __future__ import annotations

import base64
import copy
import urllib.parse
import xml.etree.ElementTree as ET
import zlib

import pytest

from app.kernel.artifacts import flowchart
from app.kernel.artifacts.flowchart import FlowchartError


def _ref(kind: str, key: str, name: str) -> dict:
    return {"kind": kind, "id": f"{kind}:{key}", "key": key, "name": name}


def _node(nid, ntype, label, x, y, **extra) -> dict:
    node = {"id": nid, "type": ntype, "label": label, "description": "", "x": x, "y": y,
            "refs": [], "pending": False, "pending_note": None}
    node.update(extra)
    return node


def _edge(eid, etype, src, dst, label="", condition="") -> dict:
    return {"id": eid, "type": etype, "from": src, "to": dst, "label": label, "condition": condition}


def _replenish_model() -> dict:
    return {
        "storage": "standard",
        "process": {
            "name": "门店补货",
            "description": "门店按陈列上限申请补货，\n超限时走区域审批。",
            "roles": ["门店店长", "区域经理", "仓配中心"],
            "inputs": ["补货申请单", "陈列上限表"],
            "exceptions": ["审批驳回"],
            "handoffs": ["门店 → 仓配中心"],
        },
        "nodes": [
            _node("n-start", "start", "门店发起补货", 200, 60),
            _node("n-submit", "step", "提交补货申请", 200, 160,
                  description="店长在系统里填报 SKU 与数量 & 期望到货日",
                  refs=[_ref("object", "replenish_request", "补货申请"),
                        _ref("operation", "store_replenish", "门店补货")]),
            _node("n-check", "step", "校验陈列上限", 200, 260, refs=[_ref("rule", "display_cap", "陈列上限")]),
            _node("n-stock", "data", "门店库存台账", 460, 260),
            _node("n-over", "decision", "申请量 > 陈列上限？", 200, 380),
            _node("n-approve", "step", "区域审批", 412.5, 380, pending=True, pending_note="审批阈值待业务确认"),
            _node("n-ship", "step", "仓配中心发货", 200, 520),
            _node("n-reject", "end", "驳回申请", 620, 380),
            _node("n-end", "end", "补货完成", 200, 620),
        ],
        "edges": [
            _edge("e1", "seq", "n-start", "n-submit"),
            _edge("e2", "seq", "n-submit", "n-check"),
            _edge("e3", "data", "n-check", "n-stock", "读取库存"),
            _edge("e4", "seq", "n-check", "n-over"),
            _edge("e5", "cond", "n-over", "n-approve", "是", "申请量 > 陈列上限"),
            _edge("e6", "cond", "n-over", "n-ship", "否", "申请量 <= 陈列上限"),
            _edge("e7", "exc", "n-approve", "n-reject", "审批驳回"),
            _edge("e8", "seq", "n-approve", "n-ship"),
            _edge("e9", "seq", "n-ship", "n-end"),
        ],
        "drawio_xml": None,
        "operation": _ref("operation", "store_replenish", "门店补货"),
    }


def _cells_by_id(xml: str) -> dict[str, ET.Element]:
    """写出的 draw.io 里按 id 取单元格（object 包着的取 object）。"""
    root = ET.fromstring(xml).find("diagram/mxGraphModel/root")
    return {el.get("id"): el for el in root}


# ---- 往返 ----


def test_round_trip_keeps_model():
    model = _replenish_model()
    xml = flowchart.render(model)
    back = flowchart.parse(xml, key="replenish")

    assert back["storage"] == "standard"
    assert back["drawio_xml"] is None
    assert back["process"] == model["process"]
    assert back["operation"] == model["operation"]
    assert back["edges"] == model["edges"]
    assert [n["id"] for n in back["nodes"]] == [n["id"] for n in model["nodes"]]
    for got, want in zip(back["nodes"], model["nodes"]):
        assert abs(got["x"] - want["x"]) <= 1 and abs(got["y"] - want["y"]) <= 1, got["id"]
        assert {k: v for k, v in got.items() if k not in ("x", "y")} == \
               {k: v for k, v in want.items() if k not in ("x", "y")}

    # 再写一遍内容不变（坐标、属性都稳定）
    assert flowchart.render(back) == xml


def test_render_shapes_metadata_and_geometry():
    xml = flowchart.render(_replenish_model())
    cells = _cells_by_id(xml)

    meta = cells["0"]
    assert meta.tag == "object" and meta.get("process_name") == "门店补货"
    assert meta.get("process_roles") == '["门店店长","区域经理","仓配中心"]'
    assert '"store_replenish"' in meta.get("operation")
    assert cells["1"].get("parent") == "0"

    def style(cid):
        return cells[cid].find("mxCell").get("style")

    assert style("n-start").startswith("ellipse;") and style("n-end").startswith("ellipse;")
    assert style("n-submit").startswith("rounded=1;")
    assert style("n-over").startswith("rhombus;")
    assert "shape=parallelogram" in style("n-stock")
    assert "dashed=1" in style("e7") and "strokeColor=#b85450" in style("e7")
    assert "dashed=1" in style("e3") and "dashPattern" in style("e3")
    assert "dashed" not in style("e5") and "dashed" not in style("e1")

    approve = cells["n-approve"]
    assert approve.get("pending") == "1" and approve.get("pending_note") == "审批阈值待业务确认"
    geo = approve.find("mxCell/mxGeometry")
    # 中心 412.5,380，步骤默认 160x60 → 左上角 332.5,350
    assert (geo.get("x"), geo.get("y"), geo.get("width"), geo.get("height")) == ("332.5", "350", "160", "60")
    decision = cells["n-over"].find("mxCell/mxGeometry")
    assert (decision.get("width"), decision.get("height")) == ("120", "80")
    assert cells["e5"].find("mxCell").get("source") == "n-over"


def test_render_escapes_label_html():
    model = _replenish_model()
    model["nodes"][4]["label"] = "库存 < 上限 & 未锁定"
    xml = flowchart.render(model)
    assert _cells_by_id(xml)["n-over"].get("label") == "库存 &lt; 上限 &amp; 未锁定"
    assert flowchart.parse(xml)["nodes"][4]["label"] == "库存 < 上限 & 未锁定"


def test_render_with_previous_keeps_sizes_and_page():
    model = _replenish_model()
    previous = flowchart.render(model).replace('id="flowchart" name="门店补货"', 'id="page-a1" name="补货主流程"')
    # 上一版里「提交补货申请」被拉大到 220x90，「校验陈列上限」也拉大但这次改成了判断节点
    root = ET.fromstring(previous)
    cells = {el.get("id"): el for el in root.find("diagram/mxGraphModel/root")}
    cells["n-submit"].find("mxCell/mxGeometry").attrib.update(width="220", height="90")
    cells["n-check"].find("mxCell/mxGeometry").attrib.update(width="300", height="100")
    previous = ET.tostring(root, encoding="unicode")

    model["nodes"][1]["x"] = 240
    model["nodes"][2]["type"] = "decision"
    xml = flowchart.render(model, previous=previous)

    diagram = ET.fromstring(xml).find("diagram")
    assert (diagram.get("id"), diagram.get("name")) == ("page-a1", "补货主流程")
    out = _cells_by_id(xml)
    submit = out["n-submit"].find("mxCell/mxGeometry")
    assert (submit.get("width"), submit.get("height")) == ("220", "90")
    assert (submit.get("x"), submit.get("y")) == ("130", "115")
    check = out["n-check"].find("mxCell/mxGeometry")
    assert (check.get("width"), check.get("height")) == ("120", "80")
    back = flowchart.parse(xml)
    assert back["process"]["name"] == "门店补货"
    assert (back["nodes"][1]["x"], back["nodes"][1]["y"]) == (240, 160)

    # 上一版解析不了时当作没有
    assert flowchart.render(_replenish_model(), previous="not xml") == flowchart.render(_replenish_model())


def test_render_rejects_bad_ids():
    model = _replenish_model()
    model["edges"][0]["id"] = "n-start"
    with pytest.raises(FlowchartError):
        flowchart.render(model)
    model = _replenish_model()
    model["nodes"][0]["id"] = "1"
    with pytest.raises(FlowchartError):
        flowchart.render(model)


# ---- 不带内核属性的图 ----

PLAIN_DRAWIO = """<mxfile host="app.diagrams.net">
  <diagram name="门店补货流程" id="pg1">
    <mxGraphModel dx="800" dy="600" grid="1">
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        <mxCell id="lane-store" value="门店店长" style="swimlane;horizontal=0;html=1;" vertex="1" parent="1">
          <mxGeometry x="0" y="0" width="900" height="200" as="geometry"/>
        </mxCell>
        <mxCell id="lane-area" value="区域经理" style="swimlane;horizontal=0;html=1;" vertex="1" parent="1">
          <mxGeometry x="0" y="200" width="900" height="200" as="geometry"/>
        </mxCell>
        <mxCell id="s" value="开始" style="ellipse;whiteSpace=wrap;html=1;" vertex="1" parent="lane-store">
          <mxGeometry x="40" y="70" width="80" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="a" value="&lt;b&gt;提交&lt;/b&gt;补货申请&lt;br&gt;（门店）&amp;nbsp;&amp;amp;  确认" style="rounded=1;whiteSpace=wrap;html=1;" vertex="1" parent="lane-store">
          <mxGeometry x="160" y="60" width="120" height="60" as="geometry"/>
        </mxCell>
        <mxCell id="d" value="超出陈列上限？" style="rhombus;whiteSpace=wrap;html=1;" vertex="1" parent="1">
          <mxGeometry x="340" y="40" width="100" height="100" as="geometry"/>
        </mxCell>
        <mxCell id="ledger" value="库存台账" style="shape=cylinder3;whiteSpace=wrap;html=1;" vertex="1" parent="1">
          <mxGeometry x="500" y="20" width="60" height="80" as="geometry"/>
        </mxCell>
        <mxCell id="r" value="区域审批" style="whiteSpace=wrap;html=1;" vertex="1" parent="lane-area">
          <mxGeometry x="300" y="70" width="120" height="60" as="geometry"/>
        </mxCell>
        <mxCell id="x" value="驳回" style="ellipse;html=1;" vertex="1" parent="1">
          <mxGeometry x="600" y="270" width="80" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="t" value="结束" style="ellipse;html=1;" vertex="1" parent="1">
          <mxGeometry x="600" y="70" width="80" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="memo" value="备注：周末不补货" style="text;html=1;" vertex="1" parent="1">
          <mxGeometry x="20" y="420" width="160" height="30" as="geometry"/>
        </mxCell>
        <mxCell id="c1" style="edgeStyle=orthogonalEdgeStyle;html=1;" edge="1" parent="1" source="s" target="a">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="c2" style="html=1;" edge="1" parent="1" source="a" target="d">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="c3" value="是" style="html=1;" edge="1" parent="1" source="d" target="r">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="c4" style="html=1;" edge="1" parent="1" source="d" target="t">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="c4-label" value="否" style="edgeLabel;html=1;" vertex="1" connectable="0" parent="c4">
          <mxGeometry x="-0.2" relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="c5" value="驳回" style="dashed=1;strokeColor=#FF3333;html=1;" edge="1" parent="1" source="r" target="x">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="c6" style="dashed=1;html=1;" edge="1" parent="1" source="a" target="ledger">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="c7" value="库存异常" style="dashed=1;html=1;" edge="1" parent="1" source="ledger" target="x">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="c8" style="html=1;" edge="1" parent="1" source="a" target="lost">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
        <mxCell id="c9" style="html=1;" edge="1" parent="1" source="memo" target="t">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""


def test_parse_plain_drawio_infers_types(tmp_path):
    path = tmp_path / "门店补货.drawio"
    path.write_text(PLAIN_DRAWIO, encoding="utf-8")
    model = flowchart.parse(path.read_bytes(), key="replenish")

    assert model["storage"] == "standard" and model["drawio_xml"] is None
    assert model["process"]["name"] == "门店补货流程"
    assert model["process"]["roles"] == ["门店店长", "区域经理"]
    assert model["operation"] is None

    nodes = {n["id"]: n for n in model["nodes"]}
    assert list(nodes) == ["s", "a", "d", "ledger", "r", "x", "t"]   # 泳道、文本、连线标签不算节点
    assert {k: n["type"] for k, n in nodes.items()} == {
        "s": "start", "a": "step", "d": "decision", "ledger": "data", "r": "step", "x": "end", "t": "end",
    }
    assert nodes["a"]["label"] == "提交补货申请 （门店） & 确认"
    # 中心坐标；泳道里的节点加上泳道偏移
    assert (nodes["s"]["x"], nodes["s"]["y"]) == (80, 90)
    assert (nodes["r"]["x"], nodes["r"]["y"]) == (360, 300)
    assert (nodes["d"]["x"], nodes["d"]["y"]) == (390, 90)
    assert nodes["a"]["refs"] == [] and nodes["a"]["pending"] is False and nodes["a"]["description"] == ""

    edges = {e["id"]: e for e in model["edges"]}
    assert list(edges) == ["c1", "c2", "c3", "c4", "c5", "c6", "c7"]   # 端点缺失或不是节点的跳过
    assert {k: e["type"] for k, e in edges.items()} == {
        "c1": "seq", "c2": "seq", "c3": "cond", "c4": "cond", "c5": "exc", "c6": "data", "c7": "exc",
    }
    assert (edges["c3"]["label"], edges["c3"]["condition"]) == ("是", "是")
    assert (edges["c4"]["label"], edges["c4"]["condition"]) == ("否", "否")
    assert edges["c1"]["condition"] == "" and edges["c5"]["condition"] == ""

    assert flowchart.summary(model) == "7 个节点 · 7 条连线"


def test_parse_bare_graph_model_uses_key_and_include_xml():
    xml = """<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>
      <mxCell id="a" value="校验陈列上限" vertex="1" parent="1"><mxGeometry x="0" y="0" width="100" height="40" as="geometry"/></mxCell>
    </root></mxGraphModel>"""
    model = flowchart.parse(xml, key="replenish_check")
    assert model["process"]["name"] == "replenish_check"
    assert model["nodes"][0]["type"] == "step" and (model["nodes"][0]["x"], model["nodes"][0]["y"]) == (50, 20)
    assert flowchart.parse(xml, include_xml=True)["drawio_xml"] == xml

    # 默认页名不当流程名
    page = '<mxfile><diagram name="Page-1" id="p">' + xml + "</diagram></mxfile>"
    assert flowchart.parse(page, key="replenish_check")["process"]["name"] == "replenish_check"
    assert flowchart.parse("<mxfile/>", key="empty")["nodes"] == []


def test_onto_type_wins_unless_shape_class_changed():
    model = _replenish_model()
    # 高级编辑器里把「区域审批」换成菱形；「校验陈列上限」换成普通矩形（通用形状，保持 onto_type）
    root = ET.fromstring(flowchart.render(model))
    cells = {el.get("id"): el for el in root.find("diagram/mxGraphModel/root")}
    cells["n-approve"].find("mxCell").set("style", "rhombus;html=1;")
    cells["n-check"].find("mxCell").set("style", "whiteSpace=wrap;html=1;")
    cells["e1"].find("mxCell").set("style", "dashed=1;html=1;")
    back = flowchart.parse(ET.tostring(root, encoding="unicode"))
    types = {n["id"]: n["type"] for n in back["nodes"]}
    assert types["n-approve"] == "decision" and types["n-check"] == "step"
    assert {e["id"]: e["type"] for e in back["edges"]}["e1"] == "data"


# ---- 压缩格式 ----


def _compress(inner: str) -> str:
    quoted = urllib.parse.quote(inner, safe="~()*!.'")
    deflater = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = deflater.compress(quoted.encode("ascii")) + deflater.flush()
    return base64.b64encode(raw).decode("ascii")


def test_parse_compressed_diagram():
    inner = flowchart.render(_replenish_model())
    graph = ET.tostring(ET.fromstring(inner).find("diagram/mxGraphModel"), encoding="unicode")
    text = f'<mxfile host="app.diagrams.net"><diagram name="门店补货" id="z1">{_compress(graph)}</diagram></mxfile>'

    model = flowchart.parse(text.encode("utf-8"))
    assert model["storage"] == "drawio_compressed"
    assert model["drawio_xml"] == text
    assert [n["id"] for n in model["nodes"]] == [n["id"] for n in _replenish_model()["nodes"]]
    assert model["nodes"][5]["pending_note"] == "审批阈值待业务确认"
    assert len(model["edges"]) == 9
    assert model["operation"]["key"] == "store_replenish"


def test_parse_bad_compressed_payload():
    with pytest.raises(FlowchartError):
        flowchart.parse('<mxfile><diagram name="p" id="p">@@not-base64-deflate@@</diagram></mxfile>')
    payload = _compress('<!DOCTYPE x [<!ENTITY a "b">]><mxGraphModel/>')
    with pytest.raises(FlowchartError, match="DOCTYPE"):
        flowchart.parse(f'<mxfile><diagram name="p" id="p">{payload}</diagram></mxfile>')


# ---- 高级编辑器保存 ----


def test_from_drawio_edit_keeps_kernel_fields_by_id():
    previous = _replenish_model()
    # draw.io 里重画过：同 id 的节点只剩普通单元格，改了名字、挪了位置；新增一个节点和一条连线
    xml = """<mxfile><diagram name="Page-1" id="p1"><mxGraphModel><root>
      <mxCell id="0"/><mxCell id="1" parent="0"/>
      <mxCell id="n-start" value="门店发起补货" style="ellipse;html=1;" vertex="1" parent="1"><mxGeometry x="140" y="35" width="120" height="50" as="geometry"/></mxCell>
      <mxCell id="n-submit" value="提交补货申请单" style="rounded=1;html=1;" vertex="1" parent="1"><mxGeometry x="120" y="130" width="160" height="60" as="geometry"/></mxCell>
      <object id="n-approve" label="区域审批" description="区域经理在 24 小时内审批" pending="0"><mxCell style="rounded=1;html=1;" vertex="1" parent="1"><mxGeometry x="300" y="350" width="160" height="60" as="geometry"/></mxCell></object>
      <mxCell id="n-notify" value="通知门店收货" style="rounded=1;html=1;" vertex="1" parent="1"><mxGeometry x="120" y="230" width="160" height="60" as="geometry"/></mxCell>
      <mxCell id="n-end" value="补货完成" style="ellipse;html=1;" vertex="1" parent="1"><mxGeometry x="140" y="335" width="120" height="50" as="geometry"/></mxCell>
      <mxCell id="e1" style="html=1;" edge="1" parent="1" source="n-start" target="n-submit"><mxGeometry relative="1" as="geometry"/></mxCell>
      <mxCell id="e-new" style="html=1;" edge="1" parent="1" source="n-submit" target="n-notify"><mxGeometry relative="1" as="geometry"/></mxCell>
      <mxCell id="e-new2" style="html=1;" edge="1" parent="1" source="n-notify" target="n-end"><mxGeometry relative="1" as="geometry"/></mxCell>
    </root></mxGraphModel></diagram></mxfile>"""
    model = flowchart.from_drawio_edit(xml, previous)
    nodes = {n["id"]: n for n in model["nodes"]}

    assert list(nodes) == ["n-start", "n-submit", "n-approve", "n-notify", "n-end"]
    submit = nodes["n-submit"]
    assert submit["label"] == "提交补货申请单" and (submit["x"], submit["y"]) == (200, 160)
    assert submit["description"] == previous["nodes"][1]["description"]
    assert submit["refs"] == previous["nodes"][1]["refs"]
    # XML 带了的以 XML 为准，没带的沿用上一版
    approve = nodes["n-approve"]
    assert approve["description"] == "区域经理在 24 小时内审批"
    assert approve["pending"] is False and approve["pending_note"] == "审批阈值待业务确认"
    assert nodes["n-notify"] == {
        "id": "n-notify", "type": "step", "label": "通知门店收货", "description": "", "x": 200, "y": 260,
        "refs": [], "pending": False, "pending_note": None,
    }
    assert model["process"] == previous["process"]
    assert model["operation"] == previous["operation"]
    assert [e["id"] for e in model["edges"]] == ["e1", "e-new", "e-new2"]
    # 上一版对象没有被改动
    assert previous == _replenish_model()


def test_from_drawio_edit_of_rendered_xml_is_identity_and_without_previous():
    previous = _replenish_model()
    model = flowchart.from_drawio_edit(flowchart.render(previous), copy.deepcopy(previous))
    assert model["nodes"] == previous["nodes"] and model["edges"] == previous["edges"]
    assert model["process"] == previous["process"] and model["operation"] == previous["operation"]

    plain = flowchart.from_drawio_edit(PLAIN_DRAWIO, None)
    assert plain["process"]["name"] == "门店补货流程" and len(plain["nodes"]) == 7


# ---- 校验 ----


def test_validate_clean_model_only_warns_pending():
    errors, warnings = flowchart.validate(_replenish_model())
    assert errors == []
    assert [(w["code"], w["field"]) for w in warnings] == [("FLOW-PENDING", "nodes/n-approve")]
    assert "审批阈值待业务确认" in warnings[0]["message"]


def test_validate_findings_and_fields():
    model = {
        "nodes": [
            _node("a", "step", "提交补货申请", 0, 0),
            _node("a", "step", "重复的节点", 0, 100),
            _node("b", "decision", "超出陈列上限？", 0, 200),
            _node("c", "step", "  ", 0, 300),
            _node("d", "subprocess", "区域审批", 0, 400),
            _node("", "step", "无 id", 0, 500),
            _node("f", "step", "仓配中心发货", 0, 600, pending=True),
        ],
        "edges": [
            _edge("e1", "seq", "a", "b"),
            _edge("e2", "cond", "b", "c"),
            _edge("e3", "seq", "c", "ghost"),
            _edge("e4", "jump", "c", "d"),
            _edge("a", "seq", "d", "f"),
        ],
    }
    errors, warnings = flowchart.validate(model)
    assert [(f["code"], f.get("field")) for f in errors] == [
        ("FLOW-ID", "nodes/a"),
        ("FLOW-LABEL", "nodes/c"),
        ("FLOW-TYPE", "nodes/d"),
        ("FLOW-ID", "nodes"),
        ("FLOW-EDGE-DANGLING", "edges/e3"),
        ("FLOW-TYPE", "edges/e4"),
        ("FLOW-ID", "edges/a"),
    ]
    assert all(f["level"] == "error" for f in errors)
    assert [(f["code"], f.get("field")) for f in warnings] == [
        ("FLOW-PENDING", "nodes/f"),
        ("FLOW-COND-LABEL", "edges/e2"),
        ("FLOW-NO-START", None),
        ("FLOW-NO-END", None),
        ("FLOW-DECISION", "nodes/b"),
    ]
    assert warnings[0]["message"] == "「仓配中心发货」待确认"
    assert all(f["level"] == "warning" for f in warnings)


def test_validate_unreachable_from_start():
    model = _replenish_model()
    model["nodes"].append(_node("n-orphan", "step", "门店盘点", 700, 600))
    model["nodes"].append(_node("n-sheet", "data", "陈列上限表", 50, 260))
    model["edges"].append(_edge("e10", "data", "n-sheet", "n-check"))
    _, warnings = flowchart.validate(model)
    assert [(w["code"], w["field"]) for w in warnings if w["code"] == "FLOW-UNREACHABLE"] == [
        ("FLOW-UNREACHABLE", "nodes/n-orphan"),
    ]


def test_validate_reports_shape_error_instead_of_raising():
    errors, warnings = flowchart.validate({"nodes": "n-start"})
    assert [e["code"] for e in errors] == ["FLOW-SHAPE"] and warnings == []


# ---- 形状核对 ----


def test_normalize_fills_defaults():
    model = flowchart.normalize({
        "nodes": [{"id": "n1", "type": "step", "label": "提交补货申请", "x": 10, "y": 20.5,
                   "refs": [{"kind": "rule", "key": "display_cap"}], "pending_note": ""}],
        "edges": [{"id": "e1", "type": "seq", "from": "n1", "to": "n1"}],
    })
    assert model["storage"] == "standard" and model["operation"] is None and model["drawio_xml"] is None
    assert model["process"] == {"name": "", "description": "", "roles": [], "inputs": [], "exceptions": [],
                                "handoffs": []}
    assert model["nodes"][0] == {
        "id": "n1", "type": "step", "label": "提交补货申请", "description": "", "x": 10, "y": 20.5,
        "refs": [{"kind": "rule", "id": "rule:display_cap", "key": "display_cap", "name": "display_cap"}],
        "pending": False, "pending_note": None,
    }
    assert model["edges"][0] == {"id": "e1", "type": "seq", "from": "n1", "to": "n1", "label": "", "condition": ""}


@pytest.mark.parametrize("bad", [
    [],
    {"nodes": {"n1": {}}},
    {"nodes": ["n1"]},
    {"nodes": [{"id": "n1", "type": "step", "label": "补货", "x": "10", "y": 0}]},
    {"nodes": [{"id": "n1", "type": "step", "label": "补货", "x": True, "y": 0}]},
    {"nodes": [{"id": "n1", "type": "step", "label": "补货", "x": float("nan"), "y": 0}]},
    {"nodes": [{"id": "n1", "type": "step", "label": "补货", "y": 0}]},
    {"nodes": [{"id": 1, "type": "step", "label": "补货", "x": 0, "y": 0}]},
    {"nodes": [{"id": "n1", "type": "step", "label": "补货", "x": 0, "y": 0, "refs": [{"kind": "table", "key": "t"}]}]},
    {"nodes": [{"id": "n1", "type": "step", "label": "补货", "x": 0, "y": 0, "refs": "rule:display_cap"}]},
    {"nodes": [{"id": "n1", "type": "step", "label": "补货", "x": 0, "y": 0, "pending": "yes"}]},
    {"edges": [{"id": "e1", "type": "seq", "from": "n1", "to": ["n2"]}]},
    {"process": {"roles": "门店店长"}},
    {"process": {"roles": ["门店店长", 3]}},
    {"storage": "zip"},
    {"operation": {"kind": "operation"}},
    {"drawio_xml": 5},
])
def test_normalize_rejects_bad_shapes(bad):
    with pytest.raises(FlowchartError):
        flowchart.normalize(bad)


# ---- 输入防护 ----


@pytest.mark.parametrize("data", [
    '<?xml version="1.0"?><!DOCTYPE mxfile [<!ENTITY x "补货">]><mxfile><diagram name="&x;"/></mxfile>',
    '<!doctype mxfile><mxfile/>',
    b'<!ENTITY a "b"><mxfile/>',
])
def test_parse_rejects_doctype_and_entities(data):
    with pytest.raises(FlowchartError, match="DOCTYPE"):
        flowchart.parse(data)


@pytest.mark.parametrize("data", ["", "   ", b"", "<mxfile><diagram>", "<svg/>", b"\xff\xfe<mxfile/>", 42])
def test_parse_rejects_empty_and_invalid(data):
    with pytest.raises(FlowchartError):
        flowchart.parse(data)


def test_parse_rejects_oversize_input():
    big = "<mxfile>" + " " * flowchart.MAX_BYTES + "</mxfile>"
    with pytest.raises(FlowchartError, match="5 MiB"):
        flowchart.parse(big)
    with pytest.raises(FlowchartError, match="5 MiB"):
        flowchart.parse(big.encode("utf-8"))


def test_summary():
    assert flowchart.summary(_replenish_model()) == "9 个节点 · 9 条连线"
    assert flowchart.summary({}) == "0 个节点 · 0 条连线"


# ---- 工作台高级编辑交回的 XML（develop !547 的移植：drawio 编辑保留内核元数据） ----

# 图示包 ui/flowchart/drawioConvert.js 的 modelToDrawio 对一张合成流程图的输出，原样摘录：
# 标准编辑器里改了还没保存就切到高级编辑时，交给编辑器、保存时交回的就是这种 XML
_EDITOR_XML = """<mxfile host="onto-modeling" agent="onto-modeling-flowchart">
  <diagram name="合成流程">
    <mxGraphModel grid="1" gridSize="10" guides="1" connect="1" arrows="1" page="0">
      <root>
        <object id="0" label="" process_name="合成流程" process_description="第一行&#10;第二行" process_roles="[&quot;专员&quot;]" process_inputs="[]" process_exceptions="[]" process_handoffs="[]" operation="{&quot;kind&quot;:&quot;operation&quot;,&quot;id&quot;:&quot;operation:op-a&quot;,&quot;key&quot;:&quot;op-a&quot;,&quot;name&quot;:&quot;操作甲&quot;}">
          <mxCell />
        </object>
        <mxCell id="1" parent="0" />
        <object id="n1" label="开始" onto_type="start" description="" refs="[]" pending="0">
          <mxCell style="rounded=1;whiteSpace=wrap;html=1;shape=mxgraph.flowchart.terminator;fillColor=#EFF5FF;strokeColor=#2563EB;" vertex="1" parent="1">
            <mxGeometry x="48" y="21" width="104" height="38" as="geometry" />
          </mxCell>
        </object>
        <object id="n2" label="A&amp;lt;B" onto_type="step" description="说明一&#10;说明二" refs="[{&quot;kind&quot;:&quot;object&quot;,&quot;id&quot;:&quot;object:obj-a&quot;,&quot;key&quot;:&quot;obj-a&quot;,&quot;name&quot;:&quot;对象甲&quot;}]" pending="1" pending_note="待定">
          <mxCell style="rounded=1;whiteSpace=wrap;html=1;arcSize=20;strokeColor=#9BBEFB;dashed=1;" vertex="1" parent="1">
            <mxGeometry x="34" y="119" width="132" height="42" as="geometry" />
          </mxCell>
        </object>
        <object id="n3" label="结束" onto_type="end" description="" refs="[]" pending="0">
          <mxCell style="rounded=1;whiteSpace=wrap;html=1;shape=mxgraph.flowchart.terminator;fillColor=#F1F0FF;strokeColor=#4F46E5;" vertex="1" parent="1">
            <mxGeometry x="48" y="221" width="104" height="38" as="geometry" />
          </mxCell>
        </object>
        <object id="e1" label="" onto_type="seq" condition="">
          <mxCell style="endArrow=block;html=1;strokeColor=#7C8CA6;" edge="1" parent="1" source="n1" target="n2">
            <mxGeometry relative="1" as="geometry" />
          </mxCell>
        </object>
        <object id="e2" label="是" onto_type="cond" condition="条件一&#10;条件二">
          <mxCell style="endArrow=block;html=1;strokeColor=#F59E0B;" edge="1" parent="1" source="n2" target="n3">
            <mxGeometry relative="1" as="geometry" />
          </mxCell>
        </object>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""


def test_editor_xml_carries_unsaved_metadata_over_previous_version():
    """交回的 XML 带着内核属性：描述、引用、待定、条件、流程元数据以它为准，不退回上一版；多行文字的换行不丢。"""
    previous = flowchart.parse(_EDITOR_XML, key="k")
    previous["process"]["description"] = "上一版"
    previous["nodes"][1].update(description="上一版", refs=[], pending=False, pending_note=None)
    previous["edges"][1]["condition"] = "上一版"
    previous["operation"] = None

    model = flowchart.from_drawio_edit(_EDITOR_XML, previous)

    assert model["process"]["name"] == "合成流程"
    assert model["process"]["description"] == "第一行\n第二行"
    assert model["process"]["roles"] == ["专员"]
    assert model["operation"] == {"kind": "operation", "id": "operation:op-a", "key": "op-a", "name": "操作甲"}
    n2 = next(node for node in model["nodes"] if node["id"] == "n2")
    assert n2["label"] == "A<B"
    assert n2["description"] == "说明一\n说明二"
    assert n2["refs"] == [{"kind": "object", "id": "object:obj-a", "key": "obj-a", "name": "对象甲"}]
    assert (n2["pending"], n2["pending_note"]) == (True, "待定")
    assert [node["type"] for node in model["nodes"]] == ["start", "step", "end"]
    e2 = next(edge for edge in model["edges"] if edge["id"] == "e2")
    assert (e2["type"], e2["label"], e2["condition"]) == ("cond", "是", "条件一\n条件二")

    # 写回文件再读一遍，内容不变
    again = flowchart.parse(flowchart.render(model, previous=_EDITOR_XML), key="k")
    assert again["process"] == model["process"] and again["nodes"] == model["nodes"] and again["edges"] == model["edges"]


# ---- 文字标注（develop !607）----

ANNOTATED = """<mxfile><diagram name="示例"><mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="decision" value="是否通过" style="rhombus;" vertex="1" parent="1"><mxGeometry x="300" y="200" width="100" height="80" as="geometry"/></mxCell>
<mxCell id="left" value="处理甲" vertex="1" parent="1"><mxGeometry x="100" y="370" width="100" height="60" as="geometry"/></mxCell>
<mxCell id="right" value="处理乙" vertex="1" parent="1"><mxGeometry x="500" y="370" width="100" height="60" as="geometry"/></mxCell>
<mxCell id="yes" edge="1" parent="1" source="decision" target="left"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="150" y="300"/></Array></mxGeometry></mxCell>
<mxCell id="no" edge="1" parent="1" source="decision" target="right"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="550" y="300"/></Array></mxGeometry></mxCell>
<mxCell id="yes-text" value="通过" style="text;html=1;strokeColor=none;fillColor=none;" vertex="1" parent="1"><mxGeometry x="150" y="300" width="50" height="24" as="geometry"/></mxCell>
<mxCell id="no-text" value="未通过" style="text;html=1;strokeColor=none;fillColor=none;" vertex="1" parent="1"><mxGeometry x="550" y="300" width="50" height="24" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""


def _elements(xml: str) -> dict[str, ET.Element]:
    """id → 单元格的外层元素（裸 mxCell 或包着 mxCell 的 object）。"""
    root = ET.fromstring(xml).find("diagram/mxGraphModel/root")
    return {el.get("id"): el for el in root}


def _inner(el: ET.Element) -> ET.Element:
    return el if el.tag == "mxCell" else el.find("mxCell")


def _text_of(el: ET.Element) -> str:
    return el.get("value" if el.tag == "mxCell" else "label", "")


def test_text_annotations_attach_to_branch_edges_and_follow_relabel():
    """分支旁的文字标注归到连线当条件；保存不丢标注和折点；改连线文字时标注跟着改，连线本身不写文字。"""
    model = flowchart.parse(ANNOTATED)
    assert len(model["nodes"]) == 3
    assert [(e["id"], e["label"], e["type"], e["condition"]) for e in model["edges"]] == [
        ("yes", "通过", "cond", "通过"), ("no", "未通过", "cond", "未通过"),
    ]

    saved = flowchart.render(model, previous=ANNOTATED)
    cells = [_inner(el) for el in _elements(saved).values()]
    assert sum(c.get("vertex") == "1" for c in cells) == 5
    assert sum(c.get("edge") == "1" for c in cells) == 2
    assert len(_inner(_elements(saved)["yes"]).findall(".//mxPoint")) == 1
    assert [e["label"] for e in flowchart.parse(saved)["edges"]] == ["通过", "未通过"]

    renamed = copy.deepcopy(model)
    renamed["edges"][0]["label"] = "已通过"
    out = flowchart.render(renamed, previous=saved)
    els = _elements(out)
    assert _text_of(els["yes-text"]) == "已通过"
    assert els["yes-text"].get("data-flowchart-annotation-edge") == "yes"
    assert _text_of(els["yes"]) == ""
    assert flowchart.parse(out)["edges"][0]["label"] == "已通过"

    # 连线文字清空：标注一起去掉
    cleared = copy.deepcopy(model)
    cleared["edges"][1].update(label="", condition="")
    assert "no-text" not in _elements(flowchart.render(cleared, previous=saved))


def test_unattached_text_annotation_stays_a_plain_note():
    """离哪条分支都不近的文字不归属、不当节点，写回时原样保留（不必像 develop 那样退回 drawio 编辑器）。"""
    xml = ANNOTATED.replace('x="150" y="300" width="50"', 'x="20" y="20" width="50"')
    model = flowchart.parse(xml)
    assert len(model["nodes"]) == 3
    assert [e["label"] for e in model["edges"]] == ["", "未通过"]
    els = _elements(flowchart.render(model, previous=xml))
    assert _text_of(els["yes-text"]) == "通过"
    assert els["yes-text"].get("data-flowchart-annotation-edge") is None
