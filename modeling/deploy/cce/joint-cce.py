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
                    checksum, read_yaml, safe_extract, validate_name,
                    validate_release, validate_compatibility_manifest, IMAGE_REF)
from upgrade import transition as modeling_transition

JOINT_SIDE_ORDER = ("consumer", "modeling")


def run(*command, input_text=None):
    result = subprocess.run(command, input=input_text, text=True, capture_output=True)
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
    matched = [item for item in combinations if isinstance(item, dict) and
               all(item.get(key) == value for key, value in expected.items())]
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


def invoke(side, action, config, package, operation_id, image_file, resume="", backup_record="", transition_id="", lock_holder="", transition_record=""):
    script = package[0] / "deploy/install-cce.sh"
    if not script.is_file():
        raise ConfigError(f"{side} package has no deploy/install-cce.sh")
    options = ["bash", str(script), action, "--context", config["cluster"]["context"],
               "--namespace", config[side]["namespace"], "--release", config[side]["releaseName"],
               "--values", str(config[side]["values"]), "--resolved-images", str(image_file),
               "--operation-id", operation_id,
               "--state-dir", str(config["stateDir"]), "--timeout-seconds", str(config.get("timeoutSeconds", 900))]
    if resume:
        options.extend(("--resume", resume))
    if backup_record and side == "modeling":
        options.extend(("--backup-record", backup_record))
    if transition_id and side == "modeling":
        options.extend(("--transition-id", transition_id))
    if transition_record and side == "modeling":
        options.extend(("--transition-record", str(transition_record)))
    if lock_holder:
        options.extend(("--lock-holder", lock_holder))
    result = subprocess.run(options, text=True, capture_output=True, timeout=config.get("timeoutSeconds", 900) + 30)
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


def completed_release_matches(config, side, package) -> bool:
    entry = config[side]
    output = run("helm", "--kube-context", config["cluster"]["context"],
                 "--namespace", entry["namespace"], "status", entry["releaseName"], "-o", "json")
    status = json.loads(output)
    if status.get("info", {}).get("status") != "deployed" or not str(status.get("chart", "")).endswith(f"-{package[1]['chartVersion']}"):
        return False
    values = json.loads(run("helm", "--kube-context", config["cluster"]["context"],
                            "--namespace", entry["namespace"], "get", "values", entry["releaseName"], "-o", "json"))
    return values.get("global", {}).get("releaseVersion") == package[1]["releaseVersion"]


def installed_identity(config, side):
    entry = config[side]
    status = json.loads(run("helm", "--kube-context", config["cluster"]["context"],
                            "--namespace", entry["namespace"], "status", entry["releaseName"], "-o", "json"))
    values = json.loads(run("helm", "--kube-context", config["cluster"]["context"],
                            "--namespace", entry["namespace"], "get", "values", entry["releaseName"], "-o", "json"))
    if status.get("info", {}).get("status") != "deployed" or not values.get("global", {}).get("releaseVersion"):
        raise ConfigError(f"installed {side} release is not deployed with a recorded version")
    manifest = validate_compatibility_manifest(read_yaml(config["compatibilityFile"]))
    versions = {item.get(f"{side}ChartVersion") for item in manifest.get("combinations", [])
                if isinstance(item, dict) and item.get(f"{side}ChartVersion")}
    chart = str(status.get("chart", ""))
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
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
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
        result = subprocess.run(("kubectl", "--context", self.config["cluster"]["context"],
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
            document["spec"].update(holderIdentity="", renewTime="1970-01-01T00:00:00Z",
                                    leaseDurationSeconds=1)
            kubectl(self.config, "-n", self.namespace, "replace", "-f", "-",
                    input_text=json.dumps(document))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "preflight", "install", "upgrade", "status", "verify"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scope", choices=("consumer", "modeling", "both"))
    parser.add_argument("--resume")
    parser.add_argument("--backup-record", help="customer-managed Modeling backup record")
    parser.add_argument("--transition-id")
    args = parser.parse_args()
    if args.action == "upgrade" and not args.scope:
        parser.error("upgrade requires --scope")
    if args.action == "upgrade" and args.scope != "both":
        parser.error("this joint entry currently supports only --scope both for upgrade")
    if args.action == "upgrade" and (not args.transition_id or not args.backup_record):
        parser.error("upgrade requires --transition-id and --backup-record")
    if args.resume and args.action not in ("install", "upgrade"):
        parser.error("--resume is only supported for install or upgrade")
    operation_id = args.resume or str(uuid.uuid4())
    leases = []
    result = {"schemaVersion": "cce-joint-operation/v1", "operationId": operation_id,
              "action": args.action, "status": "failed", "phase": "input", "errorCode": None,
              "message": "", "results": {}}
    try:
        config = load_config(args.config.resolve())
        state = config["stateDir"]
        state.mkdir(parents=True, exist_ok=True)
        if args.action == "status":
            for side in JOINT_SIDE_ORDER:
                entry = config[side]
                output = run("helm", "--kube-context", config["cluster"]["context"],
                             "--namespace", entry["namespace"], "status", entry["releaseName"], "-o", "json")
                current = json.loads(output)
                result["results"][side] = {"releaseName": entry["releaseName"],
                    "namespace": entry["namespace"], "chart": current.get("chart"),
                    "status": current.get("info", {}).get("status")}
            result.update(status="succeeded", phase="status", message="completed")
            print(json.dumps(result, ensure_ascii=False))
            return 0
        with tempfile.TemporaryDirectory(prefix="cce-packages-", dir=state) as temporary:
            packages = {}
            for side in JOINT_SIDE_ORDER:
                root = safe_extract(config[side]["package"], Path(temporary), side)
                packages[side] = root, validate_release(root, side)
            combination = validate_combination(config, packages)
            consumer_values = read_yaml(config["consumer"]["values"])
            modeling_values = read_yaml(config["modeling"]["values"])
            validate_cross_side(config, packages, consumer_values, modeling_values, combination)
            if consumer_values.get("global", {}).get("clusterFlavor") != modeling_values.get("global", {}).get("clusterFlavor"):
                raise ConfigError("clusterFlavor differs between releases")
            if (consumer_values.get("global", {}).get("access", {}).get("elb", {}).get("id") ==
                modeling_values.get("global", {}).get("access", {}).get("elb", {}).get("id") and
                consumer_values.get("global", {}).get("access", {}).get("listenerPort") ==
                modeling_values.get("global", {}).get("access", {}).get("listenerPort")):
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
                    fingerprint = hashlib.sha256((fingerprint + args.transition_id +
                        checksum(Path(args.backup_record))).encode()).hexdigest()
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
                           image_files["modeling"], backup_record=args.backup_record,
                           transition_id=args.transition_id, lock_holder=operation_id,
                           transition_record=transition_record_path)
            for side in JOINT_SIDE_ORDER:
                for current_lease in leases:
                    current_lease.check()
                result["phase"] = side
                if journal and side in journal["completed"]:
                    if not completed_release_matches(config, side, packages[side]):
                        raise ConfigError(f"completed {side} release no longer matches operation journal")
                    result["results"][side] = {"status": "succeeded", "phase": "already-completed"}
                    continue
                if journal:
                    if args.action == "upgrade" and journal.get("phase") == f"{side}-started":
                        raise ConfigError(f"{side} upgrade outcome is uncertain; inspect actual state before retry")
                    journal["phase"] = f"{side}-started"
                    write_journal(journal_path, journal)
                result["results"][side] = invoke(side, args.action, config, packages[side], operation_id,
                    image_files[side], args.resume if args.action == "install" else "",
                    (args.backup_record if side == "modeling" else "") or "",
                    args.transition_id or "", operation_id if args.action in ("install", "upgrade") else "",
                    transition_record_path if side == "modeling" and args.action == "upgrade" else "")
                if args.action == "upgrade":
                    for current_lease in leases:
                        current_lease.check()
                    invoke(side, "verify", config, packages[side], operation_id, image_files[side])
                if journal:
                    journal["completed"].append(side)
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
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
