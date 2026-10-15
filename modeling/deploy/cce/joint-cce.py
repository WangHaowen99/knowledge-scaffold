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
                    validate_release, IMAGE_REF)


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
    for side in ("consumer", "modeling"):
        validate_name(raw[side]["releaseName"], "releaseName")
        validate_name(raw[side]["namespace"], "namespace")
        for field in ("package", "values", "resolvedImages"):
            raw[side][field] = absolute(path.parent, raw[side][field])
    raw["stateDir"] = absolute(path.parent, raw["stateDir"])
    raw["compatibilityFile"] = absolute(path.parent, raw["compatibilityFile"])
    return raw


def validate_combination(config, packages):
    document = read_yaml(config["compatibilityFile"])
    if document.get("schemaVersion") != "cce-compatibility/v1":
        raise ConfigError("invalid compatibility schema")
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
    if len(matched) != 1 or any(not matched[0].get(key) for key in ("id", "authApiVersion", "consumerApiVersion")):
        raise CompatibilityError("exactly one verified Consumer/Modeling combination required")
    return matched[0]


def validate_cross_side(config, packages, consumer_values, modeling_values):
    auth_a = consumer_values.get("global", {}).get("auth", {})
    auth_b = modeling_values.get("global", {}).get("auth", {})
    for key in ("issuer", "audience"):
        if not auth_a.get(key) or auth_a.get(key) != auth_b.get(key):
            raise ConfigError(f"shared auth {key} differs between releases")
    endpoints = read_yaml(packages["consumer"][0] / "deploy/endpoints.yaml")
    if endpoints.get("schemaVersion") != "cce-endpoints/v1":
        raise ConfigError("invalid Consumer endpoint schema")
    by_id = {item["id"]: item for item in endpoints["endpoints"]}
    for field, endpoint_id in (("externalAuth", "auth"), ("externalConsumer", "consumer-api")):
        item = by_id.get(endpoint_id)
        if not item:
            raise ConfigError(f"Consumer endpoint {endpoint_id} missing")
        reference = modeling_values.get(field, {})
        expected_name = f"{config['consumer']['releaseName']}-{item['serviceNameSuffix']}"
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


def invoke(side, action, config, package, operation_id, image_file, resume="", backup_record=""):
    script = package[0] / "deploy/install-cce.sh"
    if not script.is_file():
        raise ConfigError(f"{side} package has no deploy/install-cce.sh")
    options = ["bash", str(script), action, "--context", config["cluster"]["context"],
               "--namespace", config[side]["namespace"], "--release", config[side]["releaseName"],
               "--values", str(config[side]["values"]), "--resolved-images", str(image_file),
               "--compatibility", str(config["compatibilityFile"]), "--operation-id", operation_id,
               "--state-dir", str(config["stateDir"]), "--timeout-seconds", str(config.get("timeoutSeconds", 900))]
    if resume:
        options.extend(("--resume", resume))
    if backup_record:
        options.extend(("--backup-record", backup_record))
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
    for side in ("consumer", "modeling"):
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
    return (status.get("info", {}).get("status") == "deployed" and
            str(status.get("chart", "")).endswith(f"-{package[1]['chartVersion']}"))


class Lease:
    def __init__(self, config, name, holder):
        self.config = config
        self.name = name
        self.holder = holder
        self.namespace = config["consumer"]["namespace"]
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
            if current["spec"].get("holderIdentity") != self.holder and expires > datetime.now(timezone.utc):
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

    def release(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        current = self._get()
        if current and current["spec"].get("holderIdentity") == self.holder:
            kubectl(self.config, "-n", self.namespace, "delete", "lease", self.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "preflight", "install", "upgrade", "status", "verify"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scope", choices=("consumer", "modeling", "both"))
    parser.add_argument("--resume")
    parser.add_argument("--backup-record")
    args = parser.parse_args()
    if args.action == "upgrade" and not args.scope:
        parser.error("upgrade requires --scope")
    if args.resume and args.action != "install":
        parser.error("--resume is only supported for install")
    operation_id = args.resume or str(uuid.uuid4())
    lease = None
    result = {"schemaVersion": "cce-joint-operation/v1", "operationId": operation_id,
              "action": args.action, "status": "failed", "phase": "input", "errorCode": None,
              "message": "", "results": {}}
    try:
        config = load_config(args.config.resolve())
        state = config["stateDir"]
        state.mkdir(parents=True, exist_ok=True)
        if args.action == "upgrade":
            raise MigrationUnavailable("joint CCE upgrade requires both sides' verified data migration workflows; not implemented")
        if args.action == "status":
            for side in ("consumer", "modeling"):
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
            for side in ("consumer", "modeling"):
                root = safe_extract(config[side]["package"], Path(temporary), side)
                packages[side] = root, validate_release(root, side)
            validate_combination(config, packages)
            consumer_values = read_yaml(config["consumer"]["values"])
            modeling_values = read_yaml(config["modeling"]["values"])
            validate_cross_side(config, packages, consumer_values, modeling_values)
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
            if args.action == "install":
                fingerprint = operation_fingerprint(config)
                if args.resume:
                    if not journal_path.is_file():
                        raise ConfigError("resume operation journal missing")
                    journal = json.loads(journal_path.read_text())
                    if journal.get("fingerprint") != fingerprint:
                        raise ConfigError("resume inputs differ from original install")
                else:
                    if journal_path.exists():
                        raise ConfigError("operation ID already exists")
                    journal = {"operationId": operation_id, "fingerprint": fingerprint, "completed": []}
                    write_journal(journal_path, journal)
            if args.action in ("install", "upgrade"):
                lock_name = f"cce-{config['consumer']['releaseName']}-{config['modeling']['releaseName']}"[:63].rstrip("-")
                lease = Lease(config, lock_name, operation_id)
                lease.acquire()
            for side in ("consumer", "modeling"):
                if lease:
                    lease.check()
                result["phase"] = side
                if journal and side in journal["completed"]:
                    if not completed_release_matches(config, side, packages[side]):
                        raise ConfigError(f"completed {side} release no longer matches operation journal")
                    result["results"][side] = {"status": "succeeded", "phase": "already-completed"}
                    continue
                result["results"][side] = invoke(side, args.action, config, packages[side], operation_id,
                    image_files[side], args.resume or "", args.backup_record or "")
                if journal:
                    journal["completed"].append(side)
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
        if lease:
            try:
                lease.release()
            except Exception:
                result.update(status="failed", errorCode="LOCK_CONFLICT", message="could not release operation lease")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
