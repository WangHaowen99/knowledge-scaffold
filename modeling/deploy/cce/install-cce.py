#!/usr/bin/env python3
"""Modeling-side CCE release operations. Secrets are read by Kubernetes only."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

import jsonschema

from common import (CompatibilityError, ConfigError, checksum, endpoint_results, read_yaml,
                    release_chart_from_list, validate_images, validate_name, validate_release)
from upgrade import (DATA_COUNTS, assert_same_bindings, bindings, transition,
                     verified_snapshots, verify_persisted_data)


class OperationLockError(ConfigError):
    """The Modeling release operation Lease is held by another operation."""


class OperationLease:
    """A short-lived Kubernetes Lease for a standalone Modeling upgrade.

    Joint upgrades create and renew this Lease in joint-cce.py.  Standalone
    upgrades use the same Lease name and lifecycle here so both entry points
    serialize changes to the Modeling release.
    """

    def __init__(self, args, holder: str):
        self.args = args
        self.holder = holder
        self.name = f"{args.release}-cce-operation"
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.error: Exception | None = None

    def _get(self) -> dict | None:
        result = subprocess.run(
            ("kubectl", "--context", self.args.context, "-n", self.args.namespace,
             "get", "lease", self.name, "-o", "json"),
            text=True, capture_output=True,
        )
        if result.returncode:
            if "notfound" in result.stderr.lower() or "not found" in result.stderr.lower():
                return None
            raise RuntimeError(f"unable to read operation Lease: {result.stderr.strip()[:300]}")
        return json.loads(result.stdout)

    def _document(self, current: dict | None = None) -> str:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        document = {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {"name": self.name, "namespace": self.args.namespace},
            "spec": {
                "holderIdentity": self.holder,
                "leaseDurationSeconds": 90,
                "acquireTime": now,
                "renewTime": now,
            },
        }
        if current:
            resource_version = current.get("metadata", {}).get("resourceVersion")
            if resource_version:
                document["metadata"]["resourceVersion"] = resource_version
            document["spec"]["acquireTime"] = current.get("spec", {}).get("acquireTime", now)
        return json.dumps(document)

    @staticmethod
    def _active(current: dict) -> bool:
        spec = current.get("spec", {})
        renewed = spec.get("renewTime") or spec.get("acquireTime")
        if not renewed:
            return False
        try:
            timestamp = datetime.fromisoformat(renewed.replace("Z", "+00:00"))
            duration = int(spec.get("leaseDurationSeconds", 90))
        except (TypeError, ValueError):
            return False
        return timestamp + timedelta(seconds=duration) > datetime.now(timezone.utc)

    def acquire(self) -> None:
        current = self._get()
        if current and self._active(current):
            owner = current.get("spec", {}).get("holderIdentity") or "unknown"
            if owner != self.holder:
                raise OperationLockError(f"Modeling operation Lease is held by {owner}")
        action = "replace" if current else "create"
        result = subprocess.run(
            ("kubectl", "--context", self.args.context, "-n", self.args.namespace,
             action, "-f", "-"),
            input=self._document(current), text=True, capture_output=True,
        )
        if result.returncode:
            raise OperationLockError(f"unable to acquire Modeling operation Lease: {result.stderr.strip()[:300]}")
        self.thread = threading.Thread(target=self._renew, daemon=True)
        self.thread.start()

    def _renew(self) -> None:
        while not self.stop.wait(20):
            try:
                current = self._get()
                if not current or current.get("spec", {}).get("holderIdentity") != self.holder:
                    raise OperationLockError("Modeling operation Lease was lost")
                result = subprocess.run(
                    ("kubectl", "--context", self.args.context, "-n", self.args.namespace,
                     "replace", "-f", "-"),
                    input=self._document(current), text=True, capture_output=True,
                )
                if result.returncode:
                    raise OperationLockError("unable to renew Modeling operation Lease")
            except Exception as exc:  # pragma: no cover - timing-dependent worker path
                self.error = exc
                return

    def check(self) -> None:
        if self.error:
            raise OperationLockError("Modeling operation Lease was lost") from self.error
        current = self._get()
        if not current or current.get("spec", {}).get("holderIdentity") != self.holder:
            raise OperationLockError("Modeling operation Lease is not held by this operation")
        if not self._active(current):
            raise OperationLockError("Modeling operation Lease has expired")

    def release(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        try:
            current = self._get()
            if current and current.get("spec", {}).get("holderIdentity") == self.holder:
                document = json.loads(self._document(current))
                document["spec"].update(
                    holderIdentity="", renewTime="1970-01-01T00:00:00Z", leaseDurationSeconds=1,
                )
                subprocess.run(
                    ("kubectl", "--context", self.args.context, "-n", self.args.namespace,
                     "replace", "-f", "-"),
                    input=json.dumps(document), text=True, capture_output=True,
                )
        except Exception:
            # A failed cleanup must not hide the original upgrade result.
            pass


def run(*command: str) -> str:
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{command[0]} {command[1] if len(command) > 1 else ''} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def merge(a: dict, b: dict) -> dict:
    result = dict(a)
    for key, value in b.items():
        result[key] = merge(result[key], value) if key in result and isinstance(result[key], dict) and isinstance(value, dict) else value
    return result


def helm_base(args) -> tuple[str, ...]:
    return ("helm", "--kube-context", args.context, "--namespace", args.namespace)


def kube(args, *rest: str) -> dict:
    return json.loads(run("kubectl", "--context", args.context, *rest, "-o", "json"))


def release_status(args) -> dict | None:
    result = subprocess.run((*helm_base(args), "status", args.release, "-o", "json"), text=True, capture_output=True)
    if result.returncode:
        if "not found" in result.stderr.lower():
            return None
        raise RuntimeError("unable to read Helm release status")
    return json.loads(result.stdout)


def installed_values(args) -> dict:
    return json.loads(run(*helm_base(args), "get", "values", args.release, "-o", "json"))


def release_chart(args) -> str:
    releases = json.loads(run(*helm_base(args), "list", "--all", "-o", "json"))
    return release_chart_from_list(releases, args.release)


def installed_release(args) -> dict:
    current = release_status(args)
    if not current or current.get("info", {}).get("status") != "deployed":
        raise ConfigError("installed Modeling release is not deployed")
    chart = release_chart(args)
    if not chart.startswith("modeling-"):
        raise ConfigError("installed release is not a Modeling Chart")
    version = installed_values(args).get("global", {}).get("releaseVersion")
    if not version:
        raise ConfigError("installed release version is missing")
    return {"releaseVersion": version, "chartVersion": chart.removeprefix("modeling-")}


def load_inputs(args, root: Path):
    release = validate_release(root, "modeling")
    if not args.values or not args.resolved_images:
        raise ConfigError("values and resolved-images are required")
    values = read_yaml(args.values)
    mapping = read_yaml(args.resolved_images)
    images = validate_images(values, mapping, release)
    defaults = read_yaml(root / "deploy/chart/values.yaml")
    merged = merge(merge(defaults, values), mapping)
    schema = json.loads((root / "deploy/chart/values.schema.json").read_text())
    try:
        jsonschema.validate(merged, schema)
    except jsonschema.ValidationError as exc:
        raise ConfigError(f"values schema invalid at {'.'.join(map(str, exc.path))}: {exc.message}") from exc
    if merged["global"]["access"]["protocol"] == "HTTPS" and not merged["global"]["access"]["tlsSecretName"]:
        raise ConfigError("HTTPS requires tlsSecretName")
    access = merged["global"]["access"]
    if access["mode"] == "host" and access["listeners"]:
        raise ConfigError("host access mode cannot define listeners")
    if access["mode"] == "host" and not access["routes"]:
        raise ConfigError("host access mode requires routes")
    if access["mode"] == "port":
        listeners = access["listeners"]
        if not listeners:
            raise ConfigError("port access mode requires listeners")
        if access["routes"]:
            raise ConfigError("port access mode cannot define routes")
        ids = [item["id"] for item in listeners]
        ports = [item["listenerPort"] for item in listeners]
        endpoints = [item["endpointId"] for item in listeners]
        if len(set(ids)) != len(ids) or len(set(ports)) != len(ports) or len(set(endpoints)) != len(endpoints):
            raise ConfigError("port access listeners require unique IDs, ports and endpoints")
        for item in listeners:
            if item["protocol"] == "HTTPS" and not item["tlsSecretName"]:
                raise ConfigError(f"HTTPS listener {item['id']} requires tlsSecretName")
    if merged["modeling"]["workloads"]["core"]["nodeSelector"].keys() & merged["modeling"]["workloads"]["jiuwenbox"]["nodeSelector"].keys():
        raise ConfigError("core and JiuwenBox nodeSelector keys must not overlap")
    for key in ("llmApiKey", "evalDbUrl", "cleanupToken", "anthropicAuthToken"):
        secret = merged["modeling"]["secrets"][key]
        if bool(secret["name"]) != bool(secret["key"]):
            raise ConfigError(f"incomplete {key} Secret reference")
    if merged["modeling"]["nl2sql"]["enabled"] and not merged["modeling"]["secrets"]["evalDbUrl"]["name"]:
        raise ConfigError("NL2SQL requires the evalDbUrl Secret reference")
    if args.action != "upgrade" and any(merged["modeling"]["workloads"][name]["replicas"] != 1
            for name in ("core", "agent", "onto")):
        raise ConfigError("stateful workloads must run at one replica")
    if not images["jiuwenbox"]:
        raise ConfigError("JiuwenBox reference missing")
    return release, merged


def preflight(args, merged: dict, *, check_external_services: bool = True) -> None:
    namespace = kube(args, "get", "namespace", args.namespace)
    if namespace["status"]["phase"] != "Active":
        raise ConfigError("namespace is not Active")
    for workload in ("core", "agent", "onto"):
        name = merged["modeling"]["workloads"][workload]["persistence"]["existingClaim"]
        pvc = kube(args, "get", "pvc", name, "-n", args.namespace)
        spec = pvc["spec"]
        if pvc["status"]["phase"] != "Bound" or spec.get("volumeMode", "Filesystem") != "Filesystem":
            raise ConfigError(f"PVC for {workload} is not Bound Filesystem")
        if "ReadWriteOnce" not in spec.get("accessModes", []):
            raise ConfigError(f"PVC for {workload} lacks ReadWriteOnce")
    refs = [merged["global"]["integration"]["serviceTokenSecret"]]
    refs.extend(merged["modeling"]["secrets"].values())
    for item in refs:
        if item["name"]:
            secret = kube(args, "get", "secret", item["name"], "-n", args.namespace)
            if item["key"] not in secret.get("data", {}):
                raise ConfigError(f"Secret key missing in {item['name']}")
    for secret_name in merged["global"]["imageDelivery"]["pullSecretNames"]:
        kube(args, "get", "secret", secret_name, "-n", args.namespace)
    if merged["global"]["access"]["protocol"] == "HTTPS":
        kube(args, "get", "secret", merged["global"]["access"]["tlsSecretName"], "-n", args.namespace)
    if check_external_services:
        for field in ("externalAuth", "externalConsumer"):
            target = merged[field]
            service = kube(args, "get", "service", target["serviceName"], "-n", target["namespace"])
            if target["servicePort"] not in [port["port"] for port in service["spec"]["ports"]]:
                raise ConfigError(f"{field} service port mismatch")
    nodes = kube(args, "get", "nodes")
    arch = merged["global"]["targetArch"]
    if not any(node["metadata"]["labels"].get("kubernetes.io/arch") == arch and
               not node["spec"].get("unschedulable", False) for node in nodes["items"]):
        raise ConfigError(f"no schedulable {arch} node")


def render(args, root: Path, release: dict) -> Path:
    args.state_dir.mkdir(parents=True, exist_ok=True)
    output = args.state_dir / f"{args.release}-{args.operation_id}-modeling-rendered.yaml"
    manifest = run(*helm_base(args), "template", args.release, str(root / "deploy/chart"),
                   "-f", str(args.values), "-f", str(args.resolved_images),
                   "--set-string", f"global.releaseVersion={release['releaseVersion']}",
                   "--set-string", f"global.operationId={args.operation_id or ''}")
    output.write_text(manifest, encoding="utf-8")
    return output


def verify(args, root: Path):
    for component in ("agent", "core", "onto"):
        run("kubectl", "--context", args.context, "-n", args.namespace, "rollout", "status",
            f"deployment/{args.release}-{component}", f"--timeout={args.timeout_seconds}s")
    run("kubectl", "--context", args.context, "-n", args.namespace, "rollout", "status",
        f"deployment/{args.release}-sandbox-gateway", f"--timeout={args.timeout_seconds}s")
    run("kubectl", "--context", args.context, "-n", args.namespace, "exec",
        f"deployment/{args.release}-onto", "-c", "onto", "--", "python", "-c",
        "import urllib.request; r=urllib.request.urlopen('http://%s-sandbox-gateway:8110/health', timeout=5); raise SystemExit(0 if r.status == 200 else 1)" % args.release)
    run("kubectl", "--context", args.context, "-n", args.namespace, "exec",
        f"deployment/{args.release}-core", "-c", "core", "--", "node", "-e",
        "const http=require('http');const q=http.request({socketPath:'/run/jiuwenbox/jiuwenbox.sock',path:'/health'},r=>process.exit(r.statusCode===200?0:1));q.on('error',()=>process.exit(1));q.end()")
    if read_yaml(args.values).get("modeling", {}).get("nl2sql", {}).get("enabled"):
        run("kubectl", "--context", args.context, "-n", args.namespace, "rollout", "status",
            f"deployment/{args.release}-nl2sql", f"--timeout={args.timeout_seconds}s")


def upgrade_precheck(args, root: Path, release: dict, merged: dict) -> dict:
    if not args.backup_record or (not args.transition_id and not getattr(args, "standalone_upgrade", False)):
        raise ConfigError("upgrade requires a backup record and joint upgrades require --transition-id")
    if not args.lock_holder:
        raise ConfigError("upgrade requires a delegated operation Lease")
    check_delegated_lease(args)
    installed = installed_release(args)
    transition_entry = None
    if args.transition_record:
        try:
            record = json.loads(args.transition_record.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CompatibilityError("cannot read delegated transition record") from exc
        if (record.get("schemaVersion") != "cce-transition-record/v1" or
                record.get("transition", {}).get("id") != args.transition_id):
            raise CompatibilityError("delegated transition record does not match this upgrade")
        manifest_path = Path(record.get("compatibilityFile", ""))
        if not manifest_path.is_file() or record.get("compatibilitySha256") != checksum(manifest_path):
            raise CompatibilityError("delegated compatibility manifest checksum differs")
        if transition(manifest_path, args.transition_id, installed, release) != record["transition"]:
            raise CompatibilityError("delegated transition differs from compatibility manifest")
        transition_entry = record["transition"]
        old = record.get("from", {})
        middle = record.get("intermediate", {})
        new = record.get("to", {})
        if ((old.get("modelingReleaseVersion"), old.get("modelingChartVersion")) !=
                (installed["releaseVersion"], installed["chartVersion"]) or
                (new.get("modelingReleaseVersion"), new.get("modelingChartVersion")) !=
                (release["releaseVersion"], release["chartVersion"]) or
                (middle.get("modelingReleaseVersion"), middle.get("modelingChartVersion")) !=
                (installed["releaseVersion"], installed["chartVersion"]) or
                transition_entry.get("steps") not in (["modeling"], ["consumer", "modeling"]) or
                transition_entry.get("requiresJointMaintenance") is not True or
                transition_entry.get("modelingDataMigration") != "none" or
                transition_entry.get("preserveModelingData") is not True):
            raise CompatibilityError("delegated transition does not match installed or target Modeling")
    else:
        if not getattr(args, "standalone_upgrade", False):
            transition_entry = transition(root / "deploy/compatibility.yaml", args.transition_id, installed, release)
    old_values = installed_values(args)
    if old_values.get("global", {}).get("targetArch") != merged["global"]["targetArch"]:
        raise CompatibilityError("target architecture differs from the installed release")
    if old_values.get("global", {}).get("auth", {}) != merged["global"]["auth"]:
        raise CompatibilityError("shared auth settings changed during Modeling upgrade")
    for component in ("core", "agent", "onto"):
        old_claim = old_values.get("modeling", {}).get("workloads", {}).get(component, {}).get("persistence", {}).get("existingClaim")
        target_claim = merged["modeling"]["workloads"][component]["persistence"]["existingClaim"]
        if old_claim != target_claim:
            raise ConfigError(f"{component} target PVC differs from installed values")
    before = bindings(kube, args, merged, stopped=True)
    verified_snapshots(kube, args, args.backup_record, before)
    return before


def check_delegated_lease(args) -> None:
    """Verify that the caller still owns this release's fixed operation Lease."""
    if not args.lock_holder:
        raise ConfigError("joint operation requires a delegated operation Lease")
    lease = kube(args, "get", "lease", f"{args.release}-cce-operation", "-n", args.namespace)
    spec = lease.get("spec", {})
    if spec.get("holderIdentity") != args.lock_holder or args.lock_holder != args.operation_id:
        raise ConfigError("operation Lease is not held by this operation")
    try:
        renewed = datetime.fromisoformat(spec["renewTime"].replace("Z", "+00:00"))
        expires = renewed + timedelta(seconds=int(spec["leaseDurationSeconds"]))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ConfigError("operation Lease has no valid expiry") from exc
    if expires <= datetime.now(timezone.utc):
        raise ConfigError("operation Lease has expired")


def _operation_slug(operation_id: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", (operation_id or "").lower()).strip("-")
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:8]
    return f"{value[:30].rstrip('-') or 'operation'}-{digest}"


def _snapshot_name(args, workload: str) -> str:
    # Keep the name deterministic for resume/retry while staying within the
    # Kubernetes 63-character DNS label limit.
    suffix = hashlib.sha256(args.operation_id.encode("utf-8")).hexdigest()[:12]
    prefix = f"{args.release}-{workload}-snapshot"
    return f"{prefix[:63 - len(suffix) - 1].rstrip('-')}-{suffix}"


def _snapshot_group(args) -> str:
    return _operation_slug(args.operation_id)


def _run_with_input(command: tuple[str, ...], input_text: str) -> str:
    result = subprocess.run(command, input=input_text, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def _optional_json(args, *rest: str) -> dict | None:
    command = ("kubectl", "--context", args.context, *rest, "-o", "json")
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        if "notfound" in result.stderr.lower() or "not found" in result.stderr.lower():
            return None
        raise RuntimeError(f"kubectl failed: {result.stderr.strip()[:500]}")
    return json.loads(result.stdout)


def _count_persisted_data(args) -> dict:
    counts = {}
    for workload in ("core", "agent", "onto"):
        output = run(
            "kubectl", "--context", args.context, "-n", args.namespace,
            "exec", f"deployment/{args.release}-{workload}", "-c", workload,
            "--", *DATA_COUNTS[workload],
        )
        try:
            counts[workload] = int(output.strip())
        except ValueError as exc:
            raise ConfigError(f"{workload} returned invalid pre-upgrade data evidence") from exc
    return counts


def _check_capacity(args) -> None:
    """Require enough free space in each Modeling data filesystem.

    This is a conservative local guard.  It cannot expose the backend pool's
    copy-on-write reserve, but it prevents starting a snapshot when a mounted
    PVC is already close to full.
    """
    minimum = args.min_free_percent
    if minimum < 0 or minimum > 100:
        raise ConfigError("min-free-percent must be between 0 and 100")
    for workload in ("core", "agent", "onto"):
        output = run(
            "kubectl", "--context", args.context, "-n", args.namespace,
            "exec", f"deployment/{args.release}-{workload}", "-c", workload,
            "--", "df", "-Pk", "/data",
        )
        rows = [line.split() for line in output.splitlines() if line.strip()]
        if len(rows) < 2 or len(rows[-1]) < 5:
            raise ConfigError(f"{workload} returned invalid /data capacity information")
        try:
            total = int(rows[-1][1])
            available = int(rows[-1][3])
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{workload} returned invalid /data capacity numbers") from exc
        if total <= 0 or available < 0:
            raise ConfigError(f"{workload} returned invalid /data capacity values")
        free_percent = available * 100.0 / total
        if free_percent < minimum:
            raise ConfigError(
                f"{workload} /data free space {free_percent:.1f}% is below the "
                f"minimum {minimum:.1f}%"
            )


def _stop_for_backup(args) -> None:
    for workload in ("core", "agent", "onto"):
        run(
            "kubectl", "--context", args.context, "-n", args.namespace,
            "scale", "deployment", f"{args.release}-{workload}", "--replicas=0",
        )
    wait_old_pods_gone(args)


def _ensure_volume_snapshot(args, workload: str, claim_name: str, deadline: float) -> dict:
    name = _snapshot_name(args, workload)
    current = _optional_json(args, "-n", args.namespace, "get", "volumesnapshot", name)
    if current is None:
        manifest = {
            "apiVersion": "snapshot.storage.k8s.io/v1",
            "kind": "VolumeSnapshot",
            "metadata": {"name": name, "namespace": args.namespace,
                          "labels": {
                              "app.kubernetes.io/managed-by": "modeling-cce",
                              "modeling.cce/release": args.release,
                              "modeling.cce/backup-group": args.snapshot_group,
                              "modeling.cce/workload": workload,
                              "modeling.cce/status": "pending",
                          }},
            "spec": {"source": {"persistentVolumeClaimName": claim_name}},
        }
        snapshot_class = getattr(args, "snapshot_class", None)
        if snapshot_class:
            manifest["spec"]["volumeSnapshotClassName"] = snapshot_class
        _run_with_input(
            ("kubectl", "--context", args.context, "-n", args.namespace, "apply", "-f", "-"),
            json.dumps(manifest),
        )
    elif current.get("spec", {}).get("source", {}).get("persistentVolumeClaimName") != claim_name:
        raise ConfigError(f"existing VolumeSnapshot {name} points to a different PVC")
    else:
        labels = current.get("metadata", {}).get("labels", {})
        expected = {
            "modeling.cce/release": args.release,
            "modeling.cce/backup-group": args.snapshot_group,
        }
        if any(labels.get(key) != value for key, value in expected.items()):
            raise ConfigError(f"existing VolumeSnapshot {name} is not owned by this backup group")

    while True:
        current = _optional_json(args, "-n", args.namespace, "get", "volumesnapshot", name)
        if current is None:
            raise ConfigError(f"VolumeSnapshot {name} disappeared while waiting")
        status = current.get("status", {})
        if status.get("error", {}).get("message"):
            raise ConfigError(f"VolumeSnapshot {name} failed: {status['error']['message']}")
        if status.get("readyToUse") is True and status.get("boundVolumeSnapshotContentName"):
            uid = current.get("metadata", {}).get("uid")
            created = current.get("metadata", {}).get("creationTimestamp")
            if not uid or not created:
                raise ConfigError(f"VolumeSnapshot {name} identity is incomplete")
            return {"snapshotName": name, "snapshotUid": uid}
        if time.monotonic() >= deadline:
            raise ConfigError(f"VolumeSnapshot {name} was not ready before timeout")
        time.sleep(2)


def _label_snapshot_group(args, status: str) -> None:
    for workload in ("core", "agent", "onto"):
        name = _snapshot_name(args, workload)
        run(
            "kubectl", "--context", args.context, "-n", args.namespace,
            "label", "volumesnapshot", name,
            f"modeling.cce/status={status}", "--overwrite",
        )


def _cleanup_old_successful_snapshots(args) -> list[str]:
    """Delete only complete, successful groups older than retention count."""
    if args.snapshot_retention < 1:
        raise ConfigError("snapshot-retention must be at least 1")
    snapshots = _optional_json(
        args, "-n", args.namespace, "get", "volumesnapshot",
        "-l", f"modeling.cce/release={args.release},modeling.cce/status=successful",
    ) or {"items": []}
    groups: dict[str, list[dict]] = {}
    for item in snapshots.get("items", []):
        labels = item.get("metadata", {}).get("labels", {})
        group = labels.get("modeling.cce/backup-group")
        if group:
            groups.setdefault(group, []).append(item)
    ordered = sorted(
        groups.items(),
        key=lambda pair: max(
            item.get("metadata", {}).get("creationTimestamp", "") for item in pair[1]
        ),
        reverse=True,
    )
    stale = ordered[args.snapshot_retention:]
    warnings: list[str] = []
    for group, items in stale:
        workloads = {
            item.get("metadata", {}).get("labels", {}).get("modeling.cce/workload")
            for item in items
        }
        if len(items) != 3 or workloads != {"core", "agent", "onto"}:
            # Never delete a partial or externally modified snapshot group.
            continue
        for item in items:
            name = item.get("metadata", {}).get("name")
            if not name:
                continue
            result = subprocess.run(
                ("kubectl", "--context", args.context, "-n", args.namespace,
                 "delete", "volumesnapshot", name, "--ignore-not-found"),
                text=True, capture_output=True,
            )
            if result.returncode:
                warnings.append(f"snapshot cleanup failed for {name}: {result.stderr.strip()[:180]}")
    return warnings


def prepare_auto_backup(args, root: Path, release: dict, merged: dict) -> Path:
    """Stop Modeling, snapshot all PVCs, and write a verified backup record."""
    args.state_dir.mkdir(parents=True, exist_ok=True)
    backup_path = args.state_dir / f"{args.release}-{_operation_slug(args.operation_id)}-modeling-backup.json"
    if backup_path.is_file():
        try:
            record = json.loads(backup_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError("automatic backup record exists but is invalid") from exc
        if (record.get("schemaVersion") != "cce-backup/v1" or
                record.get("context") != args.context or
                record.get("namespace") != args.namespace or
                record.get("releaseName") != args.release):
            raise ConfigError("automatic backup record belongs to another operation or release")
        args.snapshot_group = _snapshot_group(args)
        args.backup_record = backup_path
        return backup_path
    installed_release(args)
    old_values = installed_values(args)
    if old_values.get("global", {}).get("targetArch") != merged["global"]["targetArch"]:
        raise CompatibilityError("target architecture differs from the installed release")
    if old_values.get("global", {}).get("auth", {}) != merged["global"]["auth"]:
        raise CompatibilityError("shared auth settings changed during Modeling upgrade")
    before = bindings(kube, args, merged, stopped=False)
    _check_capacity(args)
    args.snapshot_group = _snapshot_group(args)
    snapshot_classes = _optional_json(args, "get", "volumesnapshotclass")
    classes = snapshot_classes.get("items", []) if snapshot_classes else []
    defaults = [item for item in classes if str(
        item.get("metadata", {}).get("annotations", {}).get(
            "snapshot.storage.kubernetes.io/is-default-class", ""
        )
    ).lower() == "true"]
    if len(defaults) > 1:
        raise ConfigError("multiple default VolumeSnapshotClasses are configured")
    if defaults:
        args.snapshot_class = defaults[0].get("metadata", {}).get("name")
    elif len(classes) == 1:
        args.snapshot_class = classes[0].get("metadata", {}).get("name")
    else:
        raise ConfigError("no unambiguous VolumeSnapshotClass is available for automatic backup")
    if not args.snapshot_class:
        raise ConfigError("VolumeSnapshotClass name is missing")
    evidence = _count_persisted_data(args)
    stopped_at = datetime.now(timezone.utc)
    _stop_for_backup(args)
    stopped = bindings(kube, args, merged, stopped=True)
    if stopped != before:
        raise ConfigError("stopping Modeling changed a PVC or its bound PV")
    deadline = time.monotonic() + args.timeout_seconds
    claims = {}
    for workload in ("core", "agent", "onto"):
        claims[workload] = {
            **stopped[workload],
            **_ensure_volume_snapshot(
                args, workload, stopped[workload]["claimName"], deadline,
            ),
        }
    backup_path.write_text(json.dumps({
        "schemaVersion": "cce-backup/v1",
        "context": args.context,
        "namespace": args.namespace,
        "releaseName": args.release,
        "stoppedAt": stopped_at.isoformat().replace("+00:00", "Z"),
        "dataEvidence": evidence,
        "claims": claims,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    backup_path.chmod(0o600)
    args.backup_record = backup_path
    return backup_path


def wait_old_pods_gone(args) -> None:
    deadline = time.monotonic() + args.timeout_seconds
    for component in ("core", "agent", "onto"):
        while True:
            pods = kube(args, "get", "pods", "-n", args.namespace,
                        "-l", f"app.kubernetes.io/instance={args.release},app.kubernetes.io/component={component}")
            if not pods.get("items"):
                break
            if time.monotonic() >= deadline:
                raise ConfigError(f"old {component} Pods did not exit before timeout")
            time.sleep(2)


def apply_upgrade(args, root: Path, release: dict, merged: dict, before: dict) -> None:
    helm_args = (*helm_base(args), "upgrade", args.release, str(root / "deploy/chart"),
                 "-f", str(args.values), "-f", str(args.resolved_images),
                 "--set-string", f"global.releaseVersion={release['releaseVersion']}",
                 "--set-string", f"global.operationId={args.operation_id}")
    run(*helm_args, "--set", "modeling.workloads.core.replicas=0",
        "--set", "modeling.workloads.agent.replicas=0",
        "--set", "modeling.workloads.onto.replicas=0",
        f"--timeout={args.timeout_seconds}s")
    wait_old_pods_gone(args)
    assert_same_bindings(before, bindings(kube, args, merged, stopped=True))
    run(*helm_args, "--wait", f"--timeout={args.timeout_seconds}s")
    verify(args, root)
    assert_same_bindings(before, bindings(kube, args, merged, stopped=False))
    verify_persisted_data(run, args, args.backup_record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "preflight", "install", "upgrade", "status", "verify"))
    for option in ("context", "namespace", "release", "operation-id", "state-dir", "values", "resolved-images", "resume", "backup-record", "lock-holder", "transition-record"):
        parser.add_argument(f"--{option}")
    parser.add_argument("--transition-id")
    parser.add_argument("--auto-backup", action="store_true",
                        help="automatically stop Modeling, create PVC snapshots, and generate backup record")
    parser.add_argument("--snapshot-retention", type=int, default=2,
                        help="number of successful snapshot groups to retain (default: 2)")
    parser.add_argument("--min-free-percent", type=float, default=15.0,
                        help="minimum free percentage on each /data filesystem (default: 15)")
    parser.add_argument("--defer-external-services", action="store_true",
                        help="defer Consumer Service lookup during initial joint preflight only")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--package-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.state_dir = Path(args.state_dir).resolve() if args.state_dir else None
    args.values = Path(args.values).resolve() if args.values else None
    args.resolved_images = Path(args.resolved_images).resolve() if args.resolved_images else None
    args.backup_record = Path(args.backup_record).resolve() if args.backup_record else None
    args.transition_record = Path(args.transition_record).resolve() if args.transition_record else None
    if args.resume and not args.operation_id:
        parser.error("resume requires operation-id")
    if args.action != "status" and not args.operation_id:
        args.operation_id = str(uuid.uuid4())
    if args.action == "upgrade" and args.lock_holder and not args.transition_id:
        parser.error("delegated upgrade requires --transition-id")
    if args.action == "upgrade" and args.transition_record and not args.lock_holder:
        parser.error("--transition-record requires a delegated --lock-holder")
    if args.auto_backup and args.action != "upgrade":
        parser.error("--auto-backup is only supported for upgrade")
    if args.auto_backup and args.backup_record:
        parser.error("--auto-backup cannot be combined with --backup-record")
    if args.auto_backup and args.lock_holder:
        parser.error("--auto-backup is only supported for standalone upgrade")
    if args.snapshot_retention < 1:
        parser.error("--snapshot-retention must be at least 1")
    if args.min_free_percent < 0 or args.min_free_percent > 100:
        parser.error("--min-free-percent must be between 0 and 100")
    if args.defer_external_services and args.action != "preflight":
        parser.error("--defer-external-services is only supported for preflight")
    if args.defer_external_services and args.transition_id:
        parser.error("--defer-external-services cannot be used for upgrade preflight")
    root = args.package_root or Path(__file__).resolve().parent.parent
    result = dict(schemaVersion="cce-operation/v1", operationId=args.operation_id, side="modeling",
                  action=args.action, status="failed", releaseName=args.release, namespace=args.namespace,
                  releaseVersion=None, chartVersion=None, phase="input", errorCode=None,
                  message="", retryable=False, endpoints=[], artifacts=[])
    operation_lease: OperationLease | None = None
    try:
        if not args.context or not args.namespace or not args.release or args.timeout_seconds < 1:
            raise ConfigError("context, namespace, release and positive timeout required")
        validate_name(args.namespace, "namespace")
        validate_name(args.release, "release")
        if args.action == "status":
            current = release_status(args)
            if current is None:
                raise ConfigError("release not installed")
            result["phase"] = "status"
            result["releaseVersion"] = installed_values(args).get("global", {}).get("releaseVersion")
            result["chartVersion"] = release_chart(args).removeprefix("modeling-")
            result["message"] = current.get("info", {}).get("status", "unknown")
        else:
            if not args.state_dir:
                raise ConfigError("state-dir required")
            release, merged = load_inputs(args, root)
            result["releaseVersion"] = release["releaseVersion"]
            result["chartVersion"] = release["chartVersion"]
            result["endpoints"] = endpoint_results(root, args.release, args.namespace)
            if args.action == "plan":
                result["phase"] = "render"
                result["artifacts"] = [str(render(args, root, release))]
            else:
                result["phase"] = "preflight"
                # A standalone Modeling upgrade owns the same Lease used by
                # joint-cce.py.  The joint entry passes --lock-holder and
                # therefore keeps ownership outside this process.
                if args.action == "upgrade" and not args.lock_holder:
                    args.lock_holder = args.operation_id
                    args.standalone_upgrade = True
                    operation_lease = OperationLease(args, args.operation_id)
                    operation_lease.acquire()
                preflight(args, merged, check_external_services=not args.defer_external_services)
                if args.lock_holder and args.action in ("install", "upgrade"):
                    check_delegated_lease(args)
                current = release_status(args)
                if args.action == "preflight" and args.transition_id:
                    upgrade_precheck(args, root, release, merged)
                if args.action == "install":
                    if current and not args.resume:
                        raise ConfigError("release already exists; use explicit resume or upgrade")
                    if current and args.resume != args.operation_id:
                        raise ConfigError("resume operation ID mismatch")
                    if current and installed_values(args).get("global", {}).get("operationId") != args.operation_id:
                        raise ConfigError("existing release belongs to another operation")
                    result["phase"] = "install"
                    command = "upgrade" if current else "install"
                    run(*helm_base(args), command, args.release, str(root / "deploy/chart"),
                        "-f", str(args.values), "-f", str(args.resolved_images),
                        "--set-string", f"global.releaseVersion={release['releaseVersion']}",
                        "--set-string", f"global.operationId={args.operation_id}",
                        "--wait", f"--timeout={args.timeout_seconds}s")
                    verify(args, root)
                elif args.action == "upgrade":
                    if operation_lease and (args.auto_backup or not args.backup_record):
                        result["phase"] = "automatic-backup"
                        backup_path = prepare_auto_backup(args, root, release, merged)
                        result["artifacts"].append(str(backup_path))
                    result["phase"] = "upgrade-precheck"
                    before = upgrade_precheck(args, root, release, merged)
                    if any(merged["modeling"]["workloads"][name]["replicas"] != 1
                           for name in ("core", "agent", "onto")):
                        raise ConfigError("target values must request one replica per stateful workload")
                    result["phase"] = "upgrade"
                    if operation_lease:
                        operation_lease.check()
                    apply_upgrade(args, root, release, merged, before)
                    if operation_lease:
                        operation_lease.check()
                    if operation_lease and getattr(args, "snapshot_group", None):
                        _label_snapshot_group(args, "successful")
                        result["warnings"] = _cleanup_old_successful_snapshots(args)
                elif args.action == "verify":
                    if not current:
                        raise ConfigError("release absent")
                    result["phase"] = "verify"
                    verify(args, root)
                result["message"] = "completed"
        result["status"] = "succeeded"
    except OperationLockError as exc:
        result.update(errorCode="LOCK_CONFLICT", message=str(exc), retryable=True)
    except CompatibilityError as exc:
        result.update(errorCode="INCOMPATIBLE_VERSION", message=str(exc))
    except (ConfigError, FileNotFoundError, KeyError, TypeError, jsonschema.SchemaError) as exc:
        result.update(errorCode="CONFIG_INVALID", message=str(exc))
    except subprocess.TimeoutExpired:
        result.update(errorCode="TIMEOUT", message="operation timed out", retryable=True)
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        result.update(errorCode="RESOURCE_NOT_READY", message=str(exc), retryable=True)
    finally:
        if operation_lease:
            operation_lease.release()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
