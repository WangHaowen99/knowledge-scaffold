#!/usr/bin/env python3
"""Coordinate two independent CCE release packages using their stable command interface."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import uuid

import jsonschema
import yaml

from common import (CompatibilityError, ConfigError, LockError, MigrationUnavailable,
                    checksum, emit_stdout, read_yaml, safe_extract, validate_name,
                    run_program, validate_release, validate_compatibility_manifest,
                    release_chart_from_list, IMAGE_REF)
from upgrade import transition as modeling_transition

JOINT_SIDE_ORDER = ("consumer", "modeling")


def lease_timestamp(value: datetime) -> str:
    """Serialize Lease timestamps in the CCE-accepted RFC3339 microsecond form."""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def run(*command, input_text=None):
    result = run_program(command, input=input_text, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def kubectl(config, *args, input_text=None):
    return run("kubectl", "--context", config["cluster"]["context"], *args, input_text=input_text)


def absolute(base: Path, value: str) -> Path:
    return (base / value).resolve()


def load_config(path: Path):
    raw = read_yaml(path)
    schema = json.loads((Path(__file__).parent / "deployment.schema.json").read_text())
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as exc:
        raise ConfigError(f"joint config invalid: {exc.message}") from exc
    if raw["consumer"]["releaseName"] == raw["modeling"]["releaseName"] and raw["consumer"]["namespace"] == raw["modeling"]["namespace"]:
        raise ConfigError("Consumer and Modeling release identities must differ")
    for side in JOINT_SIDE_ORDER:
        validate_name(raw[side]["releaseName"], "releaseName")
        validate_name(raw[side]["namespace"], "namespace")
        if len(raw[side]["releaseName"]) > 49:
            raise ConfigError("releaseName is too long for its operation Lease")
        for field in ("package", "values", "resolvedImages"):
            raw[side][field] = absolute(path.parent, raw[side][field])
    raw["stateDir"] = absolute(path.parent, raw["stateDir"])
    raw["compatibilityFile"] = absolute(path.parent, raw["compatibilityFile"])
    return raw


def validate_combination(config, packages):
    document = validate_compatibility_manifest(read_yaml(config["compatibilityFile"]))
    consumer = packages["consumer"][1]
    modeling = packages["modeling"][1]
    if "jiuwenbox" not in consumer["images"]:
        raise ConfigError("Consumer release does not contain the JiuwenBox image archive")
    expected = {
        "consumerReleaseVersion": consumer["releaseVersion"],
        "consumerChartVersion": consumer["chartVersion"],
        "modelingReleaseVersion": modeling["releaseVersion"],
        "modelingChartVersion": modeling["chartVersion"],
        "platform": modeling["platform"],
        "operationSchemaVersion": "cce-operation/v1",
        "endpointSchemaVersion": "cce-endpoints/v1",
    }
    if consumer["platform"] != modeling["platform"]:
        raise CompatibilityError("Consumer and Modeling platforms differ")
    combinations = document.get("combinations", [])
    matched = []
    for item in combinations:
        if isinstance(item, dict) and all(item.get(key) == value for key, value in expected.items()):
            matched.append(item)
    if len(matched) != 1:
        raise CompatibilityError("exactly one verified Consumer/Modeling combination required")
    return matched[0]


def validate_cross_side(config, packages, consumer_values, modeling_values, combination):
    auth_a = consumer_values.get("global", {}).get("auth", {})
    auth_b = modeling_values.get("global", {}).get("auth", {})
    for key in ("issuer", "audience"):
        if not auth_a.get(key) or auth_a.get(key) != auth_b.get(key):
            raise ConfigError(f"shared auth {key} differs between releases")
    endpoints = read_yaml(packages["consumer"][0] / "deploy/endpoints.yaml")
    if endpoints.get("schemaVersion") != "cce-endpoints/v1":
        raise ConfigError("invalid Consumer endpoint schema")
    items = endpoints.get("endpoints")
    if not isinstance(items, list) or any(not isinstance(item, dict) or not item.get("id") for item in items):
        raise ConfigError("Consumer endpoint list is invalid")
    by_id = {item["id"]: item for item in items}
    if len(by_id) != len(items):
        raise ConfigError("duplicate Consumer endpoint ID")
    for field, endpoint_id, suffix, port, api_field in (
            ("externalAuth", "auth", "auth", 5056, "authApiVersion"),
            ("externalConsumer", "consumer-api", "backend", 5055, "consumerApiVersion")):
        item = by_id.get(endpoint_id)
        if not item:
            raise ConfigError(f"Consumer endpoint {endpoint_id} missing")
        if item.get("apiVersion") != combination[api_field] or item.get("port") != port:
            raise CompatibilityError(f"Consumer endpoint {endpoint_id} interface differs from verified combination")
        reference = modeling_values.get(field, {})
        expected_name = f"{config['consumer']['releaseName']}-{suffix}"
        if reference.get("serviceName") != expected_name or reference.get("namespace") != config["consumer"]["namespace"] or reference.get("servicePort") != item["port"]:
            raise ConfigError(f"Modeling {field} does not match Consumer endpoint {endpoint_id}")


def candidate_combination(packages: dict) -> dict:
    """Derive version and interface fields from checked release archives."""
    consumer = packages["consumer"][1]
    modeling = packages["modeling"][1]
    if consumer["platform"] != modeling["platform"]:
        raise CompatibilityError("Consumer and Modeling platforms differ")
    if "jiuwenbox" not in consumer["images"]:
        raise ConfigError("Consumer release does not contain the JiuwenBox image archive")
    endpoints = read_yaml(packages["consumer"][0] / "deploy/endpoints.yaml")
    if endpoints.get("schemaVersion") != "cce-endpoints/v1":
        raise ConfigError("invalid Consumer endpoint schema")
    items = endpoints.get("endpoints")
    if not isinstance(items, list):
        raise ConfigError("Consumer endpoint list is invalid")
    by_id = {item.get("id"): item for item in items if isinstance(item, dict)}
    if len(by_id) != len(items):
        raise ConfigError("Consumer endpoint list contains duplicate or invalid items")
    for endpoint_id, port in (("auth", 5056), ("consumer-api", 5055)):
        item = by_id.get(endpoint_id)
        if not item or item.get("port") != port:
            raise ConfigError(f"Consumer endpoint {endpoint_id} has no supported port or API version")
        if not isinstance(item.get("apiVersion"), str) or not item["apiVersion"]:
            raise ConfigError(f"Consumer endpoint {endpoint_id} has no supported port or API version")
    return {
        "consumerReleaseVersion": consumer["releaseVersion"],
        "consumerChartVersion": consumer["chartVersion"],
        "modelingReleaseVersion": modeling["releaseVersion"],
        "modelingChartVersion": modeling["chartVersion"],
        "platform": modeling["platform"],
        "operationSchemaVersion": "cce-operation/v1",
        "endpointSchemaVersion": endpoints["schemaVersion"],
        "authApiVersion": by_id["auth"]["apiVersion"],
        "consumerApiVersion": by_id["consumer-api"]["apiVersion"],
    }


def combination_id(fields: dict) -> str:
    identity = json.dumps(fields, sort_keys=True).encode("utf-8")
    return "combo-" + hashlib.sha256(identity).hexdigest()[:12]


def include_combination(manifest: dict, fields: dict) -> str:
    matches = [item["id"] for item in manifest["combinations"]
               if all(item.get(key) == value for key, value in fields.items())]
    if len(matches) > 1:
        raise CompatibilityError("candidate combination is ambiguous")
    if matches:
        return matches[0]
    candidate_id = combination_id(fields)
    if any(item["id"] == candidate_id for item in manifest["combinations"]):
        raise CompatibilityError("generated combination ID conflicts with existing manifest")
    manifest["combinations"].append({"id": candidate_id, **fields})
    return candidate_id


def matches_installed_combination(combination: dict, installed: dict) -> bool:
    return all(
        combination.get(f"{side}ReleaseVersion") == installed[side]["releaseVersion"] and
        combination.get(f"{side}ChartVersion") == installed[side]["chartVersion"]
        for side in JOINT_SIDE_ORDER
    )


def prepare_compatibility(config: dict, *, upgrade: bool, confirmed: bool,
                          no_migration_confirmed: bool) -> dict:
    path = config["compatibilityFile"]
    if upgrade:
        if not path.is_file():
            raise ConfigError("existing verified compatibility manifest required for upgrade")
        original_checksum = checksum(path)
        manifest = validate_compatibility_manifest(read_yaml(path))
    elif path.exists():
        raise ConfigError("compatibilityFile already exists; keep the verified manifest")
    else:
        manifest = {"schemaVersion": "cce-compatibility/v1", "combinations": [], "transitions": []}
    state = config["stateDir"]
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cce-compatibility-", dir=state) as temporary:
        packages = {}
        for side in JOINT_SIDE_ORDER:
            root = safe_extract(config[side]["package"], Path(temporary), side)
            packages[side] = root, validate_release(root, side)
        target = candidate_combination(packages)
        validate_cross_side(config, packages, read_yaml(config["consumer"]["values"]),
                            read_yaml(config["modeling"]["values"]), target)
    target_id = include_combination(manifest, target)
    transition_id = None
    old_combination = None
    middle_combination = None
    if upgrade:
        installed = {side: installed_identity(config, side) for side in JOINT_SIDE_ORDER}
        old = [item for item in manifest["combinations"]
               if matches_installed_combination(item, installed)]
        if len(old) != 1:
            raise CompatibilityError("installed Consumer/Modeling combination is missing or ambiguous")
        old_combination = old[0]
        if old_combination["platform"] != target["platform"]:
            raise CompatibilityError("upgrade cannot change CPU architecture")
        middle = dict(target, modelingReleaseVersion=old_combination["modelingReleaseVersion"],
                      modelingChartVersion=old_combination["modelingChartVersion"])
        middle_id = include_combination(manifest, middle)
        middle_combination = next(item for item in manifest["combinations"] if item["id"] == middle_id)
        transition_id = "transition-" + hashlib.sha256(
            f"{old_combination['id']}:{target_id}".encode("utf-8")).hexdigest()[:12]
        transition = {"id": transition_id, "from": old_combination["id"], "to": target_id,
                      "intermediateCombinationId": middle_id, "steps": list(JOINT_SIDE_ORDER),
                      "requiresJointMaintenance": True, "modelingDataMigration": "none",
                      "preserveModelingData": True}
        existing = [item for item in manifest["transitions"] if item.get("id") == transition_id]
        if existing and existing != [transition]:
            raise CompatibilityError("generated transition ID conflicts with existing manifest")
        if not existing:
            manifest["transitions"].append(transition)
    validate_compatibility_manifest(manifest)
    if confirmed and upgrade and not no_migration_confirmed:
        raise ConfigError("confirm that existing Modeling data needs no migration before writing upgrade transition")
    if confirmed:
        path.parent.mkdir(parents=True, exist_ok=True)
        output = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)
        if upgrade:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".compatibility-", delete=False) as stream:
                stream.write(output)
                temporary_path = Path(stream.name)
            try:
                if checksum(path) != original_checksum:
                    raise ConfigError("compatibility manifest changed while preparing upgrade")
                temporary_path.replace(path)
            finally:
                temporary_path.unlink(missing_ok=True)
        else:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(output)
    return {"status": "written" if confirmed else "preview", "compatibilityFile": str(path),
            "targetCombinationId": target_id, "targetCombination": {"id": target_id, **target},
            "oldCombination": old_combination, "intermediateCombination": middle_combination,
            "transitionId": transition_id,
            "sameVersion": bool(old_combination and old_combination["id"] == target_id),
            "confirmationsRequired": (["joint-compatibility", "modeling-data-no-migration"] if upgrade
                                      else ["joint-compatibility"])}


def effective_images(config, state: Path, source: str, operation_id: str) -> Path:
    consumer = read_yaml(config["consumer"]["resolvedImages"])
    modeling = read_yaml(config["modeling"]["resolvedImages"])
    consumer_images = consumer.get("global", {}).get("resolvedImages", {})
    modeling_images = modeling.get("global", {}).get("resolvedImages", {})
    if not isinstance(consumer_images, dict) or not isinstance(modeling_images, dict):
        raise ConfigError("resolvedImages must be mappings")
    digest = consumer_images.get("jiuwenbox")
    if source == "modeling":
        digest = modeling_images.get("jiuwenbox")
    if not isinstance(digest, str) or not IMAGE_REF.fullmatch(digest):
        raise ConfigError("JiuwenBox requires an immutable Consumer image reference")
    if modeling_images.get("jiuwenbox") not in (None, digest):
        raise ConfigError("Modeling JiuwenBox reference conflicts with selected Consumer digest")
    if set(modeling_images) - {"modeling", "modelingAgent", "ontoModeling", "jiuwenbox"}:
        raise ConfigError("unexpected Modeling image ID")
    modeling_images["jiuwenbox"] = digest
    effective = state / f"{operation_id}-modeling-effective-images.yaml"
    effective.write_text(yaml.safe_dump({"global": {"resolvedImages": modeling_images}}, sort_keys=True))
    return effective


def invoke(side, action, config, package, operation_id, image_file, resume="", backup_record="",
           transition_id="", lock_holder="", transition_record="", defer_external_services=False,
           auto_backup=False, snapshot_retention=2, min_free_percent=15.0):
    script = package[0] / "deploy/install-cce.sh"
    if not script.is_file():
        raise ConfigError(f"{side} package has no deploy/install-cce.sh")
    if side == "modeling" and action == "upgrade" and auto_backup:
        if backup_record:
            raise ConfigError("automatic Modeling backup cannot use an existing backup record")
    options = ["bash", str(script), action, "--context", config["cluster"]["context"],
               "--namespace", config[side]["namespace"], "--release", config[side]["releaseName"],
               "--values", str(config[side]["values"]), "--resolved-images", str(image_file),
               "--operation-id", operation_id,
               "--state-dir", str(config["stateDir"]), "--timeout-seconds", str(config.get("timeoutSeconds", 900))]
    if resume:
        options.extend(("--resume", resume))
    if backup_record and side == "modeling":
        options.extend(("--backup-record", backup_record))
    if auto_backup and side == "modeling" and action == "upgrade":
        options.extend(("--auto-backup", "--snapshot-retention", str(snapshot_retention),
                        "--min-free-percent", str(min_free_percent)))
    if transition_id and side == "modeling":
        options.extend(("--transition-id", transition_id))
    if transition_record and side == "modeling":
        options.extend(("--transition-record", str(transition_record)))
    if lock_holder:
        options.extend(("--lock-holder", lock_holder))
    if defer_external_services:
        if side != "modeling" or action != "preflight":
            raise ConfigError("only Modeling preflight may defer Consumer Service lookup")
        options.append("--defer-external-services")
    result = run_program(options, text=True, capture_output=True, timeout=config.get("timeoutSeconds", 900) + 30)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{side} {action} returned invalid JSON") from exc
    if result.returncode or payload.get("schemaVersion") != "cce-operation/v1" or payload.get("status") != "succeeded":
        raise RuntimeError(f"{side} {action} failed: {payload.get('errorCode', 'UNKNOWN')}")
    if payload.get("releaseVersion") != package[1]["releaseVersion"] or payload.get("chartVersion") != package[1]["chartVersion"]:
        raise RuntimeError(f"{side} {action} returned a release version mismatch")
    return payload


def operation_fingerprint(config) -> str:
    identity = {"context": config["cluster"]["context"]}
    for side in JOINT_SIDE_ORDER:
        identity[side] = {"releaseName": config[side]["releaseName"], "namespace": config[side]["namespace"]}
        for field in ("package", "values", "resolvedImages"):
            identity[side][field] = checksum(config[side][field])
    identity["compatibility"] = checksum(config["compatibilityFile"])
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def write_journal(path: Path, journal: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(journal, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def release_chart(config, side) -> str:
    entry = config[side]
    releases = json.loads(run(
        "helm", "--kube-context", config["cluster"]["context"],
        "--namespace", entry["namespace"], "list", "--all", "-o", "json",
    ))
    return release_chart_from_list(releases, entry["releaseName"])


def consumer_installed_metadata(config):
    entry = config["consumer"]
    metadata = json.loads(run(
        "helm", "--kube-context", config["cluster"]["context"],
        "--namespace", entry["namespace"], "get", "metadata",
        entry["releaseName"], "-o", "json",
    ))
    if (metadata.get("name") != entry["releaseName"] or
            metadata.get("namespace") != entry["namespace"] or
            any(not isinstance(metadata.get(key), str) or not metadata[key]
                for key in ("appVersion", "version"))):
        raise ConfigError("installed Consumer Helm metadata is invalid")
    return {"releaseVersion": metadata["appVersion"],
            "chartVersion": metadata["version"]}


def completed_release_matches(config, side, package, operation_id, image_file) -> bool:
    entry = config[side]
    output = run("helm", "--kube-context", config["cluster"]["context"],
                 "--namespace", entry["namespace"], "status", entry["releaseName"], "-o", "json")
    status = json.loads(output)
    if status.get("info", {}).get("status") != "deployed" or not release_chart(config, side).endswith(f"-{package[1]['chartVersion']}"):
        return False
    if side == "consumer":
        actual = consumer_installed_metadata(config)
        return all(actual[key] == package[1][key]
                   for key in ("releaseVersion", "chartVersion"))
    values = json.loads(run("helm", "--kube-context", config["cluster"]["context"],
                            "--namespace", entry["namespace"], "get", "values", entry["releaseName"], "-o", "json"))
    global_values = values.get("global", {})
    expected_images = read_yaml(image_file).get("global", {}).get("resolvedImages", {})
    actual_images = global_values.get("resolvedImages", {})
    return (global_values.get("releaseVersion") == package[1]["releaseVersion"] and
            global_values.get("operationId") == operation_id and
            isinstance(expected_images, dict) and bool(expected_images) and
            isinstance(actual_images, dict) and
            all(actual_images.get(image_id) == reference
                for image_id, reference in expected_images.items()))


def consumer_release_exists(config) -> bool:
    entry = config["consumer"]
    result = run_program(("helm", "--kube-context", config["cluster"]["context"],
                             "--namespace", entry["namespace"], "status", entry["releaseName"],
                             "-o", "json"), text=True, capture_output=True)
    if result.returncode:
        if "not found" in result.stderr.lower():
            return False
        raise RuntimeError("unable to read Consumer Helm release status")
    return True


def defer_consumer_services(config) -> bool:
    if consumer_release_exists(config):
        return False
    entry = config["consumer"]
    result = run_program(("kubectl", "--context", config["cluster"]["context"],
                             "-n", entry["namespace"], "get", "services", "-o", "json"),
                            text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("unable to inspect Consumer Services before joint preflight")
    items = json.loads(result.stdout).get("items", [])
    if not isinstance(items, list):
        raise ConfigError("Consumer Service list is invalid")
    expected = {f"{entry['releaseName']}-auth", f"{entry['releaseName']}-backend"}
    existing = {item.get("metadata", {}).get("name") for item in items if isinstance(item, dict)}
    if expected & existing:
        raise ConfigError("Consumer Service exists without its Helm release; inspect cluster state")
    return True


def installed_identity(config, side):
    entry = config[side]
    status = json.loads(run("helm", "--kube-context", config["cluster"]["context"],
                            "--namespace", entry["namespace"], "status", entry["releaseName"], "-o", "json"))
    if side == "consumer":
        if status.get("info", {}).get("status") != "deployed":
            raise ConfigError("installed consumer release is not deployed")
        actual = consumer_installed_metadata(config)
        manifest = validate_compatibility_manifest(read_yaml(config["compatibilityFile"]))
        versions = {item["consumerChartVersion"] for item in manifest["combinations"]}
        if actual["chartVersion"] not in versions:
            raise CompatibilityError("installed consumer Chart version is absent in compatibility manifest")
        return actual
    values = json.loads(run("helm", "--kube-context", config["cluster"]["context"],
                            "--namespace", entry["namespace"], "get", "values", entry["releaseName"], "-o", "json"))
    if status.get("info", {}).get("status") != "deployed" or not values.get("global", {}).get("releaseVersion"):
        raise ConfigError(f"installed {side} release is not deployed with a recorded version")
    manifest = validate_compatibility_manifest(read_yaml(config["compatibilityFile"]))
    versions = {item.get(f"{side}ChartVersion") for item in manifest.get("combinations", [])
                if isinstance(item, dict) and item.get(f"{side}ChartVersion")}
    chart = release_chart(config, side)
    matches = [version for version in versions if chart.endswith(f"-{version}")]
    if len(matches) != 1:
        raise CompatibilityError(f"installed {side} Chart version is absent or ambiguous in compatibility manifest")
    return {"releaseVersion": values["global"]["releaseVersion"],
            "chartVersion": matches[0]}


def verified_joint_transition(config, packages, transition_id, installed):
    manifest = validate_compatibility_manifest(read_yaml(config["compatibilityFile"]))
    combinations = manifest.get("combinations", [])
    transitions = manifest.get("transitions", [])
    if not isinstance(combinations, list) or not isinstance(transitions, list):
        raise CompatibilityError("compatibility combinations and transitions must be lists")
    by_id = {item.get("id"): item for item in combinations if isinstance(item, dict) and item.get("id")}
    if len(by_id) != len(combinations):
        raise CompatibilityError("compatibility combination IDs must be unique")
    matches = [item for item in transitions if isinstance(item, dict) and item.get("id") == transition_id]
    if len(matches) != 1 or matches[0].get("steps") != list(JOINT_SIDE_ORDER):
        raise CompatibilityError("joint upgrade requires one Consumer-then-Modeling transition")
    item = matches[0]
    if item.get("requiresJointMaintenance") is not True:
        raise CompatibilityError("joint upgrade requires a maintained cross-side transition")
    old, new = by_id.get(item.get("from")), by_id.get(item.get("to"))
    middle = by_id.get(item.get("intermediateCombinationId"))
    if not old or not new or not middle:
        raise CompatibilityError("old, intermediate and target combinations must all be verified")
    if old["id"] == new["id"] and middle["id"] != old["id"]:
        raise CompatibilityError("same-version transition requires the same intermediate combination")
    for side in JOINT_SIDE_ORDER:
        prefix = side
        if (old.get(f"{prefix}ReleaseVersion"), old.get(f"{prefix}ChartVersion")) != (
                installed[side]["releaseVersion"], installed[side]["chartVersion"]):
            raise CompatibilityError(f"installed {side} version differs from transition start")
        target = packages[side][1]
        if (new.get(f"{prefix}ReleaseVersion"), new.get(f"{prefix}ChartVersion")) != (
                target["releaseVersion"], target["chartVersion"]):
            raise CompatibilityError(f"target {side} version differs from transition end")
        expected = new if side == "consumer" else old
        if (middle.get(f"{prefix}ReleaseVersion"), middle.get(f"{prefix}ChartVersion")) != (
                expected.get(f"{prefix}ReleaseVersion"), expected.get(f"{prefix}ChartVersion")):
            raise CompatibilityError("intermediate Consumer/Modeling combination is incorrect")
    modeling_transition(config["compatibilityFile"], transition_id, installed["modeling"], packages["modeling"][1])
    if not all(combo.get("platform") == packages["modeling"][1]["platform"] for combo in (old, middle, new)):
        raise CompatibilityError("upgrade cannot change CPU architecture")
    return {"schemaVersion": "cce-transition-record/v1", "transition": item,
            "from": old, "intermediate": middle, "to": new,
            "compatibilityFile": str(config["compatibilityFile"]),
            "compatibilitySha256": checksum(config["compatibilityFile"])}


def unique_target_transition(config: dict, target_combination: dict, installed: dict) -> str:
    manifest = validate_compatibility_manifest(read_yaml(config["compatibilityFile"]))
    installed_combinations = [item for item in manifest["combinations"]
                              if matches_installed_combination(item, installed)]
    if len(installed_combinations) != 1:
        raise CompatibilityError("installed Consumer/Modeling combination is missing or ambiguous")
    source_id = installed_combinations[0]["id"]
    matches = []
    for item in manifest["transitions"]:
        if item.get("from") != source_id or item.get("to") != target_combination["id"]:
            continue
        if item.get("steps") == list(JOINT_SIDE_ORDER):
            matches.append(item["id"])
    if len(matches) != 1:
        raise CompatibilityError("current-to-target upgrade has no unique joint transition; specify --transition-id")
    return matches[0]


class Lease:
    def __init__(self, config, side, holder):
        self.config = config
        self.side = side
        self.name = f"{config[side]['releaseName']}-cce-operation"
        self.holder = holder
        self.namespace = config[side]["namespace"]
        self.stop = threading.Event()
        self.thread = None
        self.error = None

    def _document(self, current=None):
        now = lease_timestamp(datetime.now(timezone.utc))
        document = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                    "metadata": {"name": self.name, "namespace": self.namespace},
                    "spec": {"holderIdentity": self.holder, "leaseDurationSeconds": 90,
                             "acquireTime": now, "renewTime": now}}
        if current:
            if current.get("metadata", {}).get("resourceVersion"):
                document["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
            document["spec"]["acquireTime"] = current["spec"].get("acquireTime", now)
        return json.dumps(document)

    def _get(self):
        result = run_program(("kubectl", "--context", self.config["cluster"]["context"],
                                 "-n", self.namespace, "get", "lease", self.name, "-o", "json"),
                                text=True, capture_output=True)
        if result.returncode:
            if "notfound" in result.stderr.lower():
                return None
            raise RuntimeError("cannot read operation lease")
        return json.loads(result.stdout)

    def acquire(self):
        current = self._get()
        if current:
            renewed = current["spec"].get("renewTime") or current["spec"].get("acquireTime")
            expires = datetime.fromisoformat(renewed.replace("Z", "+00:00")) + timedelta(seconds=current["spec"].get("leaseDurationSeconds", 90))
            if expires > datetime.now(timezone.utc):
                raise LockError("another CCE operation holds the lease")
        kubectl(self.config, "-n", self.namespace, "replace" if current else "create", "-f", "-",
                input_text=self._document(current))
        self.thread = threading.Thread(target=self._renew, daemon=True)
        self.thread.start()

    def _renew(self):
        while not self.stop.wait(20):
            try:
                current = self._get()
                if not current or current["spec"].get("holderIdentity") != self.holder:
                    raise RuntimeError("operation lease lost")
                kubectl(self.config, "-n", self.namespace, "replace", "-f", "-", input_text=self._document(current))
            except Exception as exc:
                self.error = exc
                return

    def check(self):
        if self.error:
            raise RuntimeError("operation lease lost") from self.error
        current = self._get()
        if not current or current.get("spec", {}).get("holderIdentity") != self.holder:
            raise LockError("operation lease lost")
        spec = current["spec"]
        try:
            renewed = datetime.fromisoformat(spec["renewTime"].replace("Z", "+00:00"))
            expires = renewed + timedelta(seconds=int(spec["leaseDurationSeconds"]))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise LockError("operation lease expiry is invalid") from exc
        if expires <= datetime.now(timezone.utc):
            raise LockError("operation lease expired")

    def release(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        current = self._get()
        if current and current["spec"].get("holderIdentity") == self.holder:
            released = self._document(current)
            document = json.loads(released)
            document["spec"].update(holderIdentity="", renewTime="1970-01-01T00:00:00.000000Z",
                                    leaseDurationSeconds=1)
            kubectl(self.config, "-n", self.namespace, "replace", "-f", "-",
                    input_text=json.dumps(document))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare-compatibility", "plan", "preflight", "install", "upgrade", "status", "verify"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scope", choices=("consumer", "modeling", "both"))
    parser.add_argument("--resume")
    parser.add_argument("--backup-record", help="optional existing Modeling backup record; otherwise snapshots are created automatically")
    parser.add_argument("--transition-id")
    parser.add_argument("--snapshot-retention", type=int, default=2)
    parser.add_argument("--min-free-percent", type=float, default=15.0)
    parser.add_argument("--upgrade-candidate", action="store_true",
                        help="derive old, intermediate and target combinations for a joint upgrade")
    parser.add_argument("--confirm-verified", action="store_true",
                        help="write only after the proposed combination has been jointly verified")
    parser.add_argument("--confirm-no-modeling-migration", action="store_true",
                        help="confirm target Modeling reads existing data without migration")
    args = parser.parse_args()
    preparing_compatibility = args.action == "prepare-compatibility"
    has_compatibility_options = args.upgrade_candidate or args.confirm_verified or args.confirm_no_modeling_migration
    if not preparing_compatibility and has_compatibility_options:
        parser.error("compatibility preparation options require prepare-compatibility")
    if args.confirm_no_modeling_migration and not args.upgrade_candidate:
        parser.error("--confirm-no-modeling-migration requires --upgrade-candidate")
    if args.action == "upgrade" and not args.scope:
        parser.error("upgrade requires --scope")
    if args.action == "upgrade" and args.scope != "both":
        parser.error("this joint entry currently supports only --scope both for upgrade")
    if args.snapshot_retention < 1:
        parser.error("--snapshot-retention must be at least 1")
    if not 0 <= args.min_free_percent <= 100:
        parser.error("--min-free-percent must be between 0 and 100")
    if args.resume and args.action not in ("install", "upgrade"):
        parser.error("--resume is only supported for install or upgrade")
    operation_id = args.resume or str(uuid.uuid4())
    leases = []
    result = {"schemaVersion": "cce-joint-operation/v1", "operationId": operation_id,
              "action": args.action, "status": "failed", "phase": "input", "errorCode": None,
              "message": "", "results": {}}
    try:
        config = load_config(args.config.resolve())
        if args.action == "prepare-compatibility":
            result = prepare_compatibility(config, upgrade=args.upgrade_candidate,
                                           confirmed=args.confirm_verified,
                                           no_migration_confirmed=args.confirm_no_modeling_migration)
            emit_stdout(json.dumps(result, ensure_ascii=False))
            return 0
        state = config["stateDir"]
        state.mkdir(parents=True, exist_ok=True)
        if args.action == "status":
            for side in JOINT_SIDE_ORDER:
                entry = config[side]
                output = run("helm", "--kube-context", config["cluster"]["context"],
                             "--namespace", entry["namespace"], "status", entry["releaseName"], "-o", "json")
                current = json.loads(output)
                result["results"][side] = {"releaseName": entry["releaseName"],
                    "namespace": entry["namespace"], "chart": release_chart(config, side),
                    "status": current.get("info", {}).get("status")}
            result.update(status="succeeded", phase="status", message="completed")
            emit_stdout(json.dumps(result, ensure_ascii=False))
            return 0
        with tempfile.TemporaryDirectory(prefix="cce-packages-", dir=state) as temporary:
            packages = {}
            for side in JOINT_SIDE_ORDER:
                root = safe_extract(config[side]["package"], Path(temporary), side)
                packages[side] = root, validate_release(root, side)
            combination = validate_combination(config, packages)
            if args.action == "upgrade":
                installed_for_transition = {
                    side: installed_identity(config, side) for side in JOINT_SIDE_ORDER
                }
                args.transition_id = args.transition_id or unique_target_transition(
                    config, combination, installed_for_transition,
                )
                result["transitionId"] = args.transition_id
            consumer_values = read_yaml(config["consumer"]["values"])
            modeling_values = read_yaml(config["modeling"]["values"])
            validate_cross_side(config, packages, consumer_values, modeling_values, combination)
            if consumer_values.get("global", {}).get("clusterFlavor") != modeling_values.get("global", {}).get("clusterFlavor"):
                raise ConfigError("clusterFlavor differs between releases")
            consumer_access = consumer_values.get("global", {}).get("access", {})
            modeling_access = modeling_values.get("global", {}).get("access", {})
            if consumer_access.get("elb", {}).get("id") == modeling_access.get("elb", {}).get("id"):
                def ports(access):
                    listeners = access.get("listeners") or []
                    return {item.get("listenerPort") for item in listeners} or {access.get("listenerPort")}
                if ports(consumer_access) & ports(modeling_access):
                    raise ConfigError("both releases claim the same ELB listener")
            model_images = effective_images(config, state, "consumer", operation_id)
            image_files = {"consumer": config["consumer"]["resolvedImages"], "modeling": model_images}
            journal_path = state / f"{operation_id}.json"
            journal = None
            transition_record = None
            transition_record_path = state / f"{operation_id}-transition.json"
            if args.action in ("install", "upgrade"):
                fingerprint = operation_fingerprint(config)
                if args.action == "upgrade":
                    backup_identity = (checksum(Path(args.backup_record)) if args.backup_record else
                                       f"auto:{args.snapshot_retention}:{args.min_free_percent}")
                    fingerprint = hashlib.sha256((fingerprint + args.transition_id +
                        backup_identity).encode()).hexdigest()
                if args.resume:
                    if not journal_path.is_file():
                        raise ConfigError("resume operation journal missing")
                    journal = json.loads(journal_path.read_text())
                    if journal.get("fingerprint") != fingerprint or journal.get("action") != args.action:
                        raise ConfigError("resume inputs differ from original operation")
                    if args.action == "upgrade":
                        installed = journal.get("installed")
                        if not isinstance(installed, dict):
                            raise ConfigError("upgrade journal lacks the original installed versions")
                        transition_record = verified_joint_transition(config, packages, args.transition_id, installed)
                        if isinstance(transition_record, dict):
                            transition_record_path.write_text(json.dumps(transition_record, sort_keys=True), encoding="utf-8")
                        for side in JOINT_SIDE_ORDER:
                            current = installed_identity(config, side)
                            expected = packages[side][1] if side in journal.get("completed", []) else installed[side]
                            if (current["releaseVersion"], current["chartVersion"]) != (
                                    expected["releaseVersion"], expected["chartVersion"]):
                                raise ConfigError(f"{side} actual version differs from the upgrade journal")
                else:
                    if journal_path.exists():
                        raise ConfigError("operation ID already exists")
                    journal = {"operationId": operation_id, "action": args.action, "fingerprint": fingerprint,
                               "completed": [], "phase": "prepared"}
                    if args.action == "upgrade":
                        installed = {side: installed_identity(config, side) for side in JOINT_SIDE_ORDER}
                        transition_record = verified_joint_transition(config, packages, args.transition_id, installed)
                        if isinstance(transition_record, dict):
                            transition_record_path.write_text(json.dumps(transition_record, sort_keys=True), encoding="utf-8")
                        journal["installed"] = installed
                    write_journal(journal_path, journal)
            if args.action in ("install", "upgrade"):
                lock_identities = sorted((config[side]["namespace"], config[side]["releaseName"], side)
                                         for side in JOINT_SIDE_ORDER)
                for _, _, side in lock_identities:
                    current_lease = Lease(config, side, operation_id)
                    current_lease.acquire()
                    leases.append(current_lease)
            if args.action == "upgrade":
                if "consumer" not in journal["completed"] and journal.get("phase") != "consumer-started":
                    invoke("consumer", "preflight", config, packages["consumer"], operation_id,
                           image_files["consumer"], lock_holder=operation_id)
                if "modeling" not in journal["completed"] and journal.get("phase") != "modeling-started":
                    invoke("modeling", "preflight", config, packages["modeling"], operation_id,
                           image_files["modeling"], backup_record=args.backup_record or "",
                           transition_id=args.transition_id if args.backup_record else "",
                           lock_holder=operation_id,
                           transition_record=transition_record_path if args.backup_record else "")
            defer_external_services = False
            for side in JOINT_SIDE_ORDER:
                side_package = packages.get(side)
                side_images = image_files.get(side)
                if side_package is None or side_images is None:
                    raise ConfigError(f"{side} package or image mapping is missing")
                for current_lease in leases:
                    current_lease.check()
                result["phase"] = side
                if journal and side in journal.get("completed", []):
                    if not completed_release_matches(config, side, side_package, operation_id, side_images):
                        raise ConfigError(f"completed {side} release no longer matches operation journal")
                    result["results"][side] = {"status": "succeeded", "phase": "already-completed"}
                    continue
                if journal:
                    if args.action == "upgrade" and journal.get("phase") == f"{side}-started":
                        raise ConfigError(f"{side} upgrade outcome is uncertain; inspect actual state before retry")
                if args.action == "preflight" and side == "modeling":
                    defer_external_services = defer_consumer_services(config)
                if args.action == "install" and side == "modeling":
                    invoke("modeling", "preflight", config, packages["modeling"], operation_id,
                           image_files["modeling"], lock_holder=operation_id)
                if journal:
                    journal["phase"] = f"{side}-started"
                    write_journal(journal_path, journal)
                result["results"][side] = invoke(side, args.action, config, side_package, operation_id,
                    side_images, args.resume if args.action == "install" else "",
                    args.backup_record if side == "modeling" and args.backup_record else "",
                    args.transition_id or "", operation_id if args.action in ("install", "upgrade") else "",
                    transition_record_path if side == "modeling" and args.action == "upgrade" else "",
                    defer_external_services=defer_external_services and side == "modeling",
                    auto_backup=side == "modeling" and args.action == "upgrade" and not args.backup_record,
                    snapshot_retention=args.snapshot_retention,
                    min_free_percent=args.min_free_percent)
                if args.action == "upgrade":
                    for current_lease in leases:
                        current_lease.check()
                    invoke(side, "verify", config, side_package, operation_id, side_images,
                           lock_holder=operation_id)
                if journal:
                    journal.setdefault("completed", []).append(side)
                    journal["phase"] = f"{side}-completed"
                    write_journal(journal_path, journal)
            result.update(status="succeeded", message="completed")
    except (ConfigError, RuntimeError, OSError, subprocess.TimeoutExpired, KeyError, json.JSONDecodeError) as exc:
        if isinstance(exc, CompatibilityError):
            code = "INCOMPATIBLE_VERSION"
        elif isinstance(exc, LockError):
            code = "LOCK_CONFLICT"
        elif isinstance(exc, MigrationUnavailable):
            code = "MIGRATION_FAILED"
        elif isinstance(exc, subprocess.TimeoutExpired):
            code = "TIMEOUT"
        elif isinstance(exc, ConfigError):
            code = "CONFIG_INVALID"
        else:
            code = "RESOURCE_NOT_READY"
        result.update(errorCode=code, message=str(exc))
    finally:
        for current_lease in reversed(leases):
            try:
                current_lease.release()
            except Exception:
                result.update(status="failed", errorCode="LOCK_CONFLICT", message="could not release operation lease")
    emit_stdout(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
