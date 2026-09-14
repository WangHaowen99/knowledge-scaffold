# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Construction and validation of immutable fact snapshots."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
import unicodedata

from .contracts import FactSnapshot, NormalizedInput, SolverContractError


class FactSnapshotInvalidError(SolverContractError):
    """Raised when facts cannot form one deterministic, conflict-free snapshot."""

    code = "fact_snapshot_invalid"
    stage = "snapshot"

    def __init__(self, detail: str, *, details: Iterable[Mapping[str, Any]] = ()) -> None:
        super().__init__(detail)
        self.detail = detail
        self.details = tuple(dict(item) for item in details)


FactSnapshotInvalid = FactSnapshotInvalidError


@dataclass(frozen=True)
class FactSnapshotBuilder:
    """Normalize synthetic/provider-independent facts into a content digest."""

    def build(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        package_id: str | None = None,
        knowledge_package_id: str | None = None,
        package_revision: str | None = None,
        knowledge_package_revision: str | None = None,
        normalized_input: NormalizedInput,
        facts: Mapping[str, Any] | Iterable[Mapping[str, Any]],
        bindings: Mapping[str, Any],
        as_of: str,
        run_id: str = "",
        source_evidence: Iterable[Any] = (),
        required_facts: Iterable[str] | None = None,
    ) -> FactSnapshot:
        if not isinstance(normalized_input, NormalizedInput):
            raise FactSnapshotInvalidError("normalized_input is required")
        selected_package_id = self._resolve_identity_alias(
            package_id,
            knowledge_package_id,
            "package_id",
        )
        revision = self._resolve_identity_alias(
            package_revision,
            knowledge_package_revision,
            "package_revision",
        )
        if not isinstance(bindings, Mapping):
            raise FactSnapshotInvalidError("bindings must be an object")
        if required_facts is None:
            raise FactSnapshotInvalidError(
                "required_facts completeness declaration is required"
            )
        if isinstance(required_facts, (str, bytes, Mapping, set, frozenset)):
            raise FactSnapshotInvalidError("required_facts must be an ordered array")
        try:
            required_values = tuple(required_facts)
        except TypeError as exc:
            raise FactSnapshotInvalidError("required_facts must be an array") from exc
        required_names = []
        for value in required_values:
            name = str(value or "").strip()
            if not name:
                raise FactSnapshotInvalidError("required_facts contains an invalid name")
            required_names.append(name)
        if len(set(required_names)) != len(required_names):
            raise FactSnapshotInvalidError("required_facts contains duplicates")
        normalized_facts = self._normalize_facts(facts)
        required = set(required_names)
        present = {str(item["name"]) for item in normalized_facts}
        missing = sorted(required - present)
        if missing:
            raise FactSnapshotInvalidError(
                "required facts are missing",
                details=({"code": "missing", "facts": missing},),
            )
        checks = {
            "required": sorted(required),
            "missing": missing,
            "duplicates": [],
            "conflicts": [],
        }
        evidence = tuple(source_evidence)
        if not evidence:
            evidence = tuple(normalized_input.source_evidence)
        try:
            return FactSnapshot(
                workspace_id=workspace_id,
                owner_id=owner_id,
                package_id=selected_package_id,
                package_revision=revision,
                normalized_input_digest=normalized_input.digest,
                facts=tuple(normalized_facts),
                bindings=bindings,
                as_of=as_of,
                source_evidence=evidence,
                checks=checks,
                run_id=run_id,
            )
        except SolverContractError as exc:
            raise FactSnapshotInvalidError(str(exc)) from exc

    @staticmethod
    def _resolve_identity_alias(
        canonical: str | None,
        legacy: str | None,
        field_name: str,
    ) -> str:
        values = []
        for value in (canonical, legacy):
            if value is None:
                continue
            if not isinstance(value, str):
                raise FactSnapshotInvalidError("{} is required".format(field_name))
            normalized = unicodedata.normalize("NFC", value).strip()
            if not normalized:
                raise FactSnapshotInvalidError("{} is required".format(field_name))
            values.append(normalized)
        if not values:
            raise FactSnapshotInvalidError("{} is required".format(field_name))
        if len(set(values)) != 1:
            raise FactSnapshotInvalidError("{} aliases conflict".format(field_name))
        return values[0]

    @staticmethod
    def _normalize_facts(
        facts: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if isinstance(facts, Mapping):
            raw_items: list[Mapping[str, Any]] = []
            for name, value in facts.items():
                if isinstance(value, Mapping):
                    embedded_name = value.get("name")
                    if embedded_name is not None and str(embedded_name).strip() != str(name):
                        raise FactSnapshotInvalidError(
                            "fact name conflicts with canonical mapping key: {}".format(name),
                            details=({"code": "conflict", "fact": str(name)},),
                        )
                    raw_items.append({**dict(value), "name": name})
                else:
                    raw_items.append({"name": name, "value": value})
        else:
            try:
                raw_items = list(facts)
            except TypeError as exc:
                raise FactSnapshotInvalidError("facts must be an object or array") from exc

        result: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, Mapping):
                raise FactSnapshotInvalidError("fact {} must be an object".format(index))
            name = str(raw.get("name") or "").strip()
            if not name:
                raise FactSnapshotInvalidError("fact name is required")
            if "value" not in raw:
                raise FactSnapshotInvalidError("fact value is required: {}".format(name))
            item: dict[str, Any] = {
                "name": name,
                "value": raw["value"],
                "unit": raw.get("unit"),
                "precision": raw.get("precision"),
                "entity_id": raw.get("entity_id"),
                "business_key": raw.get("business_key"),
                "source_evidence": raw.get("source_evidence", ()),
            }
            if name in seen:
                comparable_keys = (
                    "value",
                    "unit",
                    "precision",
                    "entity_id",
                    "business_key",
                )
                conflict = any(
                    seen[name].get(key) != item.get(key) for key in comparable_keys
                )
                code = "conflict" if conflict else "duplicate"
                raise FactSnapshotInvalidError(
                    "{} fact: {}".format(code, name),
                    details=({"code": code, "fact": name},),
                )
            seen[name] = item
            result.append(item)
        result.sort(key=lambda item: item["name"])
        return result


def build_fact_snapshot(**kwargs: Any) -> FactSnapshot:
    return FactSnapshotBuilder().build(**kwargs)
