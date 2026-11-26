"""从原 modeling 服务一键迁移建模中间数据到 onto-modeling。

输入是 modeling 数据目录的一份拷贝（含 modeling.db 与 artifacts/），
对每个 modeling 项目在 onto-modeling 里创建对应工作区：

- 项目元数据：名称 / 所有者 / 领域与背景说明（卡片展示）
- 资料输入：materials 的解析文本 → resource/others/<原名>.md；
  Excel 的表结构与样例 → resource/excel/<原名>.md，并打 schema 用途标签
  （modeling 未保留原始二进制文件，迁移的是解析产物，文首有说明）
- 知识包：artifacts/<pid>/package 发布布局 → 工作区布局
  （objects/→ontology/object/ 等，剥发布 frontmatter，文件名保留英文 key
  以免 [[互链]] 断裂；index.md / manifest / mappings 跳过）

幂等：工作区 meta 记录 migrated_from=<modeling项目id>，重复运行自动跳过。

不迁移内容来自 Onto 自身的两类项目，迁回会生成重复的同名项目：Onto 数据评测导入的
知识包项目，以及 Onto 项目在 modeling 侧的关联项目（见 not_migrated_reason）。

用法（容器内）：
    python3 scripts/migrate_from_modeling.py --source /tmp/modeling-data [--owner 用户名]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.services.workspace import WorkspaceManager, _git  # noqa: E402

# 发布布局 → 工作区布局
PACKAGE_DIR_MAP = {
    "objects": "ontology/object",
    "operations": "ontology/operation",
    "rules": "ontology/rule",
    "metrics": "ontology/metric",
    "diagrams": "ontology/diagrams",
}
SKIP_PACKAGE_DIRS = {"mappings"}
# Onto 数据评测导入 modeling 时的包名：onto-eval-<Onto 项目 id>，重名时追加 -<时间戳>；
# Onto 项目 id 为 <slug>-<8 位十六进制>
EVALUATION_PROJECT_NAME = re.compile(r"onto-eval-[a-z0-9-]+-[0-9a-f]{8}(?:-[0-9]{10})?")
# Onto 为自己的项目在 modeling 侧建关联项目时写入的 external_id 前缀
ONTO_PROJECT_EXTERNAL_ID_PREFIX = "onto:"


def not_migrated_reason(project: dict) -> str:
    """内容来自 Onto 自身、迁回会重复的 modeling 项目返回原因，其余返回空串。

    评测项目要同时满足 kind=package 与评测包名格式：用户导入的知识包同为 package，
    用户建模项目可能恰好以 onto-eval 开头。旧版 modeling 库没有 kind、external_id 列时按缺失处理。
    """
    if str(project.get("external_id") or "").startswith(ONTO_PROJECT_EXTERNAL_ID_PREFIX):
        return "Onto 项目在 modeling 侧的关联项目，无需迁回"
    if project.get("kind") in (None, "package") and EVALUATION_PROJECT_NAME.fullmatch(project.get("name") or ""):
        return "Onto 数据评测导入的项目，无需迁移"
    return ""


def read_projects(db: sqlite3.Connection) -> list[dict]:
    """按列名读取 modeling 项目；kind、external_id 在旧版库中不存在。"""
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    return [dict(row) for row in cursor.execute("SELECT * FROM projects")]


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    return parts[2].lstrip("\n") if len(parts) >= 3 else text


def migrate_package(package_dir: Path, root: Path) -> int:
    copied = 0
    for entry in sorted(package_dir.rglob("*")):
        if not entry.is_file():
            continue
        rel = entry.relative_to(package_dir)
        top = rel.parts[0] if len(rel.parts) > 1 else ""
        if entry.name in ("manifest.yaml",) or entry.name.lower() == "index.md":
            continue
        if top in SKIP_PACKAGE_DIRS:
            continue
        if top == "scripts":
            if entry.suffix != ".py":
                continue  # 伴生 .md 描述符不搬：迁移后 ensure_package_layout 按 .py 重新生成
            dest = root / "ontology/scripts" / entry.name
        elif top in PACKAGE_DIR_MAP:
            dest = root / PACKAGE_DIR_MAP[top] / entry.name
        else:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if entry.suffix == ".md":
            dest.write_text(
                strip_frontmatter(entry.read_text(encoding="utf-8")), encoding="utf-8"
            )
        else:
            dest.write_bytes(entry.read_bytes())
        copied += 1
    return copied


def render_excel_tables(tables_json: str) -> str:
    try:
        tables = json.loads(tables_json or "[]")
    except json.JSONDecodeError:
        return ""
    if not isinstance(tables, list):
        return ""
    lines = []
    # modeling 解析产物里存在空表、空字段占位（null），跳过而不是中断整批迁移
    for table in tables:
        if not isinstance(table, dict):
            continue
        lines.append(f"## {table.get('name') or '未命名表'}\n")
        lines.append("| 字段 | 推断类型 | 样例值 |")
        lines.append("| --- | --- | --- |")
        for field in table.get("fields") or []:
            if not isinstance(field, dict):
                continue
            samples = "、".join(str(v) for v in (field.get("sampleValues") or [])[:3])
            lines.append(
                f"| {field.get('name') or ''} | {field.get('inferredType') or ''} | {samples} |"
            )
        lines.append("")
    return "\n".join(lines)


def migrate_documents(db: sqlite3.Connection, project_id: str, mgr: WorkspaceManager, new_id: str, root: Path) -> int:
    rows = db.execute(
        "SELECT filename, kind, text_content, tables_json FROM documents "
        "WHERE project_id=? AND lifecycle_status='active'",
        (project_id,),
    ).fetchall()
    count = 0
    for filename, kind, text, tables_json in rows:
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename or "资料")
        note = (
            f"> 从 modeling 迁移的解析文本（原始文件 `{filename}` 未随 modeling 保留）。\n\n"
        )
        if kind == "excel":
            body = render_excel_tables(tables_json)
            if not body:
                continue
            subdir, purpose = "excel", "schema"
        else:
            if not text:
                continue
            body = text
            subdir, purpose = "others", "auto"
        dest = root / "resource" / subdir / f"{stem}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(note + body, encoding="utf-8")
        rel = f"resource/{subdir}/{stem}.md"
        if purpose != "auto":
            directives = mgr.resource_directives(new_id)
            directives[rel] = {"purpose": purpose, "reading_mode": "on_demand"}
            mgr._write_resource_directives(root, directives)
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="从 modeling 数据目录迁移到 onto-modeling")
    parser.add_argument("--source", required=True, help="modeling 数据目录拷贝（含 modeling.db 与 artifacts/）")
    parser.add_argument("--owner", default="", help="覆盖迁移项目的所有者（默认沿用 modeling 记录）")
    parser.add_argument("--project", default="", help="只迁移指定的 modeling 项目 id")
    parser.add_argument("--summary-file", default="", help="成功后写入迁移计数，供部署器核对")
    args = parser.parse_args()

    source = Path(args.source)
    db_path = source / "modeling.db"
    if not db_path.is_file():
        print(f"错误：{db_path} 不存在", file=sys.stderr)
        return 1

    mgr = WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)
    migrated_ids = {
        meta.get("migrated_from")
        for meta in mgr.list_projects()
        if meta.get("migrated_from")
    }

    db = sqlite3.connect(str(db_path))
    projects = read_projects(db)

    done = skipped = excluded = 0
    failed: list[str] = []
    for project in projects:
        pid, owner, name = project.get("id"), project.get("owner"), project.get("name")
        if args.project and pid != args.project:
            continue
        reason = not_migrated_reason(project)
        if reason:
            print(f"跳过 {pid}（{name}）：{reason}")
            skipped += 1
            excluded += 1
            continue
        if pid in migrated_ids:
            print(f"跳过 {pid}（{name}）：已迁移")
            skipped += 1
            continue
        bg = "；".join(x for x in [project.get("domain"), project.get("background")] if x)
        meta = mgr.create_project(
            name, owner=args.owner or owner or "", background=bg
        )
        root = mgr.project_root(meta["id"])
        try:
            docs = migrate_documents(db, pid, mgr, meta["id"], root)
            package_dir = source / "artifacts" / pid / "package"
            files = migrate_package(package_dir, root) if package_dir.is_dir() else 0

            mgr.ensure_package_layout(meta["id"])  # 生成各目录 index 清单
            _git(root, "add", "-A")
            _git(root, "commit", "-m", "import: 从 modeling 迁移项目数据", "--allow-empty")
            # 迁移完整后才打标记：中途失败的项目重跑时会重新迁移，而不是被当成已迁移跳过
            mgr._update_meta(meta["id"], migrated_from=pid)
        except Exception as exc:  # noqa: BLE001 单个项目失败不阻断其余项目
            mgr.delete_project(meta["id"])
            print(f"失败 {pid}（{name}）：{type(exc).__name__}: {exc}", file=sys.stderr)
            failed.append(pid)
            continue
        print(f"已迁移 {pid} → {meta['id']}（{name}）：资料 {docs} 份，知识包文件 {files} 个")
        done += 1

    print(f"\n完成：迁移 {done} 个项目，跳过 {skipped} 个（其中评测和 Onto 关联项目 {excluded} 个不迁移），"
          f"失败 {len(failed)} 个。")
    if done:
        print("提示：迁移文档保留 modeling 原结构，建议在各项目里跑一次 /ontology-validate，")
        print("     由助手按本工具集的模板规范化章节。")
    if failed:
        print(f"失败项目可修复后单独重跑：--project <id>，失败列表：{', '.join(failed)}", file=sys.stderr)
        return 1
    if args.summary_file:
        summary = Path(args.summary_file)
        summary.parent.mkdir(parents=True, exist_ok=True)
        temporary = summary.with_name(summary.name + ".tmp")
        # skipped 含 excluded：部署器按 migrated + skipped == 源库项目数核对
        temporary.write_text(json.dumps({"schemaVersion": "core-project-migration/v1",
                                         "sourceProjects": done + skipped,
                                         "migrated": done, "skipped": skipped,
                                         "excluded": excluded}, sort_keys=True) + "\n",
                             encoding="utf-8")
        temporary.replace(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
