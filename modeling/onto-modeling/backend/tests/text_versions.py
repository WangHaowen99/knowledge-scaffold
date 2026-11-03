"""测试用：把资料区里的 Excel 转成文本版（F4c 起 schema 表头、数据绑定只读文本版，不解析原件）。

真实部署里由连接器网关里的转换器转（ADR-0073），测试里没有网关，这里直接调同一个转换器（assets/packs/convert-xlsx），
写到 resource/text/<分组>/<原文件名>.md，第一行是内核写的文件头。需要进 git 的（发布从提交导出）传 commit=True，只提交文本版。
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

from app import config
from app.evidence import convert

EXCEL = (".xlsx", ".xlsm")


def _converter():
    path = Path(config.ASSETS_DIR) / "packs" / "convert-xlsx" / "converter.py"
    spec = importlib.util.spec_from_file_location("test_convert_xlsx", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_text_versions(root: Path, *, commit: bool = False) -> list[str]:
    """资料区里还没有文本版的 Excel 都转一份；返回写出的文本版路径。"""
    root = Path(root)
    resource = root / "resource"
    written = []
    if not resource.is_dir():
        return written
    converter = _converter()
    for path in sorted(resource.rglob("*")):
        rel = path.relative_to(resource)
        if not path.is_file() or path.suffix.lower() not in EXCEL or rel.parts[:1] == ("text",):
            continue
        target = resource / "text" / f"{rel.as_posix()}.md"
        data = path.read_bytes()
        result = converter.convert(data, path.name, convert.ConvertLimits())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(convert.header(path.name, hashlib.sha256(data).hexdigest(), "convert-xlsx@test") + "\n"
                          + result.markdown, encoding="utf-8")
        written.append(target.relative_to(root).as_posix())
    if commit and written:
        subprocess.run(["git", "add", "--", *written], cwd=root, check=True, capture_output=True)
        # 只提交文本版，索引里别人已暂存的改动不带进来（与写入路径的提交口径一致）
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-q", "-m",
                        "数据接入: 资料的文本版", "--", *written], cwd=root, check=True, capture_output=True)
    return written
