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


def make_assets(base: Path, *, version: str = "0.1.0", commands: tuple[str, ...] = ("ontology",),
                flow: str | None = None) -> Path:
    """最小的镜像资产目录：带清单的工具箱（core 包）+ 工作区规则与说明。"""
    assets = Path(base) / "assets"
    toolkit = assets / "workspace-toolkit"
    (toolkit / "commands").mkdir(parents=True, exist_ok=True)
    (toolkit / "scripts").mkdir(parents=True, exist_ok=True)
    (toolkit / "templates").mkdir(parents=True, exist_ok=True)
    for name in commands:
        (toolkit / "commands" / f"{name}.md").write_text(f"命令 {name} $ARGUMENTS\n", encoding="utf-8")
    (toolkit / "scripts" / "state_helper.py").write_text("print('未开始')\n", encoding="utf-8")
    (toolkit / "templates" / "task-gitignore.template").write_text(
        ".claude\nCLAUDE.md\nREADME.md\n.onto-web.json\n.ontology-state/\n.onto/run/\ntmp/\n", encoding="utf-8")
    contributes = {"commands": list(commands), "scripts": ["state_helper.py"],
                   "rules": ["workspace-rules.md"], "readme": "workspace-readme.md"}
    if flow is not None:
        (toolkit / "flow.yaml").write_text(flow, encoding="utf-8")
        contributes["flow"] = "flow.yaml"
    data = {"name": "core", "version": version, "kind": "capability", "required": True,
            "kernel_api": ">=1.0 <2.0", "publisher": {"kind": "official", "label": "平台"},
            "contributes": contributes}
    (toolkit / "manifest.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (assets / "workspace-rules.md").write_text("# 工作区规则\n", encoding="utf-8")
    (assets / "workspace-readme.md").write_text("# 工作区说明\n", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True, exist_ok=True)
    return assets


def assemble_real(data_dir: Path) -> Path:
    """按仓库里真实的资产装配一份目录（core + 内核守卫），返回装配目录；给要跑工作区脚本、
    守卫 hooks 的测试用（真实项目的 `.claude` 就指向它）。"""
    from app import config
    from app.services.packs.runtime import PackRuntime

    runtime = PackRuntime(Path(data_dir), Path(config.ASSETS_DIR))
    _, target = runtime.build(runtime.default_lock())
    return target


def link_assembled(workspace: Path, data_dir: Path) -> Path:
    """工作区 `.claude` 指向真实装配目录（与 create_project 挂载的同构）。"""
    target = assemble_real(data_dir)
    (Path(workspace) / ".claude").symlink_to(target)
    return target
