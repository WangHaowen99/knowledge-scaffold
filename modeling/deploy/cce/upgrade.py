"""Checks for a Modeling upgrade that keeps the existing data volumes."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from common import CompatibilityError, ConfigError, read_yaml, validate_compatibility_manifest


WORKLOADS = ("core", "agent", "onto")
EXPANDED_WORKLOADS = (*WORKLOADS, "hub", "evaluation")


def installed_workloads(values: dict) -> tuple[str, ...]:
    """Only workloads present in the installed release need an old-data snapshot."""
    hub = bool(values.get("hub", {}).get("persistence", {}).get("existingClaim"))
    evaluation = bool(values.get("evaluation", {}).get("persistence", {}).get("existingClaim"))
    if hub != evaluation:
        raise ConfigError("installed Hub and Evaluation PVC configuration is incomplete")
    if hub:
        if values.get("hub", {}).get("replicas", 1) != 1 or values.get("evaluation", {}).get("replicas", 1) != 1:
            raise ConfigError("installed Hub and Evaluation must run at one replica")
        return EXPANDED_WORKLOADS
    return WORKLOADS


def target_claim(values: dict, workload: str) -> str:
    if workload in WORKLOADS:
        return values["modeling"]["workloads"][workload]["persistence"]["existingClaim"]
    return values[workload]["persistence"]["existingClaim"]


def mount_path(workload: str) -> str:
    return "/var/lib/evaluation" if workload == "evaluation" else "/data"


def transition(path: Path, transition_id: str, installed: dict, target: dict) -> dict:
    manifest = validate_compatibility_manifest(read_yaml(path))
    combinations = manifest.get("combinations", [])
    if not isinstance(combinations, list):
        raise CompatibilityError("compatibility combinations must be a list")
    by_id = {item.get("id"): item for item in combinations if isinstance(item, dict) and item.get("id")}
    if len(by_id) != len(combinations):
        raise CompatibilityError("compatibility combination IDs must be unique")
    matches = [item for item in manifest.get("transitions", [])
               if isinstance(item, dict) and item.get("id") == transition_id]
    if len(matches) != 1:
        raise CompatibilityError("exactly one verified upgrade transition ID required")
    item = matches[0]
    old = by_id.get(item.get("from"))
    new = by_id.get(item.get("to"))
    if not old or not new:
        raise CompatibilityError("transition references an unknown combination")
    expected_old = (installed["releaseVersion"], installed["chartVersion"])
    expected_new = (target["releaseVersion"], target["chartVersion"])
    if (old.get("modelingReleaseVersion"), old.get("modelingChartVersion")) != expected_old:
        raise CompatibilityError("installed Modeling version does not match transition start")
    if (new.get("modelingReleaseVersion"), new.get("modelingChartVersion")) != expected_new:
        raise CompatibilityError("target Modeling version does not match transition end")
    if old.get("platform") != target["platform"] or new.get("platform") != target["platform"]:
        raise CompatibilityError("upgrade cannot change CPU architecture")
    if item.get("steps") not in (["modeling"], ["consumer", "modeling"]):
        raise CompatibilityError("upgrade steps must keep Consumer before Modeling")
    if item.get("requiresJointMaintenance") is not True:
        raise CompatibilityError("upgrade transition must require joint maintenance")
    if item["steps"] == ["modeling"]:
        for key in ("consumerReleaseVersion", "consumerChartVersion"):
            if old.get(key) != new.get(key):
                raise CompatibilityError("Modeling-only upgrade cannot change Consumer version")
    if item.get("modelingDataMigration") != "none" or item.get("preserveModelingData") is not True:
        raise CompatibilityError("transition must verify existing Modeling data is readable without migration")
    return item


def bindings(kube, args, merged: dict, *, stopped: bool, workloads: tuple[str, ...] = WORKLOADS) -> dict:
    result = {}
    for workload in workloads:
        deployment = kube(args, "get", "deployment", f"{args.release}-{workload}", "-n", args.namespace)
        metadata = deployment.get("metadata", {})
        annotations = metadata.get("annotations", {})
        if (annotations.get("meta.helm.sh/release-name") != args.release or
                annotations.get("meta.helm.sh/release-namespace") != args.namespace):
            raise ConfigError(f"{workload} Deployment does not belong to this Helm release")
        spec = deployment["spec"]
        if spec.get("strategy", {}).get("type") != "Recreate":
            raise ConfigError(f"{workload} must use Recreate strategy")
        template = spec["template"]["spec"]
        volumes = [item for item in template.get("volumes", []) if item.get("name") == "data"]
        if len(volumes) != 1:
            raise ConfigError(f"{workload} has no unique data volume")
        claim_name = volumes[0].get("persistentVolumeClaim", {}).get("claimName")
        expected_claim = target_claim(merged, workload)
        if claim_name != expected_claim:
            raise ConfigError(f"{workload} would change its existing PVC")
        containers = template.get("containers", [])
        if not any(mount.get("name") == "data" and mount.get("mountPath") == mount_path(workload)
                   for container in containers for mount in container.get("volumeMounts", [])):
            raise ConfigError(f"{workload} no longer mounts its data at {mount_path(workload)}")
        if stopped:
            if spec.get("replicas", 1) != 0 or deployment.get("status", {}).get("replicas", 0) != 0:
                raise ConfigError(f"stop the old {workload} Deployment before taking snapshots")
            pods = kube(args, "get", "pods", "-n", args.namespace,
                        "-l", f"app.kubernetes.io/instance={args.release},app.kubernetes.io/component={workload}")
            if pods.get("items"):
                raise ConfigError(f"old {workload} Pods are still present")
        pvc = kube(args, "get", "pvc", claim_name, "-n", args.namespace)
        if pvc.get("status", {}).get("phase") != "Bound":
            raise ConfigError(f"{workload} PVC is not Bound")
        uid = pvc.get("metadata", {}).get("uid")
        pv = pvc.get("spec", {}).get("volumeName")
        if not uid or not pv:
            raise ConfigError(f"{workload} PVC identity is incomplete")
        result[workload] = {"claimName": claim_name, "claimUid": uid, "volumeName": pv}
    return result


def verified_snapshots(kube, args, path: Path, current_bindings: dict) -> None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read backup record") from exc
    if not isinstance(record, dict):
        raise ConfigError("backup record must be a JSON object")
    expected_identity = {
        "schemaVersion": "cce-backup/v1", "context": args.context,
        "namespace": args.namespace, "releaseName": args.release,
    }
    if any(record.get(key) != value for key, value in expected_identity.items()):
        raise ConfigError("backup record is for another cluster or release")
    try:
        stopped_at = datetime.fromisoformat(record["stoppedAt"].replace("Z", "+00:00"))
        if stopped_at.tzinfo is None:
            raise ValueError("timezone missing")
    except (KeyError, AttributeError, ValueError) as exc:
        raise ConfigError("backup record needs a timezone-aware stoppedAt") from exc
    claims = record.get("claims", {})
    workloads = tuple(current_bindings)
    if not isinstance(claims, dict) or set(claims) != set(workloads):
        raise ConfigError("backup record must cover all installed Modeling PVCs")
    evidence = record.get("dataEvidence", {})
    if not isinstance(evidence, dict) or set(evidence) != set(workloads):
        raise ConfigError("backup record must include pre-upgrade data evidence for all workloads")
    for workload in workloads:
        count = evidence.get(workload)
        if type(count) is not int or count < 0:
            raise ConfigError(f"{workload} data evidence must be a nonnegative count")
    for workload in workloads:
        saved = claims[workload]
        if not isinstance(saved, dict) or any(saved.get(key) != value
                for key, value in current_bindings[workload].items()):
            raise ConfigError(f"{workload} backup does not match the mounted PVC and PV")
        name = saved.get("snapshotName")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{workload} has no VolumeSnapshot name")
        snapshot = kube(args, "get", "volumesnapshot", name, "-n", args.namespace)
        try:
            created_at = datetime.fromisoformat(snapshot["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError) as exc:
            raise ConfigError(f"{workload} VolumeSnapshot has no creation time") from exc
        source_matches = snapshot.get("spec", {}).get("source", {}).get("persistentVolumeClaimName") == saved["claimName"]
        identity_matches = snapshot.get("metadata", {}).get("uid") == saved.get("snapshotUid")
        status = snapshot.get("status", {})
        usable = status.get("readyToUse") is True and bool(status.get("boundVolumeSnapshotContentName"))
        identity_valid = source_matches and identity_matches and created_at >= stopped_at
        if not identity_valid or not usable:
            raise ConfigError(f"{workload} VolumeSnapshot is not ready for restoration")


def assert_same_bindings(before: dict, after: dict) -> None:
    if before != after:
        raise ConfigError("upgrade changed a Modeling PVC or its bound PV")


DATA_COUNTS = {
    "core": ("node", "--experimental-sqlite", "-e",
             "const {DatabaseSync}=require('node:sqlite');const d=new DatabaseSync('/data/modeling.db',{readOnly:true});"
             "if(d.prepare('PRAGMA quick_check').get().quick_check!=='ok')process.exit(2);"
             "console.log(d.prepare('SELECT COUNT(*) AS n FROM projects').get().n)"),
    "agent": ("python", "-c",
              "import sqlite3; d=sqlite3.connect('file:/data/runtime.sqlite?mode=ro',uri=True);"
              "assert d.execute('PRAGMA quick_check').fetchone()[0]=='ok';"
              "print(d.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0])"),
    "onto": ("python", "-c",
             "from pathlib import Path; p=Path('/data/onto-web/workspaces');"
             "assert p.is_dir();print(sum(d.is_dir() and (d/'.onto-web.json').is_file() for d in p.iterdir()))"),
    "hub": ("python", "-c",
            "from pathlib import Path; p=Path('/data/knowledge-hub/projects');"
            "print(sum(x.is_dir() for x in p.iterdir()) if p.exists() else 0)"),
    "evaluation": ("python", "-c",
                   "import sqlite3; d=sqlite3.connect('file:/var/lib/evaluation/evaluation.sqlite?mode=ro',uri=True);"
                   "assert d.execute('PRAGMA quick_check').fetchone()[0]=='ok';"
                   "print(d.execute('SELECT COUNT(*) FROM records').fetchone()[0])"),
}


def verify_persisted_data(run, args, backup_record: Path) -> None:
    record = json.loads(backup_record.read_text(encoding="utf-8"))
    for workload in record["dataEvidence"]:
        output = run("kubectl", "--context", args.context, "-n", args.namespace,
                     "exec", f"deployment/{args.release}-{workload}", "-c", workload,
                     "--", *DATA_COUNTS[workload])
        try:
            count = int(output.strip())
        except ValueError as exc:
            raise ConfigError(f"{workload} returned invalid persisted data evidence") from exc
        if count < record["dataEvidence"][workload]:
            raise ConfigError(f"{workload} persisted record count decreased after upgrade")
