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


# 合成节点包（详设 6.3、6.9；kind: node）：一个挂在质量检查之后的 agent 关卡节点，命令、探针都在包里
NODE_PACK_FLOW = {
    "requires_flow": "core>=0.1",
    "probes": {"naming_report": {"files": ["reports"], "glob": "naming-*.md"}},
    "nodes": [{
        "id": "naming.check", "title": "命名一致性检查", "kind": "agent", "placement": "step",
        "section": "required", "after": "core.validate", "done": ["naming_report"],
        "stale_on": ["object"], "produces": [{"path": "reports/naming-*.md", "kind": "report"}],
        "entry": {"command": "/naming-check"},
        "actions": [{"label": "运行命名检查", "command": "/naming-check"}],
    }],
}


def make_node_pack(base: Path, name: str = "naming", version: str = "0.1.0", *, flow: dict | None = None,
                   files: dict[str, str] | None = None, **extra) -> Path:
    """合成节点包：清单 kind: node，流程声明写在 flow.yaml。files 默认带入口命令。"""
    flow = flow if flow is not None else NODE_PACK_FLOW
    files = dict(files if files is not None else {f"commands/{name}-check.md": "按命名约定逐条检查对象名，写报告 $ARGUMENTS\n"})
    files["flow.yaml"] = yaml.safe_dump(flow, allow_unicode=True, sort_keys=False)
    contributes = {"flow": "flow.yaml", "nodes": [node["id"] for node in flow.get("nodes") or []]}
    contributes.update(extra.pop("contributes", {}))
    return make_pack(base, name, version, files=files, kind="node", contributes=contributes, **extra)


# 合成的按事件触发、限定范围的能力包（ADR-0085、ADR-0091）：已经建了一部分又上传新资料时触发三步——圈定范围 →
# 范围内修改 → 核对，核对没做完挡发布；没触发时节点不出现。范围确认后基线里不在 editable 的条目助手只读，
# 触发后、范围确认前已有条目全部只读。内核的触发、抢先推荐、范围限定用它测，不依赖哪个真实的包。
ROUND_PACK = "intake"
ROUND_SCOPE_FILE = "intake/scope.json"
ROUND_STEPS = ("intake.scope", "intake.apply", "intake.check")
_ROUND_WRITES = ["ontology/object/*", "ontology/operation/*", "ontology/logic/*", "ontology/rule/*", "ontology/metric/*",
                 "ontology/action/*", "ontology/scripts/*", "ontology/testcase/*", "ontology/data-mock/*",
                 "ontology/knowledge/*"]


def _round_node(step: str, title: str, **fields) -> dict:
    return {"id": f"intake.{step}", "title": title, "kind": "agent", "placement": "spine", "section": "required",
            "autobuild": False, "visible_if": "activated", **fields, "entry": {"command": f"/intake-{step}"},
            "actions": [{"label": title, "command": f"/intake-{step}"}]}


ROUND_PACK_FLOW = {
    "activation": {"event": "materials_added", "requires": ["intake_has_entries"], "session": ROUND_SCOPE_FILE},
    "confine": {"scope_file": ROUND_SCOPE_FILE, "lock_on_activation": True},
    "probes": {
        "intake_has_entries": {"files": ["ontology/object", "ontology/operation"], "glob": "*.md", "recursive": True},
        "intake_session": {"kernel": "session_open", "session": ROUND_SCOPE_FILE},
        "intake_applied": {"files": ["intake"], "glob": "applied.json"},
        "intake_checked": {"files": ["intake"], "glob": "check.md"},
        "intake_pending": {"pending": ["intake"], "glob": "check.md"},
    },
    "nodes": [
        _round_node("scope", "圈定范围", preempt="上传了新资料，先圈定这次要改的范围", display_after=["core.operation"],
                    done=["intake_session"], produces=[ROUND_SCOPE_FILE, "intake/scope.md"]),
        _round_node("apply", "范围内修改", preempt=True, after="intake.scope", display_after=["intake.scope"],
                    done=["intake_applied", "intake_session"], produces=["intake/applied.json"], writes=_ROUND_WRITES),
        _round_node("check", "核对", preempt=True, after="intake.apply", display_after=["intake.apply"],
                    done=["intake_checked", "!intake_pending", "intake_session"],
                    stale_on=["object", "operation", "rule", "metric"], blocks=["core.publish"],
                    produces=["intake/check.md", "intake/check.json"], writes=_ROUND_WRITES),
    ],
    "commands": {f"intake-{step}": {"node": f"intake.{step}", "requires": ["对象已提取"], "lock_waits": ["confirm"]}
                 for step in ("scope", "apply", "check")},
}
# 用户要结束这一轮时提示助手走圈定范围的命令
ROUND_PACK_PROMPT = {"id": "intake-close-round", "match": ["结束本轮"],
                     "text": "用户要结束这一轮。运行 /intake-scope 把范围改成已结束，结束后范围外的条目恢复可写。"}


def make_round_pack(base: Path, version: str = "0.1.0", *, flow: dict | None = None, checksums: bool = True) -> Path:
    """合成的按事件触发、限定范围的包（ROUND_PACK_FLOW）：三个节点各带入口命令，清单带一条提示规则。"""
    flow = flow if flow is not None else ROUND_PACK_FLOW
    files = {f"commands/intake-{step}.md": f"按合成范围{title} $ARGUMENTS\n"
             for step, title in (("scope", "圈定"), ("apply", "修改"), ("check", "核对"))}
    files["flow.yaml"] = yaml.safe_dump(flow, allow_unicode=True, sort_keys=False)
    return make_pack(base, ROUND_PACK, version, files=files, checksums=checksums, category="capability",
                     description="合成：上传新资料后圈定范围、范围内修改、核对。", default_on=False,
                     depends={"core": ">=0.5"}, changelog="合成版本。",
                     contributes={"flow": "flow.yaml", "nodes": [node["id"] for node in flow.get("nodes") or []],
                                  "prompts": [ROUND_PACK_PROMPT]})


def install_round_pack(manager, base: Path, version: str = "0.1.0") -> None:
    """合成的触发包入库、stable 指向它，项目里就能按名字启用。"""
    source = make_round_pack(Path(base) / "round-pack-src", version)
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel(ROUND_PACK, "stable", version, by="admin")
