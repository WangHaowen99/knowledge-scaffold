#!/usr/bin/env python3
"""把 plugins/<包名>/ 打成能在插件中心上传的 zip。

    python3 modeling/onto-modeling/plugins/build_pack.py modeling/onto-modeling/plugins/common-ontology [--out 目录]

做法和插件开发 skill 的 pack_tool.py build 一致（ADR-0081）：复制到临时目录、写 checksums.sha256、跑平台查上传包的
同一份静态检查（后端 app/services/packs/checks.py，按租户包口径），全部通过才打 zip，zip 的根就是包。源目录不写任何文件。
另外按仓库里的官方包查命令重名，并卡上传限制（zip ≤ 20 MiB，解压后的大小与文件数同私有包上限）。
离线查不了的（包名、命令是否被别人的私有包占用，同版本是否装过）以上传后的检查报告为准。只要 Python 3.9+ 和 PyYAML。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

PLUGINS = Path(__file__).resolve().parent
APP_ROOT = PLUGINS.parent
sys.path.insert(0, str(APP_ROOT / "backend"))

from app.services.packs import checks  # noqa: E402
from app.services.packs import manifest as mf  # noqa: E402
from app.services.packs.repository import pack_files, write_checksums  # noqa: E402
from app.services.packs.uploads import PRIVATE_MAX_EXTRACTED_BYTES, PRIVATE_MAX_FILES  # noqa: E402

ZIP_MAX_BYTES = 20 * 1024 * 1024
MARKS = {"pass": "通过", "warn": "警告", "fail": "失败"}
_SKIP_NAMES = {"__pycache__", ".DS_Store", mf.CHECKSUMS_FILE}
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def stage(source: Path, target: Path) -> Path:
    """复制包目录（不带缓存、系统杂项和旧校验和）并写校验和；返回副本目录。"""
    shutil.copytree(source, target, ignore=lambda _dir, names: [
        name for name in names if name in _SKIP_NAMES or name.endswith((".pyc", ".pyo"))])
    write_checksums(target)
    return target


def _core_names() -> dict[str, set[str]]:
    toolkit = APP_ROOT / "assets" / "workspace-toolkit"
    return checks.declared_names(mf.load(toolkit), toolkit)


def _official_commands() -> dict[str, str]:
    taken = {}
    for path in sorted((APP_ROOT / "assets" / "packs").glob("*/manifest.yaml")):
        try:
            manifest = mf.load(path.parent)
        except mf.ManifestError:
            continue
        for command in manifest.items("commands"):
            taken.setdefault(command, manifest.name)
    return taken


def check(directory: Path) -> tuple[mf.Manifest | None, list[dict]]:
    """副本目录上跑检查：平台静态检查 + 官方包命令重名 + 上传限制（zip 大小在打包后另查）。"""
    manifest, items = checks.run(directory, None, tenant=True, core_names=_core_names())
    if manifest is None:
        return None, items
    taken = _official_commands()
    clash = [f"{name}（{taken[name]}）" for name in manifest.items("commands") if name in taken]
    items.append(checks.item("official_commands", "命令不和官方包重名", "fail" if clash else "pass", "、".join(clash)))
    files = pack_files(directory)
    size = sum((directory / rel).stat().st_size for rel in files)
    over = len(files) + 1 > PRIVATE_MAX_FILES or size > PRIVATE_MAX_EXTRACTED_BYTES
    items.append(checks.item("limits", "包大小在上传限制内", "fail" if over else "pass",
                             f"{len(files) + 1} 个文件，解压后 {size} 字节"))
    return manifest, items


def write_zip(directory: Path, target: Path) -> int:
    """zip 根就是包；成员按路径排序、时间戳固定，同样的内容打出同样的 zip。"""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in [*pack_files(directory), mf.CHECKSUMS_FILE]:
            info = zipfile.ZipInfo(rel, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (directory / rel).read_bytes())
    return target.stat().st_size


def build(source: Path, out: Path) -> tuple[Path | None, list[dict]]:
    source, out = source.resolve(), out.resolve()
    if out == source or source in out.parents:
        raise SystemExit("--out 不能在包目录里")
    with tempfile.TemporaryDirectory(prefix="onto-plugin-build-") as scratch:
        staged = stage(source, Path(scratch) / source.name)
        manifest, items = check(staged)
        if manifest is None or any(item["result"] == "fail" for item in items):
            return None, items
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{manifest.name}-{manifest.version}.zip"
        size = write_zip(staged, target)
    if size > ZIP_MAX_BYTES:
        target.unlink()
        items.append(checks.item("zip_size", "zip 不超过 20 MiB", "fail", f"{size} 字节"))
        return None, items
    items.append(checks.item("zip_size", "zip 不超过 20 MiB", "pass", f"{size} 字节"))
    return target, items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把插件目录打成可上传的 zip")
    parser.add_argument("directory", help="插件目录，如 plugins/common-ontology")
    parser.add_argument("--out", default=str(PLUGINS / "dist"), help="zip 放哪个目录，缺省 plugins/dist/")
    args = parser.parse_args(argv)
    target, items = build(Path(args.directory), Path(args.out))
    for item in items:
        detail = f"：{item['detail']}" if item["detail"] else ""
        print(f"{MARKS.get(item['result'], item['result'])}  {item['label']}{detail}")
    if target is None:
        print("检查没通过，没有打包")
        return 1
    print(f"zip：{target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
