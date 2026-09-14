# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Problem-family implementations supported by solver coordination."""

from .binary_ordered_route import (
    BinaryOrderedRouteAdapter,
    BinaryOrderedRouteProblemFamilyAdapter,
)
from .registry import ProblemFamilyRegistry


def build_default_problem_family_registry() -> ProblemFamilyRegistry:
    return ProblemFamilyRegistry([BinaryOrderedRouteAdapter()])


__all__ = [
    "BinaryOrderedRouteAdapter",
    "BinaryOrderedRouteProblemFamilyAdapter",
    "ProblemFamilyRegistry",
    "build_default_problem_family_registry",
]
