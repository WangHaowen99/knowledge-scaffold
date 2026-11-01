"""测试用的合成能力包：写清单、内容与校验和。"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.services.packs.repository import write_checksums


def make_pack(base: Path, name: str, version: str, *, files: dict[str, str] | None = None,
              kind: str = "capability", tenant: bool = False, checksums: bool = True, **extra) -> Path:
    """在 base/<name>-<version>/ 下生成一个包目录，返回目录。

    files：相对路径 → 内容；commands/agents/scripts 的名字会自动写进清单 contributes。
    """
    directory = Path(base) / f"{name}-{version}"
    directory.mkdir(parents=True, exist_ok=True)
    files = files if files is not None else {f"commands/{name}-run.md": "运行 $ARGUMENTS\n"}
    for rel, content in files.items():
        path = directory / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    contributes = {
        "commands": sorted(Path(rel).stem for rel in files if rel.startswith("commands/")),
        "agents": sorted(Path(rel).stem for rel in files if rel.startswith("agents/")),
        "scripts": sorted(Path(rel).name for rel in files if rel.startswith("scripts/")),
    }
    contributes.update(extra.pop("contributes", {}))
    data = {
        "name": name, "version": version, "kind": kind, "label": f"{name} 包",
        "kernel_api": ">=1.0 <2.0", "engines": ["claude", "jiuwen"],
        "publisher": {"kind": "tenant" if tenant else "official", "label": "租户 A" if tenant else "平台"},
        "contributes": contributes,
        **extra,
    }
    (directory / "manifest.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if checksums:
        write_checksums(directory)
    return directory
