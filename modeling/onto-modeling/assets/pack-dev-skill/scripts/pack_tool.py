#!/usr/bin/env python3
"""插件包工具：init 建骨架，check 离线检查，build 检查通过后打 zip。只依赖 Python 3.9+ 和 PyYAML。

scripts/checker/ 是平台查上传包的同一份代码，platform.json 是下载 skill 时的平台快照。
快照里没有别的用户的私有包，这部分结果离线不知道，单独列成「上传时检查」，不算通过。
"""

from __future__ import annotations

import argparse
import json
import platform as host
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True  # 不往 skill 目录里留 __pycache__
SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))  # python3 -I 运行时脚本目录不在 sys.path 里
MARKS = {"pass": "通过", "warn": "警告", "fail": "失败"}


def _require_runtime() -> None:
    if sys.version_info < (3, 9):
        sys.exit(f"pack_tool 需要 Python 3.9 或更新版本，当前是 {sys.version.split()[0]}")
    try:
        import yaml  # noqa: F401
    except ImportError:
        sys.exit("缺少 PyYAML：运行 python3 -m pip install pyyaml 后重试")


def _platform() -> dict:
    return json.loads((SKILL / "platform.json").read_text(encoding="utf-8"))


class _PackView:
    """快照里一个包的声明：检查器只读命令、MCP 工具、指标和 core 的探针。"""

    contributes = {"flow": "flow.yaml"}

    def __init__(self, entry: dict):
        self.entry = entry

    def items(self, kind: str) -> list[str]:
        return list(self.entry.get(kind) or [])

    def declared(self, kind: str) -> list[dict]:
        return [{"name": name} for name in self.entry.get(kind) or []]


class _Snapshot:
    """用平台快照当包仓库，方法和服务器上的 PackRepository 同名（检查器用到的那几个）。"""

    def __init__(self, platform: dict, scratch: Path):
        import yaml

        self.packs = platform["packs"]
        self.scratch = scratch
        probes = {name: {} for name in platform["core"]["probes"]}
        (scratch / "flow.yaml").write_text(yaml.safe_dump({"probes": probes}), encoding="utf-8")

    def names(self, *, private: bool = False) -> list[str]:
        return sorted(self.packs)

    def versions(self, name: str) -> list[str]:
        return list((self.packs.get(name) or {}).get("versions") or [])

    def latest(self, name: str, channel: str = "stable") -> str | None:
        return (self.packs.get(name) or {}).get("stable")

    def has(self, name: str, version: str) -> bool:
        return version in self.versions(name)

    def version_dir(self, name: str, version: str) -> Path:
        return self.scratch

    def manifest(self, name: str, version: str) -> _PackView:
        return _PackView(self.packs.get(name) or {})


def _defer_private_depends(items: list[dict], manifest, repository: _Snapshot) -> list[dict]:
    """依赖快照里没有的包：可能是开发者自己的私有包，离线判不了，从兼容项挪到上传时检查。"""
    from checker.services.packs import checks

    unknown = [dep for dep in manifest.depends if not repository.versions(dep)]
    if not unknown:
        return []
    compat = next(item for item in items if item["id"] == "compat")
    kept = [part for part in compat["detail"].split("；")
            if part and not any(part.startswith(f"依赖 {dep} ") for dep in unknown)]
    compat.update(result="fail" if kept else "pass", detail="；".join(kept))
    return [checks.item("depends_private", "依赖的包在平台上有满足的版本", "upload",
                        f"{dep} 不在公开包里，只有它是开发者自己装过的包才能通过") for dep in unknown]


def _upload_time_items(directory: Path, manifest, platform: dict) -> list[dict]:
    from checker.services.packs import checks
    from checker.services.packs.repository import pack_files

    items = [
        checks.item("private_name", "包名没被别的用户占用", "upload", "别的用户的私有包名不在快照里"),
        checks.item("same_version", "这个版本没有装过", "upload", f"{manifest.name} {manifest.version} 装过的话要升版本号"),
    ]
    scripts = [rel for rel in pack_files(directory) if rel.startswith("scripts/")]
    local = ".".join(host.python_version_tuple()[:2])
    if local != platform["python"] and any(rel.endswith(".py") for rel in scripts):
        items.append(checks.item("python_syntax", "Python 脚本语法", "upload",
                                 f"离线按本机 Python {local} 查的，平台是 Python {platform['python']}"))
    if shutil.which("bash") is None and any(rel.endswith(".sh") for rel in scripts):
        items.append(checks.item("shell_syntax", "Shell 脚本语法", "upload", "本机没有 bash，离线没查"))
    return items


def _limits_item(directory: Path, platform: dict) -> dict:
    from checker.services.packs import checks
    from checker.services.packs.repository import pack_files

    files = pack_files(directory)
    size = sum((directory / rel).stat().st_size for rel in files)
    limits = platform["limits"]
    # 校验和文件也进 zip，算一个文件
    over = len(files) + 1 > limits["files"] or size > limits["extracted_bytes"]
    return checks.item("limits", "包大小在上传限制内", "fail" if over else "pass",
                       f"{len(files) + 1} 个文件（上限 {limits['files']}），解压后 {size} 字节（上限 {limits['extracted_bytes']}）")


def run_checks(directory: Path, platform: dict, *, write_checksums: bool) -> tuple[object, list[dict], list[dict]]:
    """返回（清单或 None, 检查项, 上传时检查项）。检查项和服务器 PL-22 上传时的同一份，多一项 limits。"""
    from checker.services.packs import checks
    from checker.services.packs import manifest as mf
    from checker.services.packs import repository as repo

    if write_checksums and (directory / mf.MANIFEST_FILE).is_file():
        repo.write_checksums(directory)
    core_names = {key: set(names) for key, names in platform["core"]["names"].items()}
    with tempfile.TemporaryDirectory(prefix="onto-pack-tool-") as scratch:
        snapshot = _Snapshot(platform, Path(scratch))
        try:
            manifest, items = checks.run(directory, snapshot, tenant=True, core_names=core_names)
        except Exception as exc:  # noqa: BLE001 - 和服务器一样：检查本身出错也算失败
            return None, [checks.item("internal", "静态检查", "fail", f"检查过程出错：{type(exc).__name__}: {exc}")], []
        if manifest is None:
            return None, items, []
        upload_time = _defer_private_depends(items, manifest, snapshot)
    upload_time += _upload_time_items(directory, manifest, platform)
    if manifest.name in platform["packs"]:
        items.append(checks.item("private_name", "包名没被占用", "fail", f"{manifest.name} 已被占用，请换一个名字"))
        upload_time = [item for item in upload_time if item["id"] != "private_name"]
    items.append(_limits_item(directory, platform))
    return manifest, items, upload_time


def _report(manifest, items: list[dict], upload_time: list[dict], *, as_json: bool, extra: dict | None = None) -> None:
    from checker.services.packs import checks

    summary = checks.summary(items)
    if as_json:
        print(json.dumps({"ok": summary["fail"] == 0, "name": manifest.name if manifest else None,
                          "version": manifest.version if manifest else None, "summary": summary, "checks": items,
                          "upload_time": upload_time, **(extra or {})}, ensure_ascii=False))
        return
    for item in items:
        detail = f"：{item['detail']}" if item["detail"] else ""
        print(f"{MARKS.get(item['result'], item['result'])}  {item['label']}{detail}")
    if upload_time:
        print("上传时检查（离线查不了，以上传后的报告为准）：")
        for item in upload_time:
            print(f"  - {item['label']}：{item['detail']}")
    print(f"结果：通过 {summary['pass']} 项，警告 {summary['warn']} 项，失败 {summary['fail']} 项")
    for key, value in (extra or {}).items():
        print(f"{key}: {value}")


def _pack_dir(raw: str) -> Path:
    directory = Path(raw).resolve()
    if not directory.is_dir():
        sys.exit(f"{directory} 不是目录")
    return directory


def cmd_init(args) -> int:
    import yaml

    from checker.services.packs import checks
    from checker.services.packs import manifest as mf

    platform = _platform()
    kind = args.kind or platform["kinds"][0]
    if kind not in platform["kinds"]:
        sys.exit(f"这个 skill 能建的 kind：{' / '.join(platform['kinds'])}")
    name = args.name
    if not mf.NAME_RE.match(name):
        sys.exit("包名要是 2～40 位小写字母、数字或连字符，以字母开头")
    if name in checks.RESERVED_PACK_NAMES or name in platform["packs"]:
        sys.exit(f"{name} 已被平台占用，请换一个名字")
    target = Path(args.out).resolve() / name
    if target.exists():
        sys.exit(f"{target} 已存在，换个包名或先处理掉它")
    for manifest_path in sorted((SKILL / "references" / "examples").glob("*/manifest.yaml")):
        example = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if example.get("kind") == kind:
            break
    else:
        sys.exit(f"skill 里没有 kind 为 {kind} 的样例")
    old = str(example["name"])
    renames = ((old, name), (old.replace("-", "_"), name.replace("-", "_")))
    source = manifest_path.parent
    created = []
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source).as_posix()
        if not path.is_file() or rel == mf.CHECKSUMS_FILE or "__pycache__" in rel or path.name == ".DS_Store":
            continue
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            for before, after in renames:
                text = text.replace(before, after)
            data = text.encode("utf-8")
        for before, after in renames:
            rel = rel.replace(before, after)
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        created.append(rel)
    print(f"已建骨架：{target}（kind: {kind}）")
    for rel in created:
        print(f"  {rel}")
    print(f"下一步：按 references/{platform['category']}.md 把样例内容换成需求内容，再运行 build")
    return 0


def cmd_check(args) -> int:
    directory = _pack_dir(args.directory)
    manifest, items, upload_time = run_checks(directory, _platform(), write_checksums=not args.verify_only)
    _report(manifest, items, upload_time, as_json=args.json)
    return 1 if any(item["result"] == "fail" for item in items) else 0


def cmd_build(args) -> int:
    from checker.services.packs import manifest as mf
    from checker.services.packs.repository import pack_files

    directory = _pack_dir(args.directory)
    out = Path(args.out).resolve() if args.out else directory.parent / "dist"
    if out == directory or directory in out.parents:
        sys.exit("--out 不能在包目录里，否则 zip 会被算进包内容")
    platform = _platform()
    manifest, items, upload_time = run_checks(directory, platform, write_checksums=True)
    if any(item["result"] == "fail" for item in items):
        _report(manifest, items, upload_time, as_json=args.json)
        print("检查没通过，没有打包", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{manifest.name}-{manifest.version}.zip"
    # zip 根就是包；pack_files 已经排除 __pycache__、.DS_Store、.pyc
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
        for rel in [*pack_files(directory), mf.CHECKSUMS_FILE]:
            archive.write(directory / rel, rel)
    size, limit = target.stat().st_size, platform["limits"]["zip_bytes"]
    if size > limit:
        target.unlink()
        _report(manifest, items, upload_time, as_json=args.json)
        print(f"zip 有 {size} 字节，超过上传上限 {limit} 字节，没有留下文件", file=sys.stderr)
        return 1
    _report(manifest, items, upload_time, as_json=args.json, extra={"zip": str(target), "zip_bytes": size})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="插件包工具：init 建骨架，check 离线检查，build 打 zip")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="从合成样例建骨架")
    init.add_argument("name", help="包名")
    init.add_argument("--kind", help="清单的 kind，缺省取这个 skill 的第一种")
    init.add_argument("--out", default=".", help="骨架放在哪个目录下，缺省当前目录")
    init.set_defaults(run=cmd_init)
    check = commands.add_parser("check", help="写校验和并离线检查")
    check.add_argument("directory", help="包目录")
    check.add_argument("--verify-only", action="store_true", help="不写校验和，只核对现有的")
    check.add_argument("--json", action="store_true", help="输出 JSON")
    check.set_defaults(run=cmd_check)
    build = commands.add_parser("build", help="检查通过后打 zip")
    build.add_argument("directory", help="包目录")
    build.add_argument("--out", help="zip 放在哪个目录，缺省包目录旁边的 dist/")
    build.add_argument("--json", action="store_true", help="输出 JSON")
    build.set_defaults(run=cmd_build)
    args = parser.parse_args(argv)
    _require_runtime()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
