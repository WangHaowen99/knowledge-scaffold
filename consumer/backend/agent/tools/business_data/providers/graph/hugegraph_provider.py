# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""HugeGraph GraphSearchProvider."""

from __future__ import annotations

from .backends.hugegraph import HugeGraphBackend
from .provider import GraphSearchProvider


class HugeGraphSearchProvider(GraphSearchProvider):
    name = "hugegraph"

    def __init__(self, config: dict):
        ontology_search = dict(config.get("ontology_search") or {})
        provider_config = dict(ontology_search.get("hugegraph") or {})
        super().__init__(
            provider_config=provider_config,
            backend=HugeGraphBackend(provider_config),
        )
