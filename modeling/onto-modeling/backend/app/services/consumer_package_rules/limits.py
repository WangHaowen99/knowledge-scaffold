"""consumer 导入知识包时的数量与大小上限，字段与默认值同上游 config.LimitsConfig。

consumer 的取值顺序为 ops_config.json > 环境变量 > 默认值。建模读不到 consumer 的 ops_config.json，
部署调整了 consumer 上限时，须在建模服务设置下面的同名环境变量，自检结论才与 consumer 一致。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _limit(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class LimitsConfig:
    ontology_object_max_count: int
    ontology_operation_max_count: int
    entity_attribute_max_count: int
    operation_max_chars: int

    @classmethod
    def from_env(cls) -> LimitsConfig:
        return cls(
            ontology_object_max_count=_limit("ONTOLOGY_OBJECT_MAX_COUNT", 1000),
            ontology_operation_max_count=_limit("ONTOLOGY_OPERATION_MAX_COUNT", 500),
            entity_attribute_max_count=_limit("ENTITY_ATTRIBUTE_MAX_COUNT", 300),
            operation_max_chars=_limit("ONTOLOGY_OPERATION_MAX_CHARS", 30000),
        )
