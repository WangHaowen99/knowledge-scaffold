"""测试共用：补齐发布前必经的建模阶段（资料分析、质量检查的合成产物），保留项目已有的对象与操作。"""
from pathlib import Path

from app.services.workspace import _git


def complete_publish_stages(root: Path, commit: bool = True) -> None:
    (root / "research").mkdir(exist_ok=True)
    (root / "research" / "report.md").write_text("# Synthetic research\n", encoding="utf-8")
    (root / "reports").mkdir(exist_ok=True)
    (root / "reports" / "validation.md").write_text("# Synthetic validation\n", encoding="utf-8")
    objects = root / "ontology" / "object"
    objects.mkdir(parents=True, exist_ok=True)
    if not any(path.name != "index.md" for path in objects.glob("*.md")):
        (objects / "object_demo.md").write_text("# Synthetic object\n", encoding="utf-8")
    operations = root / "ontology" / "operation"
    if not any(path.name != "index.md" for path in operations.glob("*.md")) and not any(
            path.name != "index.md" for path in (root / "ontology" / "logic").glob("*.md")):
        operations.mkdir(parents=True, exist_ok=True)
        (operations / "operation_demo.md").write_text("# Synthetic operation\n", encoding="utf-8")
    if commit:
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "test: synthetic publish stages", "--allow-empty")
