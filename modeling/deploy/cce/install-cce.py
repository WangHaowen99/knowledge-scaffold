#!/usr/bin/env python3
"""Modeling-side CCE release operations. Secrets are read by Kubernetes only."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

import jsonschema

from common import (CompatibilityError, ConfigError, endpoint_results, read_yaml, validate_compatibility,
                    validate_images, validate_name, validate_release)
from upgrade import assert_same_bindings, bindings, transition, verified_snapshots, verify_persisted_data


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


def installed_release(args) -> dict:
    current = release_status(args)
    if not current or current.get("info", {}).get("status") != "deployed":
        raise ConfigError("installed Modeling release is not deployed")
    chart = current.get("chart", "")
    if not chart.startswith("modeling-"):
        raise ConfigError("installed release is not a Modeling Chart")
    version = installed_values(args).get("global", {}).get("releaseVersion")
    if not version:
        raise ConfigError("installed release version is missing")
    return {"releaseVersion": version, "chartVersion": chart.removeprefix("modeling-")}


def load_inputs(args, root: Path):
    release = validate_release(root, "modeling")
    if not args.values or not args.resolved_images or not args.compatibility:
        raise ConfigError("values, resolved-images and compatibility are required")
    values = read_yaml(args.values)
    mapping = read_yaml(args.resolved_images)
    images = validate_images(values, mapping, release)
    validate_compatibility(args.compatibility, release)
    defaults = read_yaml(root / "deploy/chart/values.yaml")
    merged = merge(merge(defaults, values), mapping)
    schema = json.loads((root / "deploy/chart/values.schema.json").read_text())
    try:
        jsonschema.validate(merged, schema)
    except jsonschema.ValidationError as exc:
        raise ConfigError(f"values schema invalid at {'.'.join(map(str, exc.path))}: {exc.message}") from exc
    if merged["global"]["access"]["protocol"] == "HTTPS" and not merged["global"]["access"]["tlsSecretName"]:
        raise ConfigError("HTTPS requires tlsSecretName")
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
    run("kubectl", "--context", args.context, "-n", args.namespace, "exec",
        f"deployment/{args.release}-core", "-c", "core", "--", "node", "-e",
        "const http=require('http');const q=http.request({socketPath:'/run/jiuwenbox/jiuwenbox.sock',path:'/health'},r=>process.exit(r.statusCode===200?0:1));q.on('error',()=>process.exit(1));q.end()")
    if read_yaml(args.values).get("modeling", {}).get("nl2sql", {}).get("enabled"):
        run("kubectl", "--context", args.context, "-n", args.namespace, "rollout", "status",
            f"deployment/{args.release}-nl2sql", f"--timeout={args.timeout_seconds}s")


def upgrade_precheck(args, release: dict, merged: dict) -> dict:
    if not args.backup_record or not args.transition_id:
        raise ConfigError("upgrade requires --backup-record and --transition-id")
    if not args.lock_holder or not args.lock_name or not args.lock_namespace:
        raise ConfigError("upgrade requires a delegated operation Lease")
    lease = kube(args, "get", "lease", args.lock_name, "-n", args.lock_namespace)
    if lease.get("spec", {}).get("holderIdentity") != args.lock_holder or args.lock_holder != args.operation_id:
        raise ConfigError("operation Lease is not held by this upgrade")
    spec = lease["spec"]
    try:
        renewed = datetime.fromisoformat(spec["renewTime"].replace("Z", "+00:00"))
        expires = renewed + timedelta(seconds=int(spec["leaseDurationSeconds"]))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ConfigError("operation Lease has no valid expiry") from exc
    if expires <= datetime.now(timezone.utc):
        raise ConfigError("operation Lease has expired")
    installed = installed_release(args)
    transition(args.compatibility, args.transition_id, installed, release)
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
    for option in ("context", "namespace", "release", "operation-id", "state-dir", "values", "resolved-images", "compatibility", "resume", "backup-record", "lock-holder", "lock-name", "lock-namespace"):
        parser.add_argument(f"--{option}")
    parser.add_argument("--transition-id")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--package-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.state_dir = Path(args.state_dir).resolve() if args.state_dir else None
    args.values = Path(args.values).resolve() if args.values else None
    args.resolved_images = Path(args.resolved_images).resolve() if args.resolved_images else None
    args.compatibility = Path(args.compatibility).resolve() if args.compatibility else None
    args.backup_record = Path(args.backup_record).resolve() if args.backup_record else None
    if args.resume and not args.operation_id:
        parser.error("resume requires operation-id")
    if args.action != "status" and not args.operation_id:
        args.operation_id = str(uuid.uuid4())
    if args.action == "upgrade" and not args.transition_id:
        parser.error("upgrade requires --transition-id")
    root = args.package_root or Path(__file__).resolve().parent.parent
    result = dict(schemaVersion="cce-operation/v1", operationId=args.operation_id, side="modeling",
                  action=args.action, status="failed", releaseName=args.release, namespace=args.namespace,
                  releaseVersion=None, chartVersion=None, phase="input", errorCode=None,
                  message="", retryable=False, endpoints=[], artifacts=[])
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
            result["chartVersion"] = current.get("chart", "").removeprefix("modeling-")
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
                preflight(args, merged)
                current = release_status(args)
                if args.action == "preflight" and args.transition_id:
                    upgrade_precheck(args, release, merged)
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
                    result["phase"] = "upgrade-precheck"
                    before = upgrade_precheck(args, release, merged)
                    if any(merged["modeling"]["workloads"][name]["replicas"] != 1
                           for name in ("core", "agent", "onto")):
                        raise ConfigError("target values must request one replica per stateful workload")
                    result["phase"] = "upgrade"
                    apply_upgrade(args, root, release, merged, before)
                elif args.action == "verify":
                    if not current:
                        raise ConfigError("release absent")
                    result["phase"] = "verify"
                    verify(args, root)
                result["message"] = "completed"
        result["status"] = "succeeded"
    except CompatibilityError as exc:
        result.update(errorCode="INCOMPATIBLE_VERSION", message=str(exc))
    except (ConfigError, FileNotFoundError, KeyError, TypeError, jsonschema.SchemaError) as exc:
        result.update(errorCode="CONFIG_INVALID", message=str(exc))
    except subprocess.TimeoutExpired:
        result.update(errorCode="TIMEOUT", message="operation timed out", retryable=True)
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        result.update(errorCode="RESOURCE_NOT_READY", message=str(exc), retryable=True)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
