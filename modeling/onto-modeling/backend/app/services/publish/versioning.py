"""发布标签（ADR-0091 取代 ADR-0020 的版本号部分）。

版本号由知识包 Hub 分配（0.1.N），本地不再按主线计数自算。Hub 提交成功后在发布的提交上打
release/<版本> 注释标签，历史页与发布记录靠它显示；标签说明记发布人、是否降级与 Hub 包身份。
旧版本地发布留下的 release/0.{n}.0 标签照常列出，只是没有 Hub 身份。
"""

from __future__ import annotations

import re

TAG_PREFIX = "release/"
TAG_RE = re.compile(r"release/(\d+)\.(\d+)\.(\d+)")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

Version = tuple[int, int, int]


def parse_tag(name: str) -> Version | None:
    match = TAG_RE.fullmatch(name or "")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def text(version: Version) -> str:
    return ".".join(str(part) for part in version)


def tag_name(version: str) -> str:
    return f"{TAG_PREFIX}{version}"


def release_message(version: str, author: str, degraded: list[str], hub: dict | None = None) -> str:
    lines = [f"发布 {version}", "", f"发布人：{author or '未登录用户'}"]
    if degraded:
        lines.append(f"降级发布：是（{len(degraded)} 个对象未绑定数据表）")
    else:
        lines.append("降级发布：否")
    if hub:
        lines.append(f"Hub：{hub['ontology_id']} {hub['release_id']}")
    return "\n".join(lines) + "\n"


_BY_RE = re.compile(r"^发布人：(.*)$", re.M)
_DEGRADED_RE = re.compile(r"^降级发布：是", re.M)
_HUB_RE = re.compile(r"^Hub：([\w-]{1,128}) ([\w-]{1,128})$", re.M)


def parse_release_message(message: str) -> tuple[str, bool]:
    """发布标签说明（release_message 写的）→ (发布人, 是否降级发布)。"""
    by = _BY_RE.search(message or "")
    author = by.group(1).strip() if by else ""
    return ("" if author == "未登录用户" else author), bool(_DEGRADED_RE.search(message or ""))


def parse_hub(message: str) -> dict | None:
    """标签说明里的 Hub 包身份 {ontology_id, release_id}；旧版本地发布没有，返回 None。"""
    found = _HUB_RE.search(message or "")
    return {"ontology_id": found.group(1), "release_id": found.group(2)} if found else None
