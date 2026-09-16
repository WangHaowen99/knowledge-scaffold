# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Backend-owned fields shared across runtime execution boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# Package-selection metadata travels beside business arguments at the runtime
# boundary, but it is always owned by the backend.  Keep one definition so
# runtime adapters, deterministic schema validation and checkpoint migration
# cannot disagree about which fields must be removed from business input.
DETERMINISTIC_ROUTING_ARGUMENT_FIELDS = frozenset(
    {
        "selected_ontology_id",
        "selected_ontology_version",
        "selected_version",
    }
)


def deterministic_business_arguments(
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return deterministic request arguments without backend routing fields."""

    return {
        key: value
        for key, value in dict(arguments or {}).items()
        if key not in DETERMINISTIC_ROUTING_ARGUMENT_FIELDS
    }
