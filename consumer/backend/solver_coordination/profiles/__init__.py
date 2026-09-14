# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Public profile contracts and ordered-route implementation."""

from .contracts import (
    ProfileCompiler,
    ProfileFactProvider,
    ProfileInputAdapter,
    ProfilePresentationPort,
    ProfileVerifier,
    SolverProfileRegistration,
)
from .ordered_binary_route import (
    CandidateEvaluation,
    LOCKED_OBJECTIVES,
    LockedModelFactProvider,
    OrderedBinaryRouteCompiler,
    OrderedBinaryRouteEvaluator,
    OrderedBinaryRouteInputAdapter,
    PROFILE_ID,
    ROUTE_FACT_NAME,
    scaled_int,
)
from .registry import (
    ProfileFactResolver,
    ProfileNotFound,
    ProfileRegistryError,
    SolverProfileRegistry,
)
from .input_adapter import InputAliasConflict, ProfileInputAdapterService


def build_default_solver_profile_registry(
    *, fact_provider: ProfileFactProvider | None = None
) -> SolverProfileRegistry:
    """Build the complete immutable registry used by production runtime wiring."""

    from solver_coordination.presentation import OrderedBinaryRoutePresenter
    from solver_coordination.verification.ordered_binary_route import (
        OrderedBinaryRouteIndependentVerifier,
    )

    return SolverProfileRegistry(
        [
            SolverProfileRegistration(
                profile_id=PROFILE_ID,
                fact_provider=(
                    fact_provider
                    if fact_provider is not None
                    else LockedModelFactProvider()
                ),
                compiler=OrderedBinaryRouteCompiler(),
                verifier=OrderedBinaryRouteIndependentVerifier(),
                presenter=OrderedBinaryRoutePresenter(),
                input_adapter=OrderedBinaryRouteInputAdapter(),
            )
        ]
    )


__all__ = [
    "CandidateEvaluation",
    "LOCKED_OBJECTIVES",
    "LockedModelFactProvider",
    "OrderedBinaryRouteCompiler",
    "OrderedBinaryRouteEvaluator",
    "OrderedBinaryRouteInputAdapter",
    "PROFILE_ID",
    "ProfileCompiler",
    "ProfileFactProvider",
    "ProfileInputAdapter",
    "ProfileInputAdapterService",
    "InputAliasConflict",
    "ProfileFactResolver",
    "ProfileNotFound",
    "ProfilePresentationPort",
    "ProfileRegistryError",
    "ProfileVerifier",
    "ROUTE_FACT_NAME",
    "SolverProfileRegistration",
    "SolverProfileRegistry",
    "build_default_solver_profile_registry",
    "scaled_int",
]
