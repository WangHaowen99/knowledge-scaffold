#!/usr/bin/env python3
"""Modeling-side CCE release operations. Secrets are read by Kubernetes only."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import jsonschema

from common import (CompatibilityError, ConfigError, checksum, emit_stdout, endpoint_results, read_yaml,
                    release_chart_from_list, run_program, validate_images, validate_name, validate_release)
from upgrade import (DATA_COUNTS, WORKLOADS, LEGACY_WORKLOADS, LEGACY_EXPANDED_WORKLOADS,
                     SOURCE_WORKLOAD_SETS, assert_no_retired_claim_reuse, assert_same_bindings, bindings,
                     installed_workloads, mount_path, target_claim, transition, verified_snapshots,
                     verify_persisted_data)


SNAPSHOT_RETRY_LIMIT = 5
SNAPSHOT_TRANSIENT_ERROR = re.compile(
    r"(?:EVS\.1070|detaching|attaching|in[- ]use|busy|operation\s+in\s+progress|being\s+(?:deleted|updated))",
    re.IGNORECASE,
)


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
        result = run_program(
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
        result = run_program(
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
                result = run_program(
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
                run_program(
                    ("kubectl", "--context", self.args.context, "-n", self.args.namespace,
                     "replace", "-f", "-"),
                    input=json.dumps(document), text=True, capture_output=True,
                )
        except Exception:
            # A failed cleanup must not hide the original upgrade result.
            pass


def run(*command: str) -> str:
    result = run_program(command, text=True, capture_output=True)
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
    result = run_program((*helm_base(args), "status", args.release, "-o", "json"), text=True, capture_output=True)
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
    for key in ("authAdminToken", "anthropicAuthToken"):
        secret = merged["modeling"]["secrets"][key]
        if bool(secret["name"]) != bool(secret["key"]):
            raise ConfigError(f"incomplete {key} Secret reference")
    claims = [target_claim(merged, name) for name in WORKLOADS]
    if len(set(claims)) != len(claims):
        raise ConfigError("stateful workloads require distinct PVCs")
    for section, keys in (("hub", ("consumerToken",)), ("evaluation", ("consumerToken", "flywheelToken", "judgeApiKey", "sqlConfig"))):
        for key in keys:
            ref = merged[section]["secrets"][key]
            if bool(ref["name"]) != bool(ref["key"]):
                raise ConfigError(f"incomplete {section}.{key} Secret reference")
            source = ref.get("sourceRef")
            if source and any(bool(source.get(field)) for field in ("namespace", "name", "key")):
                if not ref["name"] or not all(source.get(field) for field in ("namespace", "name", "key")):
                    raise ConfigError(f"incomplete {section}.{key} sourceRef")
    internal = merged["hub"]["secrets"]["internal"]
    if internal["modelingTokenKey"] == internal["evaluationTokenKey"]:
        raise ConfigError("Hub internal token keys must be distinct")
    internal_names = set(_internal_secret_specs(merged))
    external_names = {merged["global"]["integration"]["serviceTokenSecret"]["name"]}
    for section in ("hub", "evaluation"):
        for key, ref in merged[section]["secrets"].items():
            if key not in ("internal", "encryptionKey", "notificationToken") and ref["name"]:
                external_names.add(ref["name"])
    if len(internal_names) != 2 or internal_names & external_names:
        raise ConfigError("internal Secrets must use distinct objects from external credentials")
    notification_name, notification_key = _notification_secret_ref(args, merged)
    if notification_name in internal_names | external_names:
        raise ConfigError("notification Secret must use a distinct object")
    if not notification_key:
        raise ConfigError("notification Secret key is required")
    integration_source = merged["global"]["integration"]["serviceTokenSecret"].get("sourceRef") or {}
    if integration_source and any(bool(integration_source.get(field)) for field in ("namespace", "name", "key")):
        if not all(integration_source.get(field) for field in ("namespace", "name", "key")):
            raise ConfigError("incomplete Modeling integration sourceRef")
    feedback = merged["evaluation"].get("feedback", {})
    if feedback.get("enabled") and not merged["evaluation"]["secrets"]["flywheelToken"]["name"]:
        raise ConfigError("enabled Evaluation feedback requires flywheelToken Secret")
    if feedback.get("notifyRuns") and not feedback.get("enabled"):
        raise ConfigError("feedback run notifications require Evaluation feedback")
    if any(merged[name]["replicas"] != 1 for name in ("hub", "evaluation")):
        raise ConfigError("Hub and Evaluation must run at one replica")
    routes = access["listeners"] if access["mode"] == "port" else access["routes"]
    evaluation_routes = [item for item in routes if item["endpointId"] == "evaluation"]
    if len(evaluation_routes) != 1 or evaluation_routes[0]["path"] != "/evaluation/":
        raise ConfigError("Evaluation Ingress route is required")
    if merged["modeling"]["workloads"]["onto"]["replicas"] != 1:
        raise ConfigError("stateful workloads must run at one replica")
    if not images["jiuwenbox"]:
        raise ConfigError("JiuwenBox reference missing")
    return release, merged


def validate_auth_owner(args, merged: dict, service: dict) -> None:
    """Check the independently owned Auth Service against its durable owner record."""
    ref = merged["externalAuth"]
    if ref.get("apiVersion") == "v1":
        consumer = merged["externalConsumer"]
        consumer_name = consumer["serviceName"].removesuffix("-backend")
        metadata = service.get("metadata", {})
        if not consumer_name or consumer_name == consumer["serviceName"]:
            raise ConfigError("legacy Auth Service does not belong to Consumer")
        if (ref["serviceName"] != f"{consumer_name}-auth"
                or ref["namespace"] != consumer["namespace"]):
            raise ConfigError("legacy Auth Service does not belong to Consumer")
        if (not metadata.get("uid")
                or metadata.get("annotations", {}).get("meta.helm.sh/release-name") != consumer_name):
            raise ConfigError("legacy Auth Service does not belong to Consumer")
        raw = kube(args, "get", "configmap", f"{consumer_name}-cce-state", "-n", consumer["namespace"])
        try:
            state = json.loads(raw["data"]["state.json"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("legacy Consumer Auth state is unavailable") from exc
        auth_endpoint = next((item for item in state.get("endpoints", []) if item.get("id") == "auth"), {})
        if (state.get("phase") != "ready" or state.get("externalAuth")
                or state.get("identity", {}).get("auth") != merged["global"]["auth"]):
            raise ConfigError("legacy Consumer Auth contract differs from Modeling")
        if (auth_endpoint.get("serviceName") != ref["serviceName"]
                or auth_endpoint.get("namespace") != ref["namespace"]):
            raise ConfigError("legacy Consumer Auth contract differs from Modeling")
        if (auth_endpoint.get("port") != ref["servicePort"]
                or auth_endpoint.get("apiVersion") != "v1"):
            raise ConfigError("legacy Consumer Auth contract differs from Modeling")
        validate_auth_admin_token(args, merged, consumer_name, consumer["namespace"])
        return
    if ref.get("apiVersion") != "auth-v1":
        raise ConfigError("unsupported Auth API contract")
    metadata = service.get("metadata", {})
    annotations = metadata.get("annotations", {})
    owner = annotations.get("meta.helm.sh/release-name")
    consumer_ref = merged["externalConsumer"]
    consumer_name = consumer_ref["serviceName"].removesuffix("-backend")
    if (not owner or owner == consumer_name
            or annotations.get("meta.helm.sh/release-namespace") != ref["namespace"]):
        raise ConfigError("external Auth Service has no independent Helm owner")
    raw = kube(args, "get", "configmap", f"{owner}-cce-state", "-n", ref["namespace"])
    try:
        state = json.loads(raw["data"]["state.json"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError("independent Auth owner state is unavailable") from exc
    if state.get("phase") != "ready" or state.get("identity") != merged["global"]["auth"]:
        raise ConfigError("independent Auth owner identity or Service UID differs")
    if not metadata.get("uid") or state.get("serviceUID") not in (None, metadata.get("uid")):
        raise ConfigError("independent Auth owner identity or Service UID differs")
    owner_status = json.loads(run("helm", "--kube-context", args.context, "--namespace",
                                  ref["namespace"], "status", owner, "-o", "json"))
    if owner_status.get("info", {}).get("status") != "deployed":
        raise ConfigError("independent Auth Helm release is not deployed")
    owner_values = json.loads(run("helm", "--kube-context", args.context, "--namespace",
                                  ref["namespace"], "get", "values", owner, "-o", "json"))
    if not isinstance(owner_values, dict) or owner_values.get("authOnly") is not True:
        raise ConfigError("independent Auth installed values differ from Modeling reference")
    if (owner_values.get("serviceName") != ref["serviceName"]
            or owner_values.get("workloadName") != ref["serviceName"]):
        raise ConfigError("independent Auth installed values differ from Modeling reference")
    if owner_values.get("global", {}).get("auth") != merged["global"]["auth"]:
        raise ConfigError("independent Auth installed values differ from Modeling reference")
    validate_auth_admin_token(args, merged, owner, ref["namespace"], owner_values)
    deployment = kube(args, "get", "deployment", service["metadata"]["name"], "-n", ref["namespace"])
    if deployment.get("metadata", {}).get("annotations", {}).get("meta.helm.sh/release-name") != owner:
        raise ConfigError("independent Auth Deployment owner differs")


def validate_auth_admin_token(args, merged: dict, owner: str, namespace: str,
                              owner_values: dict | None = None) -> None:
    """Check an enabled Modeling management token against the installed Auth owner."""
    client_ref = merged.get("modeling", {}).get("secrets", {}).get("authAdminToken", {})
    if not client_ref.get("name"):
        return
    if owner_values is None:
        owner_values = json.loads(run("helm", "--kube-context", args.context, "--namespace",
                                      namespace, "get", "values", owner, "-o", "json"))
    if not isinstance(owner_values, dict):
        raise ConfigError("Auth owner values are invalid")
    owner_ref = owner_values.get("sharedAuth", {}).get("adminTokenSecret", {})
    if not owner_ref.get("name") or not owner_ref.get("key"):
        raise ConfigError("Auth owner has no management token Secret reference")
    client = _read_secret(args, client_ref["name"])
    actual = _read_secret(args, owner_ref["name"], namespace)
    if not isinstance(client, dict) or not isinstance(actual, dict):
        raise ConfigError("Modeling or Auth management token Secret is invalid")
    if client.get("type") != "Opaque" or actual.get("type") != "Opaque":
        raise ConfigError("Modeling or Auth management token Secret is invalid")
    client_value = client.get("data", {}).get(client_ref["key"])
    actual_value = actual.get("data", {}).get(owner_ref["key"])
    if not isinstance(client_value, str) or not isinstance(actual_value, str):
        raise ConfigError("Modeling Auth management token differs from Auth owner")
    if not client_value or not actual_value or not hmac.compare_digest(client_value, actual_value):
        raise ConfigError("Modeling Auth management token differs from Auth owner")


def probe_auth_from_modeling(args, merged: dict) -> None:
    """Reach Auth from the Modeling namespace before replacing application Pods."""
    ref = merged["externalAuth"]
    image = merged["global"]["resolvedImages"]["ontoModeling"]
    suffix = uuid.uuid4().hex[:10]
    name = f"{(args.release + '-auth-probe')[:52].rstrip('-')}-{suffix}"
    # The internal Auth service has no TLS listener; these endpoints expose no credentials.
    address = f"http://{ref['serviceName']}.{ref['namespace']}.svc.cluster.local:{ref['servicePort']}"  # NOSONAR(S5332)
    code = '''import json,os,urllib.request
base=os.environ["AUTH_URL"]
with urllib.request.urlopen(base+"/health",timeout=10) as response:
    assert response.status==200
with urllib.request.urlopen(base+"/.well-known/openid-configuration",timeout=10) as response:
    assert json.load(response)["issuer"]==os.environ["AUTH_ISSUER"]
with urllib.request.urlopen(base+"/jwks.json",timeout=10) as response:
    assert any(key.get("kty")=="RSA" and key.get("alg")=="RS256" for key in json.load(response)["keys"])
'''
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": args.namespace},
           "spec": {"restartPolicy": "Never", "nodeSelector": {"kubernetes.io/arch": merged["global"]["targetArch"]},
                    "containers": [{"name": "probe", "image": image, "imagePullPolicy": "IfNotPresent",
                                    "command": ["python3", "-c", "import time; time.sleep(180)"],
                                    "env": [{"name": "AUTH_URL", "value": address},
                                            {"name": "AUTH_ISSUER", "value": merged["global"]["auth"]["issuer"]}]}]}}
    secrets = merged["global"]["imageDelivery"]["pullSecretNames"]
    if secrets:
        pod["spec"]["imagePullSecrets"] = [{"name": item} for item in secrets]
    _run_with_input(("kubectl", "--context", args.context, "-n", args.namespace,
                     "create", "-f", "-"), json.dumps(pod))
    try:
        run("kubectl", "--context", args.context, "-n", args.namespace,
            "wait", "--for=condition=ready", f"pod/{name}", "--timeout=60s")
        result = run_program(("kubectl", "--context", args.context, "-n", args.namespace,
                              "exec", name, "-c", "probe", "--", "python3", "-c", code),
                             text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise ConfigError("Auth network or identity probe failed")
    finally:
        run("kubectl", "--context", args.context, "-n", args.namespace,
            "delete", "pod", name, "--ignore-not-found", "--wait=true")


def preflight(args, merged: dict, *, check_external_services: bool = True,
              allow_missing_dependencies: bool = False) -> list[str]:
    namespace = kube(args, "get", "namespace", args.namespace)
    if namespace["status"]["phase"] != "Active":
        raise ConfigError("namespace is not Active")
    for workload in WORKLOADS:
        name = target_claim(merged, workload)
        pvc = kube(args, "get", "pvc", name, "-n", args.namespace)
        spec = pvc["spec"]
        if pvc["status"]["phase"] != "Bound" or spec.get("volumeMode", "Filesystem") != "Filesystem":
            raise ConfigError(f"PVC for {workload} is not Bound Filesystem")
        if "ReadWriteOnce" not in spec.get("accessModes", []):
            raise ConfigError(f"PVC for {workload} lacks ReadWriteOnce")
        if workload == "evaluation":
            existing_key = _read_secret(args, merged["evaluation"]["secrets"]["encryptionKey"]["name"])
            if existing_key is None and _evaluation_has_data(args, name):
                raise ConfigError("Evaluation PVC contains data but its encryption Secret is missing")
    refs = [merged["global"]["integration"]["serviceTokenSecret"]]
    refs.extend(merged["modeling"]["secrets"].values())
    refs.extend(ref for key, ref in merged["hub"]["secrets"].items() if key != "internal")
    refs.extend(ref for key, ref in merged["evaluation"]["secrets"].items()
                if key not in ("encryptionKey", "notificationToken"))
    for item in refs:
        if not item["name"]:
            continue
        source = item.get("sourceRef") or {}
        secret = _read_secret(args, item["name"])
        if secret is None:
            if not all(source.get(field) for field in ("namespace", "name", "key")):
                raise ConfigError(f"Secret {item['name']} is missing without a sourceRef")
            if source["namespace"] == args.namespace and source["name"] == item["name"]:
                raise ConfigError(f"Secret {item['name']} sourceRef points to its missing target")
            origin = _read_secret(args, source["name"], source["namespace"])
            if origin is None or not origin.get("data", {}).get(source["key"]):
                raise ConfigError(f"source Secret key missing in {source['name']}")
        elif secret.get("type") != "Opaque" or not secret.get("data", {}).get(item["key"]):
            raise ConfigError(f"Secret key missing in {item['name']}")
        elif all(source.get(field) for field in ("namespace", "name", "key")):
            origin = _read_secret(args, source["name"], source["namespace"])
            if origin is None or origin.get("data", {}).get(source["key"]) != secret["data"][item["key"]]:
                raise ConfigError(f"source and target Secret differ for {item['name']}")
    for secret_name, keys in _internal_secret_specs(merged).items():
        secret = _read_secret(args, secret_name)
        if secret is not None and (secret.get("type") != "Opaque" or
                                   any(not secret.get("data", {}).get(key) for key in keys)):
            raise ConfigError(f"internal Secret {secret_name} is incomplete")
    notification_name, notification_key = _notification_secret_ref(args, merged)
    notification = _read_secret(args, notification_name)
    if notification is not None and (notification.get("type") != "Opaque" or
                                     not notification.get("data", {}).get(notification_key)):
        raise ConfigError(f"internal Secret {notification_name} is incomplete")
    for secret_name in merged["global"]["imageDelivery"]["pullSecretNames"]:
        kube(args, "get", "secret", secret_name, "-n", args.namespace)
    if merged["global"]["access"]["protocol"] == "HTTPS":
        kube(args, "get", "secret", merged["global"]["access"]["tlsSecretName"], "-n", args.namespace)
    for listener in merged["global"]["access"]["listeners"]:
        if listener["protocol"] == "HTTPS":
            kube(args, "get", "secret", listener["tlsSecretName"], "-n", args.namespace)
    pending = []
    for field in ("externalAuth", "externalConsumer"):
        if check_external_services or field == "externalAuth":
            target = merged[field]
            if allow_missing_dependencies:
                target_namespace = kube(args, "get", "namespace", target["namespace"])
                if target_namespace["status"]["phase"] != "Active":
                    raise ConfigError(f"{field} namespace is not Active")
            service = (_optional_json(args, "-n", target["namespace"], "get", "service",
                                      target["serviceName"]) if allow_missing_dependencies else
                       kube(args, "get", "service", target["serviceName"], "-n", target["namespace"]))
            if service is None:
                pending.append(field)
                continue
            if target["servicePort"] not in [port["port"] for port in service["spec"]["ports"]]:
                raise ConfigError(f"{field} service port mismatch")
            if allow_missing_dependencies:
                endpoints = _optional_json(args, "-n", target["namespace"], "get", "endpoints",
                                           target["serviceName"])
                if not endpoints or not any(group.get("addresses") for group in endpoints.get("subsets", [])):
                    pending.append(field)
                    continue
            if field == "externalAuth":
                validate_auth_owner(args, merged, service)
                if check_external_services:
                    probe_auth_from_modeling(args, merged)
    nodes = kube(args, "get", "nodes")
    arch = merged["global"]["targetArch"]
    if not any(node["metadata"]["labels"].get("kubernetes.io/arch") == arch and
               not node["spec"].get("unschedulable", False) for node in nodes["items"]):
        raise ConfigError(f"no schedulable {arch} node")
    return pending


def render(args, root: Path, release: dict) -> Path:
    args.state_dir.mkdir(parents=True, exist_ok=True)
    output = args.state_dir / f"{args.release}-{args.operation_id}-modeling-rendered.yaml"
    manifest = run(*helm_base(args), "template", args.release, str(root / "deploy/chart"),
                   "-f", str(args.values), "-f", str(args.resolved_images),
                   "--set-string", f"global.releaseVersion={release['releaseVersion']}",
                   "--set-string", f"global.operationId={args.operation_id or ''}")
    output.write_text(manifest, encoding="utf-8")
    return output


def verify(args, root: Path, *, pending_auth: bool = False):
    for component in WORKLOADS:
        if pending_auth and component == "onto":
            continue
        run("kubectl", "--context", args.context, "-n", args.namespace, "rollout", "status",
            f"deployment/{args.release}-{component}", f"--timeout={args.timeout_seconds}s")
    run("kubectl", "--context", args.context, "-n", args.namespace, "rollout", "status",
        f"deployment/{args.release}-sandbox-gateway", f"--timeout={args.timeout_seconds}s")
    if not pending_auth:
        # The gateway Service can briefly have no endpoint while its Pod is
        # recreated by Helm, even after its rollout reports ready.
        deadline = time.monotonic() + min(args.timeout_seconds, 60)
        while True:
            try:
                run("kubectl", "--context", args.context, "-n", args.namespace, "exec",
                    f"deployment/{args.release}-onto", "-c", "onto", "--", "python", "-c",
                    "import urllib.request; r=urllib.request.urlopen('http://%s-sandbox-gateway:8110/health', timeout=5); raise SystemExit(0 if r.status == 200 else 1)" % args.release)
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
    for component, port, path in (("hub", 8200, "/api/health"),
                                  ("evaluation", 5180, "/evaluation/api/health")):
        run("kubectl", "--context", args.context, "-n", args.namespace, "exec",
            f"deployment/{args.release}-{component}", "-c", component, "--",
            "python", "-c",
            f"import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:{port}{path}', timeout=5); raise SystemExit(0 if r.status == 200 else 1)")


def verify_consumer_from_modeling(args, merged: dict) -> None:
    """Confirm the declared Backend is actually serving from Modeling's network."""
    ref = merged["externalConsumer"]
    # The internal Consumer service has no TLS listener; /ready is unauthenticated.
    url = f"http://{ref['serviceName']}.{ref['namespace']}.svc.cluster.local:{ref['servicePort']}/ready"  # NOSONAR(S5332)
    run("kubectl", "--context", args.context, "-n", args.namespace, "exec",
        f"deployment/{args.release}-evaluation", "-c", "evaluation", "--",
        "python", "-c", "import urllib.request,sys; "
        "r=urllib.request.urlopen(sys.argv[1],timeout=5); "
        "raise SystemExit(0 if r.status==200 else 1)", url)


def verify_retired_workloads(args) -> None:
    """A successful legacy transition must leave no Core or Agent writers."""
    for workload in ("core", "agent"):
        for kind in ("deployment", "service"):
            if _optional_json(args, "-n", args.namespace, "get", kind,
                              f"{args.release}-{workload}") is not None:
                raise ConfigError(f"retired {workload} {kind} still exists")
        pods = kube(args, "get", "pods", "-n", args.namespace,
                    "-l", f"app.kubernetes.io/instance={args.release},app.kubernetes.io/component={workload}")
        if pods.get("items"):
            raise ConfigError(f"retired {workload} Pods still exist")
    for name in getattr(args, "retired_ingress_names", ()):
        if _optional_json(args, "-n", args.namespace, "get", "ingress", name) is not None:
            raise ConfigError(f"retired Core Ingress {name} still exists")


def upgrade_precheck(args, root: Path, release: dict, merged: dict) -> dict:
    if not args.backup_record:
        raise ConfigError("upgrade requires a backup record and joint upgrades require --transition-id")
    if not args.transition_id and not getattr(args, "standalone_upgrade", False):
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
        installed_version = (installed["releaseVersion"], installed["chartVersion"])
        target_version = (release["releaseVersion"], release["chartVersion"])
        def modeling_version(combination):
            return combination.get("modelingReleaseVersion"), combination.get("modelingChartVersion")
        versions_match = (modeling_version(old) == installed_version
                          and modeling_version(middle) == installed_version
                          and modeling_version(new) == target_version)
        policy_matches = (transition_entry.get("steps") in (["modeling"], ["consumer", "modeling"])
                          and transition_entry.get("requiresJointMaintenance") is True
                          and transition_entry.get("modelingDataMigration") in ("none", "required")
                          and transition_entry.get("preserveModelingData") is True)
        if not versions_match or not policy_matches:
            raise CompatibilityError("delegated transition does not match installed or target Modeling")
    else:
        if not getattr(args, "standalone_upgrade", False):
            transition_entry = transition(root / "deploy/compatibility.yaml", args.transition_id, installed, release)
    old_values = installed_values(args)
    source_workloads = installed_workloads(old_values)
    if transition_entry and transition_entry["modelingDataMigration"] != (
            "required" if "core" in source_workloads else "none"):
        raise CompatibilityError("transition migration policy differs from installed workloads")
    if "core" in source_workloads:
        access = old_values.get("global", {}).get("access", {})
        retired_names = []
        if access.get("mode") == "port":
            for item in access.get("listeners", []):
                if item.get("endpointId") not in ("modeling-core", "modeling-mcp"):
                    continue
                listener_id = item.get("id")
                if isinstance(listener_id, str) and listener_id:
                    retired_names.append(f"{args.release}-{listener_id}"[:63].rstrip("-"))
        args.retired_ingress_names = tuple(retired_names)
    if old_values.get("global", {}).get("targetArch") != merged["global"]["targetArch"]:
        raise CompatibilityError("target architecture differs from the installed release")
    if old_values.get("global", {}).get("auth", {}) != merged["global"]["auth"]:
        raise CompatibilityError("shared auth settings changed during Modeling upgrade")
    workloads = source_workloads
    if any(old_values.get("modeling", {}).get("workloads", {}).get(name, {}).get("replicas", 1) != 1
           for name in ("core", "agent", "onto") if name in workloads):
        raise ConfigError("installed Modeling release must request one replica per stateful workload")
    args.backup_workloads = workloads
    if "hub" in workloads:
        if any(old_values[name].get("replicas", 1) != 1 for name in ("hub", "evaluation")):
            raise ConfigError("installed Hub and Evaluation must request one replica")
        if _installed_internal_secret_specs(old_values, args) != _internal_secret_specs(merged):
            raise ConfigError("internal Secret names and keys cannot change during upgrade")
        old_notification = _notification_secret_ref(args, old_values)
        if old_values.get("evaluation", {}).get("secrets", {}).get("notificationToken") and \
                old_notification != _notification_secret_ref(args, merged):
            raise ConfigError("notification Secret name and key cannot change during upgrade")
    for component in ("onto",):
        old_claim = old_values.get("modeling", {}).get("workloads", {}).get(component, {}).get("persistence", {}).get("existingClaim")
        target_claim = merged["modeling"]["workloads"][component]["persistence"]["existingClaim"]
        if old_claim != target_claim:
            raise ConfigError(f"{component} target PVC differs from installed values")
    if "hub" in workloads:
        for component in ("hub", "evaluation"):
            if old_values[component]["persistence"]["existingClaim"] != merged[component]["persistence"]["existingClaim"]:
                raise ConfigError(f"{component} target PVC differs from installed values")
    try:
        backup_record = json.loads(args.backup_record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read backup record") from exc
    recorded_uids = backup_record.get("secretUids")
    if recorded_uids:
        if not isinstance(recorded_uids, dict) or recorded_uids != _identity_record(args, merged):
            raise ConfigError("backup record Secret identities changed")
    before = bindings(kube, args, merged, stopped=True, workloads=workloads)
    assert_no_retired_claim_reuse(merged, before)
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
    result = run_program(command, input=input_text, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def _optional_json(args, *rest: str) -> dict | None:
    command = ("kubectl", "--context", args.context, *rest, "-o", "json")
    result = run_program(command, text=True, capture_output=True)
    if result.returncode:
        if "notfound" in result.stderr.lower() or "not found" in result.stderr.lower():
            return None
        raise RuntimeError(f"kubectl failed: {result.stderr.strip()[:500]}")
    return json.loads(result.stdout)


def _internal_secret_specs(merged: dict) -> dict[str, tuple[str, ...]]:
    hub = merged["hub"]["secrets"]["internal"]
    evaluation = merged["evaluation"]["secrets"]["encryptionKey"]
    return {
        hub["name"]: (hub["modelingTokenKey"], hub["evaluationTokenKey"]),
        evaluation["name"]: (evaluation["key"],),
    }


def _notification_secret_ref(args, merged: dict) -> tuple[str, str]:
    ref = merged.get("evaluation", {}).get("secrets", {}).get("notificationToken") or {}
    return ref.get("name") or f"{args.release}-notify", ref.get("key") or "service-token"


def _assert_unchanged_internal_secrets(args, merged: dict) -> None:
    old_values = installed_values(args)
    if "hub" in old_values and "evaluation" in old_values:
        if _installed_internal_secret_specs(old_values, args) != _internal_secret_specs(merged):
            raise ConfigError("internal Secret names and keys cannot change during upgrade")
        if old_values.get("evaluation", {}).get("secrets", {}).get("notificationToken") and \
                _notification_secret_ref(args, old_values) != _notification_secret_ref(args, merged):
            raise ConfigError("notification Secret name and key cannot change during upgrade")


def _installed_internal_secret_specs(values: dict, args) -> dict[str, tuple[str, ...]]:
    """Recognize names and keys used by releases installed before these fields existed."""
    hub = values.get("hub", {}).get("secrets", {}).get("internal")
    evaluation = values.get("evaluation", {}).get("secrets", {}).get("encryptionKey")
    if hub is None and evaluation is None:
        return {f"{args.release}-hub-internal": ("modeling-token", "evaluation-token"),
                f"{args.release}-evaluation-internal": ("encryption-key",)}
    if not isinstance(hub, dict) or not isinstance(evaluation, dict):
        raise ConfigError("installed internal Secret references are incomplete")
    return _internal_secret_specs(values)


def _read_secret(args, name: str, namespace: str | None = None) -> dict | None:
    return _optional_json(args, "-n", namespace or args.namespace, "get", "secret", name)


def _secret_identity(args, name: str) -> str:
    item = _read_secret(args, name)
    uid = item.get("metadata", {}).get("uid") if item else None
    if not uid:
        raise ConfigError(f"Secret {name} has no stable UID")
    return uid


def _identity_record(args, merged: dict) -> dict[str, str]:
    names = [*list(_internal_secret_specs(merged)), _notification_secret_ref(args, merged)[0]]
    integration = merged.get("global", {}).get("integration", {}).get("serviceTokenSecret", {})
    if integration.get("name"):
        names.append(integration["name"])
    auth_admin = merged.get("modeling", {}).get("secrets", {}).get("authAdminToken", {})
    if auth_admin.get("name"):
        names.append(auth_admin["name"])
    for section in ("hub", "evaluation"):
        names.extend(ref["name"] for key, ref in merged.get(section, {}).get("secrets", {}).items()
                     if key not in ("internal", "encryptionKey") and ref["name"])
    return {name: _secret_identity(args, name) for name in sorted(set(names))}


def _evaluation_has_data(args, claim_name: str) -> bool:
    """Detect a prior Evaluation deployment using this claim; never regenerate its key."""
    deployment = _optional_json(args, "-n", args.namespace, "get", "deployment",
                                f"{args.release}-evaluation")
    if deployment is None:
        return False
    volumes = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
    return any(item.get("persistentVolumeClaim", {}).get("claimName") == claim_name for item in volumes)


def _assert_distinct_internal_tokens(args, merged: dict) -> None:
    internal = merged["hub"]["secrets"]["internal"]
    hub = _read_secret(args, internal["name"])
    integration = _read_secret(args, merged["global"]["integration"]["serviceTokenSecret"]["name"])
    if not hub or not integration:
        raise ConfigError("internal service tokens are unavailable")
    values = [hub.get("data", {}).get(internal["modelingTokenKey"]),
              hub.get("data", {}).get(internal["evaluationTokenKey"]),
              integration.get("data", {}).get(
                  merged["global"]["integration"]["serviceTokenSecret"]["key"])]
    if any(not value for value in values) or len(set(values)) != len(values):
        raise ConfigError("Hub and Modeling service tokens must be distinct")


def _assert_distinct_flywheel_token(args, merged: dict) -> None:
    flywheel = merged["evaluation"]["secrets"].get("flywheelToken", {})
    if not flywheel.get("name"):
        return
    resource = _read_secret(args, flywheel["name"])
    value = resource.get("data", {}).get(flywheel["key"]) if resource else None
    if not value:
        raise ConfigError("Evaluation flywheel token is missing")
    try:
        decoded = base64.b64decode(value, validate=True).decode()
    except (ValueError, UnicodeError):
        raise ConfigError("Evaluation flywheel token is invalid") from None
    if len(decoded.strip()) < 32:
        raise ConfigError("Evaluation flywheel token must contain at least 32 characters")
    references = [merged["global"]["integration"]["serviceTokenSecret"],
                  merged["hub"]["secrets"].get("consumerToken", {}),
                  merged["evaluation"]["secrets"].get("consumerToken", {}),
                  merged["evaluation"]["secrets"].get("notificationToken", {})]
    for reference in references:
        if reference.get("name"):
            other = _read_secret(args, reference["name"])
            if other and other.get("data", {}).get(reference["key"]) == value:
                raise ConfigError("Evaluation flywheel token must differ from other service tokens")
    internal = merged["hub"]["secrets"]["internal"]
    hub = _read_secret(args, internal["name"])
    if hub and value in (hub.get("data", {}).get(internal["modelingTokenKey"]),
                         hub.get("data", {}).get(internal["evaluationTokenKey"])):
        raise ConfigError("Evaluation flywheel token must differ from other service tokens")


def _create_secret(args, name: str, values: dict[str, str]) -> None:
    document = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": name, "namespace": args.namespace},
        "type": "Opaque",
        "data": {key: base64.b64encode(value.encode()).decode() for key, value in values.items()},
    }
    result = run_program(
        ("kubectl", "--context", args.context, "-n", args.namespace, "create", "-f", "-"),
        input=json.dumps(document), text=True, capture_output=True,
    )
    if result.returncode:
        # A concurrent creator may have won. Reuse only a complete existing object.
        current = _read_secret(args, name)
        if current is None or current.get("type") != "Opaque" or any(
            not current.get("data", {}).get(key) for key in values
        ):
            raise ConfigError(f"unable to create Secret {name}; pre-create it with the configured keys and retry")


def ensure_application_secrets(args, merged: dict) -> None:
    """Create internal keys once and import only explicitly referenced external keys."""
    encryption = merged["evaluation"]["secrets"]["encryptionKey"]
    notification_name, notification_key = _notification_secret_ref(args, merged)
    notification = _read_secret(args, notification_name)
    if notification is None:
        _create_secret(args, notification_name, {notification_key: secrets.token_urlsafe(48)})
    elif notification.get("type") != "Opaque" or not notification.get("data", {}).get(notification_key):
        raise ConfigError(f"internal Secret {notification_name} is incomplete")
    for name, keys in _internal_secret_specs(merged).items():
        current = _read_secret(args, name)
        if current is not None:
            if current.get("type") != "Opaque" or any(not current.get("data", {}).get(key) for key in keys):
                raise ConfigError(f"internal Secret {name} is incomplete")
            continue
        values = {}
        for key in keys:
            values[key] = (base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
                           if name == encryption["name"] else secrets.token_urlsafe(48))
        _create_secret(args, name, values)
    for section, refs in (("hub", merged["hub"]["secrets"]),
                          ("evaluation", merged["evaluation"]["secrets"])):
        for key, ref in refs.items():
            if key in ("internal", "encryptionKey", "notificationToken"):
                continue
            if not ref["name"]:
                continue
            target = _read_secret(args, ref["name"])
            if target is not None:
                if target.get("type") != "Opaque" or not target.get("data", {}).get(ref["key"]):
                    raise ConfigError(f"Secret key missing in {ref['name']}")
                source = ref.get("sourceRef") or {}
                if all(source.get(field) for field in ("namespace", "name", "key")):
                    origin = _read_secret(args, source["name"], source["namespace"])
                    if origin is None or origin.get("data", {}).get(source["key"]) != target["data"][ref["key"]]:
                        raise ConfigError(f"source and target Secret differ for {ref['name']}")
                continue
            source = ref.get("sourceRef") or {}
            if not all(source.get(field) for field in ("namespace", "name", "key")):
                raise ConfigError(f"{section}.{key} target Secret is missing")
            origin = _read_secret(args, source["name"], source["namespace"])
            encoded = origin.get("data", {}).get(source["key"]) if origin else None
            if not encoded:
                raise ConfigError(f"{section}.{key} source Secret key is missing")
            if source["namespace"] == args.namespace and source["name"] == ref["name"]:
                raise ConfigError(f"{section}.{key} source and target Secret cannot be the same missing object")
            # Import the exact encoded value; no credential reaches values, argv, or the journal.
            document = {
                "apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": ref["name"], "namespace": args.namespace},
                "type": "Opaque", "data": {ref["key"]: encoded},
            }
            result = run_program(
                ("kubectl", "--context", args.context, "-n", args.namespace, "create", "-f", "-"),
                input=json.dumps(document), text=True, capture_output=True,
            )
            if result.returncode:
                current = _read_secret(args, ref["name"])
                if current is None or current.get("type") != "Opaque" or ref["key"] not in current.get("data", {}):
                    raise ConfigError(f"unable to import {section}.{key} Secret")
    integration = merged["global"]["integration"]["serviceTokenSecret"]
    target = _read_secret(args, integration["name"])
    if target is None:
        source = integration.get("sourceRef") or {}
        if not all(source.get(field) for field in ("namespace", "name", "key")):
            raise ConfigError("Modeling integration Secret is missing")
        origin = _read_secret(args, source["name"], source["namespace"])
        encoded = origin.get("data", {}).get(source["key"]) if origin else None
        if not encoded:
            raise ConfigError("Modeling integration source Secret key is missing")
        document = {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": integration["name"], "namespace": args.namespace},
            "type": "Opaque", "data": {integration["key"]: encoded},
        }
        result = run_program(
            ("kubectl", "--context", args.context, "-n", args.namespace, "create", "-f", "-"),
            input=json.dumps(document), text=True, capture_output=True,
        )
        if result.returncode:
            current = _read_secret(args, integration["name"])
            if current is None or current.get("type") != "Opaque" or current.get("data", {}).get(integration["key"]) != encoded:
                raise ConfigError("unable to import Modeling integration Secret")
    _assert_distinct_internal_tokens(args, merged)
    _assert_distinct_flywheel_token(args, merged)


def _count_persisted_data(args) -> dict:
    counts = {}
    for workload in getattr(args, "backup_workloads", WORKLOADS):
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


def _pvc_bindings(args, merged: dict) -> dict:
    result = {}
    for workload in WORKLOADS:
        claim = target_claim(merged, workload)
        pvc = kube(args, "get", "pvc", claim, "-n", args.namespace)
        uid = pvc.get("metadata", {}).get("uid")
        pv = pvc.get("spec", {}).get("volumeName")
        if pvc.get("status", {}).get("phase") != "Bound" or not uid or not pv:
            raise ConfigError(f"{workload} PVC identity is incomplete")
        result[workload] = {"claimName": claim, "claimUid": uid, "volumeName": pv}
    return result


def _write_private_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _operation_state_path(args) -> Path:
    return args.state_dir / f"{args.release}-{_operation_slug(args.operation_id)}-operation.json"


def _cluster_operation_state(args) -> dict | None:
    resource = _optional_json(args, "-n", args.namespace, "get", "configmap", f"{args.release}-cce-state")
    if resource is None:
        return None
    try:
        return json.loads(resource.get("data", {})["state.json"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError("Modeling cluster operation state is invalid") from exc


def _write_cluster_state(args, document: dict) -> None:
    manifest = {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": f"{args.release}-cce-state", "namespace": args.namespace,
                             "labels": {"app.kubernetes.io/instance": args.release}},
                "data": {"state.json": json.dumps(document, sort_keys=True)}}
    _run_with_input(("kubectl", "--context", args.context, "-n", args.namespace,
                     "apply", "-f", "-"), json.dumps(manifest))


def _record_dependencies(args, merged: dict, pending: list[str], *, service_verified: bool = True) -> None:
    """Record a fresh install's expected clients without claiming they are usable."""
    state = _cluster_operation_state(args) or {}
    expected = {key: merged[key] for key in ("externalAuth", "externalConsumer")}
    if state and state.get("schemaVersion") != "cce-modeling-dependencies/v1":
        raise ConfigError("Modeling dependency record belongs to another operation")
    if state and state.get("dependencies") != expected:
        raise ConfigError("Modeling dependency record differs from this installation")
    if state and state.get("operationId") != installed_values(args).get("global", {}).get("operationId"):
        raise ConfigError("Modeling dependency record belongs to another installed release")
    state.update(schemaVersion="cce-modeling-dependencies/v1",
                 operationId=state.get("operationId", args.operation_id),
                 stage="waiting-dependencies" if pending else
                       "complete" if service_verified else "verifying-service",
                 dependencies=expected, pendingDependencies=pending)
    _write_cluster_state(args, state)


def _verify_dependency_record(args, merged: dict) -> None:
    state = _cluster_operation_state(args)
    if state and state.get("schemaVersion") == "cce-modeling-dependencies/v1":
        if state.get("dependencies") != {key: merged[key] for key in ("externalAuth", "externalConsumer")}:
            raise ConfigError("Modeling dependency reference differs from installation")
        current = merge(read_yaml(Path(__file__).resolve().parent / "chart/values.yaml"),
                        installed_values(args))
        if (current.get("global", {}).get("operationId") != state.get("operationId") or
                current.get("externalAuth") != merged["externalAuth"] or
                current.get("externalConsumer") != merged["externalConsumer"]):
            raise ConfigError("installed Modeling dependency reference differs from its record")


def _record_upgrade_stage(args, stage: str, **details) -> None:
    if getattr(args, "state_dir", None):
        path = _operation_state_path(args)
        document = {}
        try:
            if path.is_file():
                document = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(document, dict):
                    raise ConfigError("existing Modeling operation state is invalid")
                if document.get("operationId") != args.operation_id:
                    raise ConfigError("existing Modeling operation belongs to another operation")
        except (OSError, ValueError, TypeError, ConfigError):
            args.stage_warnings = getattr(args, "stage_warnings", []) + ["local operation state could not be read"]
            return
        document.update({"schemaVersion": "cce-modeling-operation/v1", "operationId": args.operation_id,
                         "context": args.context, "namespace": args.namespace, "releaseName": args.release,
                         "action": "upgrade", "stage": stage, **details})
        try:
            _write_cluster_state(args, document)
        except (OSError, RuntimeError, ConfigError):
            args.stage_warnings = getattr(args, "stage_warnings", []) + ["cluster operation state could not be written"]
        try:
            _write_private_json(path, document)
        except (OSError, RuntimeError, ConfigError):
            args.stage_warnings = getattr(args, "stage_warnings", []) + ["local operation state could not be written"]
    args.upgrade_stage = stage


def _diagnose(args, release: dict, merged: dict) -> dict:
    current = release_status(args)
    state = _cluster_operation_state(args)
    claims = _pvc_bindings(args, merged)
    issues = []
    if current is None:
        issues.append("Helm release is absent")
    elif current.get("info", {}).get("status") != "deployed":
        issues.append("Helm release is not deployed")
    else:
        installed = installed_release(args)
        if (installed["releaseVersion"], installed["chartVersion"]) != (
                release["releaseVersion"], release["chartVersion"]):
            issues.append("installed release differs from target")
    if state and state.get("stage") not in ("complete", "old-service-restored", "preparing-backup", "waiting-dependencies"):
        issues.append(f"unfinished Modeling operation at stage {state.get('stage', 'unknown')}")
    if state and state.get("stage") == "waiting-dependencies":
        issues.append("waiting for Auth and Consumer: " +
                      ", ".join(state.get("pendingDependencies", [])))
    return {"ready": not issues, "issues": issues, "claims": claims,
            "installed": current is not None,
            "operationState": {"operationId": state.get("operationId"), "stage": state.get("stage")}
            if state else None}


def _uninstall(args, root: Path) -> dict:
    """Remove only the Modeling Helm release; retain external storage and credentials."""
    release = validate_release(root, "modeling")
    record = args.state_dir / f"{args.release}-{_operation_slug(args.operation_id)}-uninstall.json"
    expected = {"operationId": args.operation_id, "namespace": args.namespace,
                "releaseName": args.release, "releaseVersion": release["releaseVersion"],
                "chartVersion": release["chartVersion"]}
    if args.resume:
        if not record.is_file() or json.loads(record.read_text()) != expected:
            raise ConfigError("Modeling uninstall record is missing or differs from this operation")
    elif record.exists():
        raise ConfigError("Modeling uninstall record already exists; use --resume")
    current = release_status(args)
    if current is None:
        if not args.resume:
            raise ConfigError("Modeling release is absent")
        return release
    installed = installed_release(args)
    if installed != {"releaseVersion": release["releaseVersion"], "chartVersion": release["chartVersion"]}:
        raise ConfigError("installed Modeling release differs from uninstall package")
    state = _cluster_operation_state(args)
    if state and state.get("stage") not in ("complete", "old-service-restored", "waiting-dependencies", "verifying-service"):
        raise ConfigError("finish or inspect the active Modeling operation before uninstall")
    if not args.resume:
        _write_private_json(record, expected)
    if args.lock_holder:
        check_delegated_lease(args)
    run(*helm_base(args), "uninstall", args.release, "--wait", f"--timeout={args.timeout_seconds}s")
    if release_status(args) is not None:
        raise RuntimeError("Modeling Helm release remains after uninstall")
    return release


def _require_upgrade_state_ready(args) -> None:
    state = _cluster_operation_state(args)
    if state and state.get("schemaVersion") == "cce-modeling-dependencies/v1":
        if state.get("stage") != "complete":
            raise ConfigError("verify Auth and Consumer dependencies before Modeling upgrade")
        return
    if not state or state.get("stage") in ("complete", "old-service-restored"):
        return
    matching_resume = (getattr(args, "resume", None), state.get("action"), state.get("operationId")) == (
        args.operation_id, "upgrade", args.operation_id)
    if matching_resume and state.get("stage") in ("preparing-backup", "backup-ready"):
        path = _operation_state_path(args)
        try:
            local = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigError("local upgrade operation state is unavailable; inspect before resuming") from exc
        if not isinstance(local, dict):
            raise ConfigError("cluster and local upgrade stages differ; inspect before resuming")
        local_identity = (local.get("action"), local.get("operationId"), local.get("stage"))
        if local_identity != ("upgrade", args.operation_id, state["stage"]):
            raise ConfigError("cluster and local upgrade stages differ; inspect before resuming")
        # A Helm revision created by this operation means its outcome needs
        # inspection, even when a state write left an older stage behind.
        if installed_values(args).get("global", {}).get("operationId") == args.operation_id:
            raise ConfigError("upgrade outcome is uncertain; inspect the Helm release before resuming")
        if state["stage"] == "backup-ready":
            backup = args.state_dir / f"{args.release}-{_operation_slug(args.operation_id)}-modeling-backup.json"
            if not backup.is_file():
                raise ConfigError("completed backup record is missing; inspect before resuming")
        return
    raise ConfigError(f"unfinished Modeling operation at stage {state.get('stage', 'unknown')}; inspect before upgrading")


def _check_capacity(args) -> None:
    """Require enough free space in each Modeling data filesystem.

    This is a conservative local guard.  It cannot expose the backend pool's
    copy-on-write reserve, but it prevents starting a snapshot when a mounted
    PVC is already close to full.
    """
    minimum = args.min_free_percent
    if minimum < 0 or minimum > 100:
        raise ConfigError("min-free-percent must be between 0 and 100")
    for workload in getattr(args, "backup_workloads", WORKLOADS):
        output = run(
            "kubectl", "--context", args.context, "-n", args.namespace,
            "exec", f"deployment/{args.release}-{workload}", "-c", workload,
            "--", "df", "-Pk", mount_path(workload),
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
    # Arm recovery before the first scale: even a partial stop must be undone
    # if a later scale, snapshot or backup-record check fails.
    args.restore_workloads_on_failure = True
    for workload in getattr(args, "backup_workloads", WORKLOADS):
        run(
            "kubectl", "--context", args.context, "-n", args.namespace,
            "scale", "deployment", f"{args.release}-{workload}", "--replicas=0",
        )
    wait_old_pods_gone(args)


def _wait_volume_detached(args, volume_name: str, deadline: float) -> None:
    """Wait until CSI no longer reports the PVC's PV attached to a node."""
    while True:
        attachments = _optional_json(args, "get", "volumeattachment") or {"items": []}
        active = []
        for item in attachments.get("items", []):
            source = item.get("spec", {}).get("source", {})
            if source.get("persistentVolumeName") != volume_name:
                continue
            if item.get("status", {}).get("attached") is True:
                active.append(item)
        if not active:
            return
        if time.monotonic() >= deadline:
            nodes = ", ".join(
                str(item.get("spec", {}).get("nodeName") or "unknown") for item in active
            )
            raise ConfigError(f"PV {volume_name} remains attached to {nodes} before snapshot")
        time.sleep(2)


def _discard_snapshot_for_retry(args, name: str, deadline: float) -> None:
    """Delete one operation-owned failed snapshot and wait for its removal."""
    result = run_program(
        ("kubectl", "--context", args.context, "-n", args.namespace,
         "delete", "volumesnapshot", name, "--ignore-not-found", "--wait=false"),
        text=True, capture_output=True,
    )
    if result.returncode:
        raise ConfigError(f"VolumeSnapshot {name} retry cleanup failed: {result.stderr.strip()[:300]}")
    while True:
        current = _optional_json(args, "-n", args.namespace, "get", "volumesnapshot", name)
        if current is None:
            return
        if time.monotonic() >= deadline:
            raise ConfigError(f"VolumeSnapshot {name} did not disappear before retry")
        time.sleep(2)


def _ensure_volume_snapshot(args, workload: str, claim_name: str, volume_name: str, deadline: float) -> dict:
    name = _snapshot_name(args, workload)
    retries = 0

    def create_snapshot() -> None:
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

    while True:
        if time.monotonic() >= deadline:
            raise ConfigError(f"VolumeSnapshot {name} was not ready before timeout")
        _wait_volume_detached(args, volume_name, deadline)
        current = _optional_json(args, "-n", args.namespace, "get", "volumesnapshot", name)
        if current is None:
            create_snapshot()
            continue
        if current.get("spec", {}).get("source", {}).get("persistentVolumeClaimName") != claim_name:
            raise ConfigError(f"existing VolumeSnapshot {name} points to a different PVC")
        labels = current.get("metadata", {}).get("labels", {})
        expected = {
            "modeling.cce/release": args.release,
            "modeling.cce/backup-group": args.snapshot_group,
        }
        if any(labels.get(key) != value for key, value in expected.items()):
            raise ConfigError(f"existing VolumeSnapshot {name} is not owned by this backup group")
        status = current.get("status", {})
        error_message = str(status.get("error", {}).get("message") or "").strip()
        if error_message:
            if not SNAPSHOT_TRANSIENT_ERROR.search(error_message) or retries >= SNAPSHOT_RETRY_LIMIT:
                raise ConfigError(f"VolumeSnapshot {name} failed: {error_message}")
            retries += 1
            _discard_snapshot_for_retry(args, name, deadline)
            if time.monotonic() >= deadline:
                raise ConfigError(f"VolumeSnapshot {name} retry deadline exceeded")
            time.sleep(min(5 * (2 ** (retries - 1)), 30))
            continue
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
    for workload in getattr(args, "backup_workloads", WORKLOADS):
        name = _snapshot_name(args, workload)
        run(
            "kubectl", "--context", args.context, "-n", args.namespace,
            "label", "volumesnapshot", name,
            f"modeling.cce/status={status}", "--overwrite",
        )


def _cleanup_old_successful_snapshots(args) -> list[str]:
    """Retain retired Core/Agent snapshots; prune complete active groups only."""
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
    active_groups = []
    for group, items in groups.items():
        workloads = {
            item.get("metadata", {}).get("labels", {}).get("modeling.cce/workload")
            for item in items
        }
        if len(items) == len(WORKLOADS) and workloads == set(WORKLOADS):
            active_groups.append((group, items))
    ordered = sorted(
        active_groups,
        key=lambda pair: max(
            item.get("metadata", {}).get("creationTimestamp", "") for item in pair[1]
        ),
        reverse=True,
    )
    stale = ordered[args.snapshot_retention:]
    warnings: list[str] = []
    for group, items in stale:
        for item in items:
            name = item.get("metadata", {}).get("name")
            if not name:
                continue
            result = run_program(
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
        expected_identity = {
            "schemaVersion": "cce-backup/v1", "context": args.context,
            "namespace": args.namespace, "releaseName": args.release,
        }
        if any(record.get(key) != value for key, value in expected_identity.items()):
            raise ConfigError("automatic backup record belongs to another operation or release")
        claims = record.get("claims")
        if not isinstance(claims, dict) or set(claims) not in map(set, SOURCE_WORKLOAD_SETS):
            raise ConfigError("automatic backup record has an invalid workload set")
        current_workloads = installed_workloads(installed_values(args))
        if set(claims) != set(current_workloads):
            raise ConfigError("automatic backup record workload set differs from installed release")
        if record.get("secretUids") and record["secretUids"] != _identity_record(args, merged):
            raise ConfigError("automatic backup record Secret identities changed")
        args.backup_workloads = tuple(name for name in LEGACY_EXPANDED_WORKLOADS if name in claims)
        args.snapshot_claims = {name: {key: value for key, value in claims[name].items()
                                       if key in ("claimName", "claimUid", "volumeName")}
                                for name in args.backup_workloads}
        args.snapshot_group = _snapshot_group(args)
        args.backup_record = backup_path
        return backup_path
    installed_release(args)
    old_values = installed_values(args)
    if old_values.get("global", {}).get("targetArch") != merged["global"]["targetArch"]:
        raise CompatibilityError("target architecture differs from the installed release")
    if old_values.get("global", {}).get("auth", {}) != merged["global"]["auth"]:
        raise CompatibilityError("shared auth settings changed during Modeling upgrade")
    args.backup_workloads = installed_workloads(old_values)
    if args.backup_workloads == LEGACY_WORKLOADS:
        for component in ("hub", "evaluation"):
            claim_name = merged[component]["persistence"]["existingClaim"]
            pods = kube(args, "get", "pods", "-n", args.namespace)
            if any(volume.get("persistentVolumeClaim", {}).get("claimName") == claim_name
                   for item in pods.get("items", []) for volume in item.get("spec", {}).get("volumes", [])):
                raise ConfigError(f"new {component} PVC is already mounted by a Pod")
    before = bindings(kube, args, merged, stopped=False, workloads=args.backup_workloads)
    assert_no_retired_claim_reuse(merged, before)
    _check_capacity(args)
    args.snapshot_group = _snapshot_group(args)
    snapshot_classes = _optional_json(args, "get", "volumesnapshotclass")
    classes = snapshot_classes.get("items", []) if snapshot_classes else []
    defaults = []
    for item in classes:
        annotations = item.get("metadata", {}).get("annotations", {})
        if str(annotations.get("snapshot.storage.kubernetes.io/is-default-class", "")).lower() == "true":
            defaults.append(item)
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
    _record_upgrade_stage(args, "preparing-backup", source=installed_release(args),
                          target={"releaseVersion": release["releaseVersion"],
                                  "chartVersion": release.get("chartVersion")}, claims=before)
    stopped_at = datetime.now(timezone.utc)
    _record_upgrade_stage(args, "stopping-writers", claims=before, dataEvidence=evidence)
    _stop_for_backup(args)
    _record_upgrade_stage(args, "writers-stopped", claims=before, dataEvidence=evidence)
    stopped = bindings(kube, args, merged, stopped=True, workloads=args.backup_workloads)
    if stopped != before:
        raise ConfigError("stopping Modeling changed a PVC or its bound PV")
    deadline = time.monotonic() + args.timeout_seconds
    claims = {}
    for workload in args.backup_workloads:
        claims[workload] = {
            **stopped[workload],
            **_ensure_volume_snapshot(
                args, workload, stopped[workload]["claimName"], stopped[workload]["volumeName"], deadline,
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
        "secretUids": _identity_record(args, merged) if "hub" in merged and "evaluation" in merged else {},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    backup_path.chmod(0o600)
    args.backup_record = backup_path
    _record_upgrade_stage(args, "backup-ready", backupRecord=str(backup_path))
    args.snapshot_claims = stopped
    return backup_path


def wait_old_pods_gone(args) -> None:
    deadline = time.monotonic() + args.timeout_seconds
    for component in getattr(args, "backup_workloads", WORKLOADS):
        while True:
            pods = kube(args, "get", "pods", "-n", args.namespace,
                        "-l", f"app.kubernetes.io/instance={args.release},app.kubernetes.io/component={component}")
            if not pods.get("items"):
                break
            if time.monotonic() >= deadline:
                raise ConfigError(f"old {component} Pods did not exit before timeout")
            time.sleep(2)


def migrate_core_projects(args, merged: dict, before: dict) -> dict | None:
    """Run the VM Onto migrator with both stopped CCE PVCs, before the target starts."""
    if "core" not in before:
        return None
    source = before["core"]["claimName"]
    target = before["onto"]["claimName"]
    suffix = hashlib.sha256(args.operation_id.encode()).hexdigest()[:12]
    prefix = f"{args.release}-core-migrate"
    name = f"{prefix[:63 - len(suffix) - 1].rstrip('-')}-{suffix}"
    expected_labels = {"app.kubernetes.io/instance": args.release,
                       "app.kubernetes.io/component": "core-migration",
                       "modeling.cce/operation": _snapshot_group(args)}
    image = merged["global"]["resolvedImages"]["ontoModeling"]
    report = f"/data/onto-web/.core-migration-{suffix}.json"
    pod = {
        "restartPolicy": "Never",
        "nodeSelector": {"kubernetes.io/arch": merged["global"]["targetArch"],
                         **merged["modeling"]["workloads"]["onto"]["nodeSelector"]},
        "containers": [{"name": "migration", "image": image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["python3", "scripts/migrate_cce_from_modeling.py",
                                    "--source", "/source", "--summary-file", report],
                        "volumeMounts": [{"name": "core", "mountPath": "/source", "readOnly": True},
                                         {"name": "onto", "mountPath": "/data"}]}],
        "volumes": [{"name": "core", "persistentVolumeClaim": {"claimName": source, "readOnly": True}},
                    {"name": "onto", "persistentVolumeClaim": {"claimName": target}}],
    }
    pull_secrets = merged["global"]["imageDelivery"]["pullSecretNames"]
    if pull_secrets:
        pod["imagePullSecrets"] = [{"name": item} for item in pull_secrets]
    job = {"apiVersion": "batch/v1", "kind": "Job",
           "metadata": {"name": name, "namespace": args.namespace, "labels": expected_labels},
           "spec": {"backoffLimit": 0, "activeDeadlineSeconds": args.timeout_seconds,
                    "template": {"metadata": {"labels": expected_labels}, "spec": pod}}}
    current = _optional_json(args, "-n", args.namespace, "get", "job", name)
    if current is not None:
        labels = current.get("metadata", {}).get("labels", {})
        current_spec = current.get("spec", {}).get("template", {}).get("spec", {})
        containers = current_spec.get("containers", [])
        expected_container = pod["containers"][0]
        container_matches = (len(containers) == 1 and all(
            containers[0].get(key) == expected_container[key]
            for key in ("name", "image", "command", "volumeMounts")
        ))
        metadata_matches = (current.get("metadata", {}).get("namespace") == args.namespace
                            and all(labels.get(key) == value for key, value in expected_labels.items()))
        job_matches = (current.get("spec", {}).get("backoffLimit") == 0
                       and current_spec.get("volumes") == pod["volumes"]
                       and container_matches)
        pod_matches = (current_spec.get("restartPolicy") == "Never"
                       and current_spec.get("nodeSelector") == pod["nodeSelector"]
                       and current_spec.get("imagePullSecrets", []) == pod.get("imagePullSecrets", []))
        if not metadata_matches or not job_matches or not pod_matches:
            raise ConfigError("existing Core migration Job differs from this operation")
    else:
        _run_with_input(("kubectl", "--context", args.context, "-n", args.namespace,
                         "create", "-f", "-"), json.dumps(job))
    args.migration_job = name
    run("kubectl", "--context", args.context, "-n", args.namespace,
        "wait", "--for=condition=complete", f"job/{name}", f"--timeout={args.timeout_seconds}s")
    output = run("kubectl", "--context", args.context, "-n", args.namespace,
                 "logs", f"job/{name}", "--tail=1")
    try:
        summary = json.loads(output.strip())
        evidence = json.loads(args.backup_record.read_text(encoding="utf-8"))["dataEvidence"]["core"]
        valid = (summary.get("schemaVersion") == "core-project-migration/v1"
                 and all(type(summary.get(key)) is int and summary[key] >= 0
                         for key in ("sourceProjects", "migrated", "skipped"))
                 and summary["migrated"] + summary["skipped"] == summary["sourceProjects"] == evidence)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigError("Core migration summary is missing or invalid") from exc
    if not valid:
        raise ConfigError("Core migration project count differs from the stopped source")
    run("kubectl", "--context", args.context, "-n", args.namespace,
        "delete", "job", name, "--cascade=foreground", "--wait=true", f"--timeout={args.timeout_seconds}s")
    args.migration_job = None
    return summary


def apply_upgrade(args, root: Path, release: dict, merged: dict, before: dict) -> None:
    helm_args = (*helm_base(args), "upgrade", args.release, str(root / "deploy/chart"),
                 "--reset-values",
                 "-f", str(args.values), "-f", str(args.resolved_images),
                 "--set-string", f"global.releaseVersion={release['releaseVersion']}",
                 "--set-string", f"global.operationId={args.operation_id}")
    args.restore_workloads_on_failure = True
    if getattr(args, "snapshot_claims", None) and args.snapshot_claims != before:
        raise ConfigError("backup snapshot PVC identities differ from upgrade precheck")
    args.helm_update_started = True
    _record_upgrade_stage(args, "helm-staging", backupRecord=str(args.backup_record))
    run(*helm_args, "--set", "modeling.workloads.onto.replicas=0",
        "--set", "hub.replicas=0", "--set", "evaluation.replicas=0",
        f"--timeout={args.timeout_seconds}s")
    wait_old_pods_gone(args)
    retained = tuple(name for name in before if name in WORKLOADS)
    assert_same_bindings({name: before[name] for name in retained},
                         bindings(kube, args, merged, stopped=True, workloads=retained))
    if "core" in before:
        _record_upgrade_stage(args, "migrating-core", backupRecord=str(args.backup_record))
        args.migration_summary = migrate_core_projects(args, merged, before)
    _record_upgrade_stage(args, "target-may-start", backupRecord=str(args.backup_record))
    run(*helm_args, "--wait", f"--timeout={args.timeout_seconds}s")
    verify(args, root)
    if "core" in before:
        verify_retired_workloads(args)
    assert_same_bindings({name: before[name] for name in retained},
                         bindings(kube, args, merged, stopped=False, workloads=retained))
    verify_persisted_data(run, args, args.backup_record)
    _record_upgrade_stage(args, "target-verified", backupRecord=str(args.backup_record))


def restore_workloads_after_failed_upgrade(args) -> dict:
    """Return the previously deployed release to service after an interrupted upgrade.

    A failed snapshot leaves the old Helm release deployed, so scaling suffices.
    Once Helm has changed the release, roll back its manifest before starting
    Pods: scaling the staging revision would start unverified target images.
    """
    issues = []
    if getattr(args, "migration_job", None):
        cleanup = run_program(
            ("kubectl", "--context", args.context, "-n", args.namespace,
             "delete", "job", args.migration_job, "--ignore-not-found", "--cascade=foreground", "--wait=true"),
            text=True, capture_output=True,
        )
        if cleanup.returncode:
            return {"status": "degraded", "issues": ["Core migration Job could not be stopped"]}
    previous_revision = getattr(args, "previous_helm_revision", None)
    if getattr(args, "helm_update_started", False):
        try:
            current = release_status(args)
            current_revision = current.get("version") if current else None
            if previous_revision is None or current_revision is None:
                issues.append("cannot establish Helm revision for automatic rollback")
            elif current_revision != previous_revision:
                run(*helm_base(args), "rollback", args.release,
                    str(previous_revision), "--wait", f"--timeout={args.timeout_seconds}s")
        except (RuntimeError, subprocess.SubprocessError) as exc:
            issues.append(f"Helm rollback failed: {exc}")
        if issues:
            return {"status": "degraded", "issues": issues}

    for workload in getattr(args, "backup_workloads", WORKLOADS):
        try:
            deployment = kube(args, "get", "deployment", f"{args.release}-{workload}", "-n", args.namespace)
            if deployment.get("spec", {}).get("replicas", 1) == 0:
                run("kubectl", "--context", args.context, "-n", args.namespace,
                    "scale", "deployment", f"{args.release}-{workload}", "--replicas=1")
        except (RuntimeError, subprocess.SubprocessError, KeyError, ValueError) as exc:
            issues.append(f"{workload} replica restore failed: {exc}")

    for workload in getattr(args, "backup_workloads", WORKLOADS):
        try:
            run("kubectl", "--context", args.context, "-n", args.namespace,
                "rollout", "status", f"deployment/{args.release}-{workload}",
                f"--timeout={args.timeout_seconds}s")
        except (RuntimeError, subprocess.SubprocessError) as exc:
            issues.append(f"{workload} did not become ready: {exc}")
    return {"status": "ready" if not issues else "degraded", "issues": issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "preflight", "diagnose", "install", "upgrade", "uninstall", "status", "verify"))
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
    if args.action not in ("status", "diagnose") and not args.operation_id:
        args.operation_id = str(uuid.uuid4())
    if args.action == "upgrade" and args.lock_holder and not args.transition_id:
        parser.error("delegated upgrade requires --transition-id")
    if args.action == "upgrade" and args.transition_record and not args.lock_holder:
        parser.error("--transition-record requires a delegated --lock-holder")
    if args.auto_backup and args.action != "upgrade":
        parser.error("--auto-backup is only supported for upgrade")
    if args.auto_backup and args.backup_record:
        parser.error("--auto-backup cannot be combined with --backup-record")
    if args.snapshot_retention < 1:
        parser.error("--snapshot-retention must be at least 1")
    if args.min_free_percent < 0 or args.min_free_percent > 100:
        parser.error("--min-free-percent must be between 0 and 100")
    if args.defer_external_services and args.action != "preflight":
        parser.error("--defer-external-services is only supported for preflight")
    if args.defer_external_services and args.transition_id:
        parser.error("--defer-external-services cannot be used for upgrade preflight")
    if args.resume and args.action not in ("install", "upgrade", "uninstall"):
        parser.error("--resume applies only to install, upgrade or uninstall")
    root = args.package_root or Path(__file__).resolve().parent.parent
    result = dict(schemaVersion="cce-operation/v1", operationId=args.operation_id, side="modeling",
                  action=args.action, status="failed", releaseName=args.release, namespace=args.namespace,
                  releaseVersion=None, chartVersion=None, phase="input", errorCode=None,
                  message="", retryable=False, endpoints=[], artifacts=[])
    operation_lease: OperationLease | None = None
    try:
        if not all((args.context, args.namespace, args.release)) or args.timeout_seconds < 1:
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
            dependency = _cluster_operation_state(args)
            if dependency and dependency.get("schemaVersion") == "cce-modeling-dependencies/v1":
                result["dependencyStage"] = dependency.get("stage")
                result["pendingDependencies"] = dependency.get("pendingDependencies", [])
        elif args.action == "uninstall":
            if not args.state_dir:
                raise ConfigError("state-dir required")
            result["phase"] = "uninstall"
            if not args.lock_holder:
                args.lock_holder = args.operation_id
                operation_lease = OperationLease(args, args.operation_id)
                operation_lease.acquire()
            release = _uninstall(args, root)
            result.update(releaseVersion=release["releaseVersion"], chartVersion=release["chartVersion"],
                          phase="uninstalled", message="Modeling release removed; PVCs and Secrets retained")
        else:
            if not args.state_dir:
                raise ConfigError("state-dir required")
            if args.action == "diagnose" and not args.state_dir.is_dir():
                raise ConfigError("diagnose requires an existing state-dir and does not create one")
            release, merged = load_inputs(args, root)
            result["releaseVersion"] = release["releaseVersion"]
            result["chartVersion"] = release["chartVersion"]
            result["endpoints"] = endpoint_results(root, args.release, args.namespace)
            if args.action == "plan":
                result["phase"] = "render"
                result["artifacts"] = [str(render(args, root, release))]
            elif args.action == "diagnose":
                result["phase"] = "diagnose"
                result["diagnosis"] = _diagnose(args, release, merged)
                result["message"] = "completed"
            else:
                result["phase"] = "preflight"
                # A standalone Modeling upgrade owns the same Lease used by
                # joint-cce.py.  The joint entry passes --lock-holder and
                # therefore keeps ownership outside this process.
                standalone_install = args.action == "install" and not args.lock_holder
                if args.action in ("install", "upgrade") and not args.lock_holder:
                    args.lock_holder = args.operation_id
                    args.standalone_upgrade = True
                    operation_lease = OperationLease(args, args.operation_id)
                    operation_lease.acquire()
                pending = preflight(args, merged,
                    check_external_services=standalone_install or not args.defer_external_services,
                    allow_missing_dependencies=standalone_install)
                if args.lock_holder and args.action in ("install", "upgrade"):
                    check_delegated_lease(args)
                current = release_status(args)
                if args.action == "upgrade" and current is not None:
                    args.previous_helm_revision = current.get("version")
                if args.action == "upgrade":
                    _require_upgrade_state_ready(args)
                if args.action == "preflight" and args.transition_id:
                    upgrade_precheck(args, root, release, merged)
                if args.action == "install":
                    if current and not args.resume:
                        raise ConfigError("release already exists; use explicit resume or upgrade")
                    if current and "hub" in merged and "evaluation" in merged:
                        _assert_unchanged_internal_secrets(args, merged)
                    if current and args.resume != args.operation_id:
                        raise ConfigError("resume operation ID mismatch")
                    if current and installed_values(args).get("global", {}).get("operationId") != args.operation_id:
                        raise ConfigError("existing release belongs to another operation")
                    result["phase"] = "install"
                    ensure_application_secrets(args, merged)
                    command = "upgrade" if current else "install"
                    run(*helm_base(args), command, args.release, str(root / "deploy/chart"),
                        "-f", str(args.values), "-f", str(args.resolved_images),
                        "--set-string", f"global.releaseVersion={release['releaseVersion']}",
                        "--set-string", f"global.operationId={args.operation_id}",
                        *([] if "externalAuth" in pending else ["--wait"]),
                        f"--timeout={args.timeout_seconds}s")
                    if standalone_install:
                        _record_dependencies(args, merged, pending, service_verified=False)
                    verify(args, root, pending_auth="externalAuth" in pending)
                    if standalone_install:
                        if "externalAuth" not in pending and "externalConsumer" not in pending:
                            try:
                                verify_consumer_from_modeling(args, merged)
                            except RuntimeError:
                                pending.append("externalConsumer")
                        _record_dependencies(args, merged, pending)
                        if pending:
                            result["phase"] = "waiting-dependencies"
                            result["pendingDependencies"] = pending
                elif args.action == "upgrade":
                    if "hub" in merged and "evaluation" in merged:
                        _assert_unchanged_internal_secrets(args, merged)
                    ensure_application_secrets(args, merged)
                    if args.auto_backup or (operation_lease and not args.backup_record):
                        result["phase"] = "automatic-backup"
                        backup_path = prepare_auto_backup(args, root, release, merged)
                        result["artifacts"].append(str(backup_path))
                    result["phase"] = "upgrade-precheck"
                    before = upgrade_precheck(args, root, release, merged)
                    if merged["modeling"]["workloads"]["onto"]["replicas"] != 1:
                        raise ConfigError("target values must request one replica per stateful workload")
                    result["phase"] = "upgrade"
                    if operation_lease:
                        operation_lease.check()
                    else:
                        check_delegated_lease(args)
                    apply_upgrade(args, root, release, merged, before)
                    if getattr(args, "migration_summary", None):
                        result["migration"] = args.migration_summary
                    if operation_lease:
                        operation_lease.check()
                    else:
                        check_delegated_lease(args)
                    if getattr(args, "snapshot_group", None):
                        _label_snapshot_group(args, "successful")
                        result["warnings"] = _cleanup_old_successful_snapshots(args)
                    if getattr(args, "upgrade_stage", None):
                        _record_upgrade_stage(args, "complete", backupRecord=str(args.backup_record))
                elif args.action == "verify":
                    if not current:
                        raise ConfigError("release absent")
                    _verify_dependency_record(args, merged)
                    result["phase"] = "verify"
                    verify(args, root)
                    state = _cluster_operation_state(args)
                    if state and state.get("schemaVersion") == "cce-modeling-dependencies/v1":
                        verify_consumer_from_modeling(args, merged)
                        _record_dependencies(args, merged, [])
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
        if args.action == "upgrade" and result["status"] == "failed" \
                and getattr(args, "restore_workloads_on_failure", False):
            try:
                result["recovery"] = restore_workloads_after_failed_upgrade(args)
            except Exception as exc:  # preserve the original upgrade error
                result["recovery"] = {"status": "failed", "issues": [str(exc)]}
            try:
                _record_upgrade_stage(args, "old-service-restored" if result["recovery"]["status"] == "ready"
                                      else "recovery-degraded")
            except Exception as exc:  # preserve the original upgrade error
                result.setdefault("warnings", []).append(f"operation state could not be recorded: {exc}")
        if operation_lease:
            operation_lease.release()
        if getattr(args, "stage_warnings", None):
            result.setdefault("warnings", []).extend(args.stage_warnings)
    emit_stdout(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
