#!/usr/bin/env python3
"""
示例数据种子。注入一份"外卖订单"迷你领域，供新用户跑通完整流程。

用法：
    python3 .claude/scripts/seed_example.py [root]
    python3 .claude/scripts/seed_example.py --clean [root]

root 为空时直接写入仓库根下 research/ 与 ontology/；非空时放到 {root}research/ 等位置。
"""

import os
import subprocess
import sys
import shutil
from pathlib import Path

MANIFEST_FILE = '.ontology-state/seed-example.manifest'

RESEARCH_MD = """# 外卖订单领域资料分析（示例）

## 业务背景

外卖订单平台连接**顾客**、**商家**与**骑手**。顾客浏览商家菜单下单、支付后，订单进入商家厨房准备，完成后由骑手取餐送达。

## 关键对象

- **顾客**：下单主体，至少包含姓名、手机号、配送地址。
- **商家**：接单方，有菜单、营业时间、地址。
- **订单**：核心交易实体，包含商品明细、金额、状态、配送地址。
- **骑手**：执行配送的个人，含姓名、手机号、当前任务。

## 核心流程

- **下单流程**：顾客选商品 → 确认地址 → 生成订单 → 支付 → 商家接单。
- **配送流程**：商家出餐 → 系统派单 → 骑手取餐 → 送达 → 顾客确认。

## 关键规则

- 订单金额小于 20 元时不可配送（仅支持到店自取）。
- 若 30 分钟内商家未接单，订单自动取消并退款。
- 骑手接单后，若 10 分钟内未取餐视为超时，系统重新派单。

## 边界与异常

- 顾客支付失败：订单保持"未支付"状态，15 分钟后自动取消。
- 骑手找不到顾客：电话沟通 3 次不接触发异常配送流程。
- 商家缺货：商家可主动拒单，系统退款并通知顾客。
"""

DOWNLOAD_DRAWIO = """<mxfile host="app.diagrams.net">
  <diagram name="下单流程" id="seed-order">
    <mxGraphModel dx="800" dy="600" grid="1" gridSize="10" pageWidth="850" pageHeight="1100">
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        <mxCell id="start" value="开始" style="ellipse;shape=mxgraph.flowchart.terminator;fillColor=#dae8fc;strokeColor=#6c8ebf;" vertex="1" parent="1">
          <mxGeometry x="360" y="40" width="100" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="browse" value="浏览菜单" style="rounded=1;whiteSpace=wrap;fillColor=#f5f5f5;" vertex="1" parent="1">
          <mxGeometry x="360" y="110" width="100" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="select" value="选择商品加入购物车" style="rounded=1;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="340" y="180" width="140" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="confirm_addr" value="确认配送地址" style="rounded=1;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="350" y="250" width="120" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="amount_ok" value="金额>=20元?" style="rhombus;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="350" y="320" width="120" height="60" as="geometry"/>
        </mxCell>
        <mxCell id="create_order" value="生成订单" style="shape=card;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="360" y="420" width="100" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="pay" value="支付" style="shape=card;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="360" y="490" width="100" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="merchant_accept" value="商家接单?" style="rhombus;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="350" y="560" width="120" height="60" as="geometry"/>
        </mxCell>
        <mxCell id="done_ok" value="下单完成" style="ellipse;shape=mxgraph.flowchart.terminator;fillColor=#d5e8d4;strokeColor=#82b366;" vertex="1" parent="1">
          <mxGeometry x="350" y="660" width="120" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="reject" value="订单取消退款" style="ellipse;shape=mxgraph.flowchart.terminator;fillColor=#f8cecc;strokeColor=#b85450;" vertex="1" parent="1">
          <mxGeometry x="560" y="560" width="140" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="selftake" value="仅支持到店自取" style="ellipse;shape=mxgraph.flowchart.terminator;fillColor=#fff2cc;strokeColor=#d6b656;" vertex="1" parent="1">
          <mxGeometry x="550" y="330" width="140" height="40" as="geometry"/>
        </mxCell>

        <mxCell id="e1" style="endArrow=classic" edge="1" parent="1" source="start" target="browse"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e2" style="endArrow=classic" edge="1" parent="1" source="browse" target="select"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e3" style="endArrow=classic" edge="1" parent="1" source="select" target="confirm_addr"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e4" style="endArrow=classic" edge="1" parent="1" source="confirm_addr" target="amount_ok"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e5" value="是" style="endArrow=classic" edge="1" parent="1" source="amount_ok" target="create_order"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e6" value="否" style="endArrow=classic" edge="1" parent="1" source="amount_ok" target="selftake"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e7" style="endArrow=classic" edge="1" parent="1" source="create_order" target="pay"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e8" style="endArrow=classic" edge="1" parent="1" source="pay" target="merchant_accept"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e9" value="是" style="endArrow=classic" edge="1" parent="1" source="merchant_accept" target="done_ok"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e10" value="否" style="endArrow=classic" edge="1" parent="1" source="merchant_accept" target="reject"><mxGeometry relative="1" as="geometry"/></mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""

DELIVER_DRAWIO = """<mxfile host="app.diagrams.net">
  <diagram name="配送流程" id="seed-deliver">
    <mxGraphModel dx="800" dy="600" grid="1" gridSize="10" pageWidth="850" pageHeight="1100">
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        <mxCell id="start" value="开始" style="ellipse;shape=mxgraph.flowchart.terminator;fillColor=#dae8fc;" vertex="1" parent="1">
          <mxGeometry x="360" y="40" width="100" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="prepare" value="商家出餐" style="rounded=1;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="360" y="110" width="100" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="dispatch" value="系统派单" style="shape=card;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="360" y="180" width="100" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="pickup_ok" value="骑手10分钟内取餐?" style="rhombus;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="320" y="250" width="180" height="60" as="geometry"/>
        </mxCell>
        <mxCell id="redispatch" value="重新派单" style="shape=card;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="580" y="260" width="100" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="delivering" value="骑手配送中" style="rounded=1;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="350" y="340" width="120" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="arrive_ok" value="顾客确认收货?" style="rhombus;whiteSpace=wrap;" vertex="1" parent="1">
          <mxGeometry x="330" y="410" width="160" height="60" as="geometry"/>
        </mxCell>
        <mxCell id="done" value="配送完成" style="ellipse;shape=mxgraph.flowchart.terminator;fillColor=#d5e8d4;" vertex="1" parent="1">
          <mxGeometry x="350" y="510" width="120" height="40" as="geometry"/>
        </mxCell>
        <mxCell id="exception" value="异常配送流程" style="ellipse;shape=mxgraph.flowchart.terminator;fillColor=#f8cecc;" vertex="1" parent="1">
          <mxGeometry x="560" y="420" width="140" height="40" as="geometry"/>
        </mxCell>

        <mxCell id="e1" style="endArrow=classic" edge="1" parent="1" source="start" target="prepare"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e2" style="endArrow=classic" edge="1" parent="1" source="prepare" target="dispatch"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e3" style="endArrow=classic" edge="1" parent="1" source="dispatch" target="pickup_ok"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e4" value="是" style="endArrow=classic" edge="1" parent="1" source="pickup_ok" target="delivering"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e5" value="否" style="endArrow=classic" edge="1" parent="1" source="pickup_ok" target="redispatch"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e6" style="endArrow=classic" edge="1" parent="1" source="redispatch" target="dispatch"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e7" style="endArrow=classic" edge="1" parent="1" source="delivering" target="arrive_ok"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e8" value="是" style="endArrow=classic" edge="1" parent="1" source="arrive_ok" target="done"><mxGeometry relative="1" as="geometry"/></mxCell>
        <mxCell id="e9" value="否" style="endArrow=classic" edge="1" parent="1" source="arrive_ok" target="exception"><mxGeometry relative="1" as="geometry"/></mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""

FILES = [
    ('research/20260417-外卖订单.md', RESEARCH_MD),
    ('ontology/diagrams/下单流程.drawio', DOWNLOAD_DRAWIO),
    ('ontology/diagrams/配送流程.drawio', DELIVER_DRAWIO),
]


def _resolve(root: str, rel: str) -> Path:
    return Path(root) / rel if root else Path(rel)


def _manifest_path() -> Path:
    return Path(MANIFEST_FILE)


def seed(root: str) -> int:
    written = []
    for rel, content in FILES:
        target = _resolve(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            print(f'跳过已存在：{target}')
            continue
        target.write_text(content, encoding='utf-8')
        written.append(str(target))
        print(f'写入：{target}')

    # 生成 diagram summary
    diagrams_dir = _resolve(root, 'ontology/diagrams')
    if diagrams_dir.exists():
        summary_dir = _resolve(root, 'ontology/diagrams-summary')
        summary_dir.mkdir(parents=True, exist_ok=True)
        script_dir = Path(__file__).resolve().parent
        sys.path.append(str(script_dir))
        try:
            from summarize_diagram import summarize  # type: ignore
            legend = Path('resource/diagrams/图示说明定义.drawio')
            for f in diagrams_dir.glob('*.drawio'):
                summary_path = summary_dir / (f.stem + '.md')
                if summary_path.exists():
                    continue
                if legend.exists():
                    text = summarize(str(f), str(legend))
                else:
                    text = f"# {f.stem} — 流程摘要\n\n（缺少图例定义，跳过摘要生成）\n"
                summary_path.write_text(text, encoding='utf-8')
                written.append(str(summary_path))
                print(f'摘要：{summary_path}')
        except Exception as exc:
            print(f'摘要生成失败（可忽略）：{exc}', file=sys.stderr)

    manifest = _manifest_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    existing = set(manifest.read_text(encoding='utf-8').splitlines()) if manifest.exists() else set()
    manifest.write_text('\n'.join(sorted(existing.union(written))), encoding='utf-8')
    print(f'清单已更新：{manifest}')
    return 0


def _git_dirty_paths(paths: list) -> list:
    """Return the subset of paths that have uncommitted changes (staged or working-tree).

    A clean path — i.e. identical to the last committed version — is safe to delete.
    A path that is modified, newly staged, or untracked-but-mentioned-in-manifest is
    considered dirty, and we must not silently discard the user's edits.
    """
    if not paths:
        return []
    try:
        result = subprocess.run(
            ['git', '-c', 'core.quotePath=false', 'status', '--porcelain', '--'] + paths,
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return []  # git not available — fall back to permissive behaviour
    if result.returncode != 0:
        return []
    dirty = set()
    for line in result.stdout.splitlines():
        # Porcelain format: "XY <path>"; XY is two status letters (may include space).
        if len(line) < 4:
            continue
        path = line[3:].strip()
        # Handle rename form "XY old -> new"
        if ' -> ' in path:
            path = path.split(' -> ', 1)[1]
        dirty.add(path)
    return [p for p in paths if p in dirty]


def clean(root: str) -> int:
    manifest = _manifest_path()
    if not manifest.exists():
        print('未发现示例清单，无内容可清理。')
        return 0
    paths = [p for p in manifest.read_text(encoding='utf-8').splitlines() if p.strip()]

    dirty = _git_dirty_paths(paths)
    if dirty:
        print('⚠️ 以下示例文件自种入后已被修改（未提交更改会随删除丢失）：', file=sys.stderr)
        for p in dirty:
            print(f'  - {p}', file=sys.stderr)
        print('', file=sys.stderr)
        print('请先处理这些修改（提交、撤销或移动到其他位置），再运行 --clean。', file=sys.stderr)
        print('如确认要丢弃这些修改，重新运行：`--clean --force`。', file=sys.stderr)
        if '--force' not in sys.argv:
            return 1

    for p in paths:
        target = Path(p)
        if target.exists():
            target.unlink()
            print(f'删除：{target}')
    manifest.unlink()
    print('示例数据已清理。')
    return 0


def main():
    args = [a for a in sys.argv[1:] if a != '--force']
    if args and args[0] == '--clean':
        root = args[1] if len(args) > 1 else ''
        return clean(root)
    root = args[0] if args else ''
    return seed(root)


if __name__ == '__main__':
    sys.exit(main())
