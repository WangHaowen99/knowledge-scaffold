"""发布适配器 SPI（方案 2.1「发布适配器 SPI」、详设 ⑱）。

发布流程（publish/service.py）只认这个协议：确定提交 → git archive 只读树 → 门禁（check）→
渲染（render）→ 留存 → 打标签 → 写发布记录。适配器只读 ReleaseSource，不碰工作区与 git。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ReleaseSource:
    """一次渲染的输入：项目、来源提交、按提交导出的只读树、元数据、版本号、回执目录。"""

    project_id: str
    commit: str
    tree: Path  # 只读树：渲染只从这里读内容
    meta: dict
    version: str
    state_root: Path  # 验证回执所在（项目工作区的 .ontology-state，不进 git）
    variant: str = "standard"  # standard | graph（图模式变体）
    allow_unbound: bool = False  # 降级发布：只放宽数据绑定
    draft: bool = False  # 求解验证候选包：manifest 标 draft，不是发布
    binding: dict | None = None  # 调用方按同一棵树已算好的数据绑定，缺省由适配器现算


@dataclass(frozen=True)
class Manifest:
    """渲染结果摘要：格式、schema 版本、包摘要与降级时未绑定的对象。"""

    format: str  # ontology | solver
    schema_version: str
    sha256: str
    size: int
    unbound_objects: list[str] = field(default_factory=list)


class PublishAdapter(Protocol):
    id: str
    formats: tuple[str, ...]
    variants: tuple[str, ...]

    def check(self, src: ReleaseSource) -> list[dict]: ...  # Finding 列表

    def render(self, src: ReleaseSource, out: Path) -> Manifest: ...

    def materialize(self, archive: Path, dest: Path) -> dict: ...  # 反向导入
