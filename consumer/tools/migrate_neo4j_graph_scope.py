#!/usr/bin/env python3
"""Dry-run/apply Neo4j graph workspace scope compatibility migration."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Mapping, TextIO

logger = logging.getLogger(__name__)


def _configure_cli_logging(stream: TextIO) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main() -> None:
    _configure_cli_logging(sys.stdout)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", required=True, help="Neo4j Bolt URI, e.g. bolt://127.0.0.1:7687")
    parser.add_argument("--username", default="neo4j")
    parser.add_argument("--password", required=True)
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--workspace-id", required=True, help="Target d2o workspace id to write.")
    parser.add_argument("--ontology-id", default="", help="Optional ontology id to write.")
    parser.add_argument("--workspace-property", default="d2o_workspace_id")
    parser.add_argument("--ontology-property", default="d2o_ontology_id")
    parser.add_argument("--legacy-scene-name", default="", help="Only migrate records matching scene_name.")
    parser.add_argument("--legacy-workspace-name", default="", help="Only migrate records matching workspace_name.")
    parser.add_argument("--label", default="D2O_NODE", help="Node label to migrate.")
    parser.add_argument("--relationship-type", default="D2O_RELATION", help="Relationship type to migrate.")
    parser.add_argument("--apply", action="store_true", help="Apply updates. Omit for dry-run.")
    parser.add_argument(
        "--limit", type=int, default=0, help="Optional max records to update per node/relationship query."
    )
    args = parser.parse_args()

    driver = _connect(args.uri, args.username, args.password)
    try:
        result = migrate_scope(driver, args)
    finally:
        driver.close()
    logger.info(json.dumps(result, ensure_ascii=False, indent=2))


def migrate_scope(driver: Any, args: argparse.Namespace) -> dict[str, Any]:
    node_match, node_params = _build_match(
        alias="n",
        label=args.label,
        workspace_property=args.workspace_property,
        legacy_scene_name=args.legacy_scene_name,
        legacy_workspace_name=args.legacy_workspace_name,
    )
    rel_match, rel_params = _build_match(
        alias="r",
        label="",
        relationship_type=args.relationship_type,
        workspace_property=args.workspace_property,
        legacy_scene_name=args.legacy_scene_name,
        legacy_workspace_name=args.legacy_workspace_name,
    )
    with driver.session(database=args.database) as session:
        node_missing = _count(session, node_match, node_params)
        rel_missing = _count(session, rel_match, rel_params)
        updated_nodes = 0
        updated_relationships = 0
        if args.apply:
            write_props = {args.workspace_property: args.workspace_id}
            if args.ontology_id and args.ontology_property:
                write_props[args.ontology_property] = args.ontology_id
            updated_nodes = _apply(session, node_match, node_params, "n", write_props, args.limit)
            updated_relationships = _apply(session, rel_match, rel_params, "r", write_props, args.limit)

    return {
        "mode": "apply" if args.apply else "dry-run",
        "workspace_property": args.workspace_property,
        "workspace_id": args.workspace_id,
        "ontology_property": args.ontology_property if args.ontology_id else "",
        "ontology_id": args.ontology_id,
        "legacy_filters": {
            "scene_name": args.legacy_scene_name,
            "workspace_name": args.legacy_workspace_name,
        },
        "missing_scope": {
            "nodes": node_missing,
            "relationships": rel_missing,
        },
        "updated": {
            "nodes": updated_nodes,
            "relationships": updated_relationships,
        },
    }


def _connect(uri: str, username: str, password: str) -> Any:
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("neo4j driver is not installed; install backend/requirements.txt first") from exc
    return GraphDatabase.driver(uri, auth=(username, password))


def _build_match(
    *,
    alias: str,
    label: str,
    workspace_property: str,
    relationship_type: str = "",
    legacy_scene_name: str = "",
    legacy_workspace_name: str = "",
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    where = ["{}.{} IS NULL".format(alias, _quote_identifier(workspace_property))]
    if legacy_scene_name:
        where.append("{}.scene_name = $legacy_scene_name".format(alias))
        params["legacy_scene_name"] = legacy_scene_name
    if legacy_workspace_name:
        where.append("{}.workspace_name = $legacy_workspace_name".format(alias))
        params["legacy_workspace_name"] = legacy_workspace_name
    if relationship_type:
        match = "MATCH ()-[{}:{}]->()".format(alias, _quote_identifier(relationship_type))
    else:
        match = "MATCH ({}:{})".format(alias, _quote_identifier(label))
    return "{} WHERE {}".format(match, " AND ".join(where)), params


def _count(session: Any, match_clause: str, params: Mapping[str, Any]) -> int:
    record = session.run("{} RETURN count(*) AS total".format(match_clause), dict(params)).single()
    return int(record["total"] if record else 0)


def _apply(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    session: Any,
    match_clause: str,
    params: Mapping[str, Any],
    alias: str,
    write_props: Mapping[str, Any],
    limit: int,
) -> int:
    set_parts = []
    write_params = dict(params)
    for index, (key, value) in enumerate(write_props.items()):
        param_name = "write_value_{}".format(index)
        write_params[param_name] = value
        set_parts.append("{}.{} = ${}".format(alias, _quote_identifier(key), param_name))
    if int(limit or 0) > 0:
        query = "{} WITH {} LIMIT {} SET {} RETURN count({}) AS total".format(
            match_clause,
            alias,
            int(limit),
            ", ".join(set_parts),
            alias,
        )
    else:
        query = "{} SET {} RETURN count({}) AS total".format(
            match_clause,
            ", ".join(set_parts),
            alias,
        )
    record = session.run(query, write_params).single()
    return int(record["total"] if record else 0)


def _quote_identifier(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not normalized.replace("_", "a").isalnum():
        raise ValueError("unsafe Neo4j identifier: {}".format(value))
    return "`{}`".format(normalized.replace("`", "``"))


if __name__ == "__main__":
    main()
