"""Synthetic CCE package and cross-release contract checks."""

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import ConfigError, checksum, safe_extract, validate_release, validate_compatibility_manifest  # noqa: E402


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


JOINT = load_script("joint-cce.py")
INSTALL = load_script("install-cce.py")
PACKAGE = load_script("package-cce.py")
UPGRADE = load_script("upgrade.py")


class CceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cce-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def package(self):
        root = self.base / "modeling-cce-demo-amd64"
        (root / "images").mkdir(parents=True)
        records = {}
        for image_id in ("modeling", "modelingAgent", "ontoModeling"):
            image = root / "images" / f"{image_id}.tar"
            image.write_bytes(b"synthetic image archive")
            records[image_id] = dict(archive=str(image.relative_to(root)), archiveSha256=checksum(image),
                archiveRef=f"example/{image_id}:demo", sourceManifestDigest="sha256:" + "a" * 64,
                configDigest="sha256:" + "b" * 64, platform="linux/amd64", purpose="business")
        release = dict(schemaVersion="cce-release/v1", releaseVersion="demo", chartVersion="0.1.0",
                       platform="linux-amd64", components=list(records), images=records,
                       imageDependencies={"jiuwenbox": {"sourceSide": "consumer", "sourceImageId": "jiuwenbox"}})
        (root / "release.yaml").write_text(yaml.safe_dump(release))
        (root / "checksums.sha256").write_text("".join(
            f"{checksum(path)}  {path.relative_to(root)}\n" for path in sorted(root.rglob("*")) if path.is_file() and path.name != "checksums.sha256"))
        return root

    def upgrade_fixture(self):
        combinations = [
            {"id": "old", "consumerReleaseVersion": "c1", "consumerChartVersion": "1.0.0",
             "modelingReleaseVersion": "m1", "modelingChartVersion": "1.0.0", "platform": "linux-amd64",
             "operationSchemaVersion": "cce-operation/v1", "endpointSchemaVersion": "cce-endpoints/v1",
             "authApiVersion": "v1", "consumerApiVersion": "v1"},
            {"id": "middle", "consumerReleaseVersion": "c2", "consumerChartVersion": "2.0.0",
             "modelingReleaseVersion": "m1", "modelingChartVersion": "1.0.0", "platform": "linux-amd64",
             "operationSchemaVersion": "cce-operation/v1", "endpointSchemaVersion": "cce-endpoints/v1",
             "authApiVersion": "v1", "consumerApiVersion": "v1"},
            {"id": "new", "consumerReleaseVersion": "c2", "consumerChartVersion": "2.0.0",
             "modelingReleaseVersion": "m2", "modelingChartVersion": "2.0.0", "platform": "linux-amd64",
             "operationSchemaVersion": "cce-operation/v1", "endpointSchemaVersion": "cce-endpoints/v1",
             "authApiVersion": "v1", "consumerApiVersion": "v1"},
        ]
        step = {"id": "upgrade-demo", "from": "old", "to": "new", "intermediateCombinationId": "middle",
                "steps": ["consumer", "modeling"], "requiresJointMaintenance": True,
                "modelingDataMigration": "none", "preserveModelingData": True}
        path = self.base / "compatibility.yaml"
        path.write_text(yaml.safe_dump({"schemaVersion": "cce-compatibility/v1",
                                        "combinations": combinations, "transitions": [step]}))
        installed = {"consumer": {"releaseVersion": "c1", "chartVersion": "1.0.0"},
                     "modeling": {"releaseVersion": "m1", "chartVersion": "1.0.0"}}
        packages = {"consumer": (self.base, {"releaseVersion": "c2", "chartVersion": "2.0.0"}),
                    "modeling": (self.base, {"releaseVersion": "m2", "chartVersion": "2.0.0", "platform": "linux-amd64"})}
        return path, installed, packages

    def test_upgrade_transition_requires_consumer_then_modeling_and_no_data_migration(self):
        path, installed, packages = self.upgrade_fixture()
        config = {"compatibilityFile": path}
        JOINT.verified_joint_transition(config, packages, "upgrade-demo", installed)
        manifest = yaml.safe_load(path.read_text())
        manifest["transitions"][0]["steps"] = ["modeling", "consumer"]
        path.write_text(yaml.safe_dump(manifest))
        with self.assertRaises(ConfigError):
            JOINT.verified_joint_transition(config, packages, "upgrade-demo", installed)
        manifest["transitions"][0]["steps"] = ["consumer", "modeling"]
        manifest["transitions"][0]["modelingDataMigration"] = "required"
        path.write_text(yaml.safe_dump(manifest))
        with self.assertRaises(ConfigError):
            JOINT.verified_joint_transition(config, packages, "upgrade-demo", installed)

    def test_installed_identity_accepts_consumer_chart_name_from_own_repository(self):
        path, installed, _ = self.upgrade_fixture()
        config = {"compatibilityFile": path, "cluster": {"context": "synthetic"},
                  "consumer": {"namespace": "consumer-demo", "releaseName": "consumer"}}
        def response(*command):
            if "status" in command:
                return json.dumps({"info": {"status": "deployed"}, "chart": "knowledge-consumer-1.0.0"})
            return json.dumps({"global": {"releaseVersion": "c1"}})
        with mock.patch.object(JOINT, "run", side_effect=response):
            self.assertEqual(JOINT.installed_identity(config, "consumer"), installed["consumer"])

    def test_upgrade_rejects_changed_claim_and_requires_ready_snapshots(self):
        args = SimpleNamespace(release="modeling", namespace="demo", context="demo")
        merged = {"modeling": {"workloads": {name: {"persistence": {"existingClaim": f"{name}-data"}}
                                           for name in UPGRADE.WORKLOADS}}}

        def resource(_, action, kind, name, *rest):
            if kind == "deployment":
                component = name.removeprefix("modeling-")
                return {"metadata": {"annotations": {"meta.helm.sh/release-name": "modeling",
                          "meta.helm.sh/release-namespace": "demo"}},
                        "spec": {"replicas": 0, "strategy": {"type": "Recreate"},
                          "template": {"spec": {"volumes": [{"name": "data", "persistentVolumeClaim":
                            {"claimName": f"{component}-data"}}], "containers": [{"volumeMounts":
                            [{"name": "data", "mountPath": "/data"}]}]}}}, "status": {"replicas": 0}}
            if kind == "pods":
                return {"items": []}
            if kind == "pvc":
                return {"metadata": {"uid": f"uid-{name}"}, "spec": {"volumeName": f"pv-{name}"},
                        "status": {"phase": "Bound"}}
            if kind == "volumesnapshot":
                component = name.removesuffix("-snapshot")
                return {"metadata": {"uid": f"snapshot-uid-{component}", "creationTimestamp": "2026-10-15T08:00:00Z"},
                        "spec": {"source": {"persistentVolumeClaimName": f"{component}-data"}},
                        "status": {"readyToUse": True, "boundVolumeSnapshotContentName": f"content-{component}"}}
            raise AssertionError((action, kind, name, rest))

        before = UPGRADE.bindings(resource, args, merged, stopped=True)
        record = {"schemaVersion": "cce-backup/v1", "context": "demo", "namespace": "demo",
                  "releaseName": "modeling", "stoppedAt": "2026-10-15T07:59:00Z",
                  "dataEvidence": {"core": 3, "agent": 2, "onto": 1},
                  "claims": {name: {**before[name], "snapshotName": f"{name}-snapshot",
                                    "snapshotUid": f"snapshot-uid-{name}"} for name in UPGRADE.WORKLOADS}}
        path = self.base / "backup.json"
        path.write_text(json.dumps(record))
        UPGRADE.verified_snapshots(resource, args, path, before)
        changed = json.loads(json.dumps(merged))
        changed["modeling"]["workloads"]["core"]["persistence"]["existingClaim"] = "other-data"
        with self.assertRaises(ConfigError):
            UPGRADE.bindings(resource, args, changed, stopped=True)
        record["claims"]["core"]["snapshotUid"] = "wrong-uid"
        path.write_text(json.dumps(record))
        with self.assertRaises(ConfigError):
            UPGRADE.verified_snapshots(resource, args, path, before)

    def test_upgrade_data_counts_must_not_decrease(self):
        backup = self.base / "backup.json"
        backup.write_text(json.dumps({"dataEvidence": {"core": 3, "agent": 2, "onto": 1}}))
        args = SimpleNamespace(context="demo", namespace="demo", release="modeling")
        calls = []

        def command(*argv):
            workload = next(name for name in UPGRADE.WORKLOADS if f"deployment/modeling-{name}" in argv)
            calls.append(workload)
            return {"core": "3\n", "agent": "2\n", "onto": "1\n"}[workload]

        UPGRADE.verify_persisted_data(command, args, backup)
        self.assertEqual(calls, list(UPGRADE.WORKLOADS))
        backup.write_text(json.dumps({"dataEvidence": {"core": 4, "agent": 2, "onto": 1}}))
        with self.assertRaises(ConfigError):
            UPGRADE.verify_persisted_data(command, args, backup)

    def test_upgrade_keeps_pods_stopped_until_pvc_binding_is_checked(self):
        args = SimpleNamespace(context="demo", namespace="modeling-demo", release="modeling",
                               values=self.base / "values.yaml", resolved_images=self.base / "images.yaml",
                               operation_id="synthetic-op", timeout_seconds=120,
                               backup_record=self.base / "backup.json")
        calls = []
        before = {"core": {"volumeName": "pv-core"}}

        def fake_run(*command):
            calls.append(command)
            return ""

        with mock.patch.object(INSTALL, "run", side_effect=fake_run), \
             mock.patch.object(INSTALL, "wait_old_pods_gone") as wait_stopped, \
             mock.patch.object(INSTALL, "bindings", return_value=before) as current_bindings, \
             mock.patch.object(INSTALL, "verify"), \
             mock.patch.object(INSTALL, "verify_persisted_data"):
            INSTALL.apply_upgrade(args, self.base, {"releaseVersion": "m2"}, {}, before)
        upgrade_calls = [command for command in calls if "upgrade" in command]
        self.assertEqual(len(upgrade_calls), 2)
        self.assertIn("modeling.workloads.core.replicas=0", upgrade_calls[0])
        self.assertIn("--wait", upgrade_calls[1])
        self.assertNotIn("--wait", upgrade_calls[0])
        self.assertEqual([call.kwargs["stopped"] for call in current_bindings.call_args_list], [True, False])
        wait_stopped.assert_called_once_with(args)
        calls.clear()
        with mock.patch.object(INSTALL, "run", side_effect=fake_run), \
             mock.patch.object(INSTALL, "wait_old_pods_gone"), \
             mock.patch.object(INSTALL, "bindings", return_value={"core": {"volumeName": "other-pv"}}), \
             mock.patch.object(INSTALL, "verify"):
            with self.assertRaises(ConfigError):
                INSTALL.apply_upgrade(args, self.base, {"releaseVersion": "m2"}, {}, before)
        self.assertEqual(len([command for command in calls if "upgrade" in command]), 1)

    def test_joint_upgrade_runs_consumer_before_modeling_and_keeps_failure_phase(self):
        config = {"stateDir": self.base, "cluster": {"context": "synthetic"},
                  "compatibilityFile": self.base / "compatibility.yaml"}
        for side in JOINT.JOINT_SIDE_ORDER:
            config[side] = {"package": self.base / f"{side}.tar.gz", "releaseName": side,
                            "namespace": f"{side}-demo", "values": self.base / f"{side}-values.yaml",
                            "resolvedImages": self.base / f"{side}-images.yaml"}
        releases = {"consumer": {"releaseVersion": "c2", "chartVersion": "2.0.0", "platform": "linux-amd64"},
                    "modeling": {"releaseVersion": "m2", "chartVersion": "2.0.0", "platform": "linux-amd64"}}
        installed = {"consumer": {"releaseVersion": "c1", "chartVersion": "1.0.0"},
                     "modeling": {"releaseVersion": "m1", "chartVersion": "1.0.0"}}
        calls = []

        class FakeLease:
            def __init__(self, *_):
                pass

            def acquire(self):
                pass

            def check(self):
                pass

            def release(self):
                pass

        def fake_invoke(side, action, _config, package, *_args, **kwargs):
            calls.append((side, action, kwargs.get("lock_holder")))
            if side == "consumer" and action == "upgrade" and self.consumer_fails:
                raise RuntimeError("synthetic Consumer upgrade failure")
            if side == "consumer" and action == "verify" and self.consumer_verify_fails:
                raise RuntimeError("synthetic Consumer verification failure")
            return {"schemaVersion": "cce-operation/v1", "status": "succeeded",
                    "releaseVersion": package[1]["releaseVersion"], "chartVersion": package[1]["chartVersion"]}

        original = sys.argv
        try:
            sys.argv = ["joint-cce.py", "upgrade", "--config", str(self.base / "deployment.yaml"),
                        "--scope", "both", "--transition-id", "upgrade-demo",
                        "--backup-record", str(self.base / "modeling-backup.json")]
            from contextlib import redirect_stdout
            from io import StringIO
            with mock.patch.object(JOINT, "load_config", return_value=config), \
                 mock.patch.object(JOINT, "safe_extract", side_effect=lambda path, *_: self.base / path.name.removesuffix(".tar.gz")), \
                 mock.patch.object(JOINT, "validate_release", side_effect=lambda _root, side: releases[side]), \
                 mock.patch.object(JOINT, "validate_combination"), \
                 mock.patch.object(JOINT, "validate_cross_side"), \
                 mock.patch.object(JOINT, "read_yaml", side_effect=lambda path: {"global": {"clusterFlavor": "standard", "access": {
                     "elb": {"id": "synthetic"}, "listenerPort": 443 if "consumer" in str(path) else 8443}}}), \
                 mock.patch.object(JOINT, "effective_images", return_value=self.base / "effective.yaml"), \
                 mock.patch.object(JOINT, "operation_fingerprint", return_value="synthetic"), \
                 mock.patch.object(JOINT, "checksum", return_value="synthetic"), \
                 mock.patch.object(JOINT, "installed_identity", side_effect=lambda _config, side: installed[side]), \
                 mock.patch.object(JOINT, "verified_joint_transition"), \
                 mock.patch.object(JOINT, "Lease", FakeLease), \
                 mock.patch.object(JOINT, "invoke", side_effect=fake_invoke):
                self.consumer_fails = False
                self.consumer_verify_fails = False
                output = StringIO()
                with redirect_stdout(output):
                    code = JOINT.main()
                self.assertEqual(code, 0, output.getvalue())
                self.assertEqual([(side, action) for side, action, _ in calls],
                                 [("consumer", "preflight"), ("modeling", "preflight"), ("consumer", "upgrade"),
                                  ("consumer", "verify"), ("modeling", "upgrade"), ("modeling", "verify")])
                operation_id = json.loads(output.getvalue())["operationId"]
                self.assertEqual([holder for side, action, holder in calls if action == "verify"],
                                 [operation_id, operation_id])
                calls.clear()
                self.consumer_fails = True
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(JOINT.main(), 1)
                self.assertEqual([(side, action) for side, action, _ in calls],
                                 [("consumer", "preflight"), ("modeling", "preflight"), ("consumer", "upgrade")])
                self.assertEqual(json.loads(output.getvalue())["phase"], "consumer")
                calls.clear()
                self.consumer_fails = False
                self.consumer_verify_fails = True
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(JOINT.main(), 1)
                self.assertEqual([(side, action) for side, action, _ in calls],
                                 [("consumer", "preflight"), ("modeling", "preflight"), ("consumer", "upgrade"),
                                  ("consumer", "verify")])
                self.assertEqual(json.loads(output.getvalue())["phase"], "consumer")
        finally:
            sys.argv = original

    def test_package_validates_and_detects_tampering(self):
        root = self.package()
        self.assertEqual(validate_release(root, "modeling")["platform"], "linux-amd64")
        (root / "images/modeling.tar").write_bytes(b"changed")
        with self.assertRaises(ConfigError):
            validate_release(root, "modeling")

    def test_release_platform_and_image_platform_are_distinct(self):
        root = self.package()
        release_path = root / "release.yaml"
        release = yaml.safe_load(release_path.read_text())
        for bad_platform in ("linux/amd64", "linux-arm64"):
            release["platform"] = bad_platform
            release_path.write_text(yaml.safe_dump(release))
            self._refresh_checksums(root)
            with self.assertRaises(ConfigError):
                validate_release(root, "modeling")
        release["platform"] = "linux-amd64"
        release["images"]["modeling"]["platform"] = "linux/arm64"
        release_path.write_text(yaml.safe_dump(release))
        self._refresh_checksums(root)
        with self.assertRaises(ConfigError):
            validate_release(root, "modeling")

    def _refresh_checksums(self, root):
        (root / "checksums.sha256").write_text("".join(
            f"{checksum(path)}  {path.relative_to(root)}\n" for path in sorted(root.rglob("*"))
            if path.is_file() and path.name != "checksums.sha256"))

    def test_compatibility_rejects_empty_duplicate_and_incomplete_combinations(self):
        path, _, _ = self.upgrade_fixture()
        manifest = yaml.safe_load(path.read_text())
        validate_compatibility_manifest(manifest)
        for combinations in ([], manifest["combinations"] + [dict(manifest["combinations"][0])],
                             [{key: value for key, value in manifest["combinations"][0].items()
                               if key != "consumerApiVersion"}]):
            changed = {**manifest, "combinations": combinations}
            with self.assertRaises(ConfigError):
                validate_compatibility_manifest(changed)

    def test_consumer_service_names_follow_release_identity(self):
        root = self.base / "consumer-cce-demo"
        (root / "deploy").mkdir(parents=True)
        (root / "deploy/endpoints.yaml").write_text(yaml.safe_dump({"schemaVersion": "cce-endpoints/v1",
            "endpoints": [{"id": "auth", "port": 5056, "apiVersion": "v1"},
                          {"id": "consumer-api", "port": 5055, "apiVersion": "v1"}]}))
        package = {"consumer": (root, {})}
        config = {"consumer": {"releaseName": "custom-consumer", "namespace": "consumer-demo"}}
        consumer_values = {"global": {"auth": {"issuer": "https://auth.example.com", "audience": "demo"}}}
        modeling_values = {"global": {"auth": dict(consumer_values["global"]["auth"])},
            "externalAuth": {"serviceName": "custom-consumer-auth", "namespace": "consumer-demo", "servicePort": 5056},
            "externalConsumer": {"serviceName": "custom-consumer-backend", "namespace": "consumer-demo", "servicePort": 5055}}
        combo = {"authApiVersion": "v1", "consumerApiVersion": "v1"}
        JOINT.validate_cross_side(config, package, consumer_values, modeling_values, combo)
        for field, key, value in (("externalAuth", "servicePort", 9999),
                                  ("externalConsumer", "serviceName", "consumer-api"),
                                  ("externalConsumer", "namespace", "other-demo")):
            bad = json.loads(json.dumps(modeling_values))
            bad[field][key] = value
            with self.assertRaises(ConfigError):
                JOINT.validate_cross_side(config, package, consumer_values, bad, combo)

    def test_modeling_preflight_checks_existing_resources_when_consumer_services_are_deferred(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo")
        merged = yaml.safe_load((ROOT / "values.customer.example.yaml").read_text())
        def resource(_args, verb, kind, *rest):
            if kind == "namespace":
                return {"status": {"phase": "Active"}}
            if kind == "pvc":
                return {"status": {"phase": "Bound"},
                        "spec": {"volumeMode": "Filesystem", "accessModes": ["ReadWriteOnce"]}}
            if kind == "secret":
                return {"data": {"service-token": "synthetic"}}
            if kind == "nodes":
                return {"items": [{"metadata": {"labels": {"kubernetes.io/arch": "amd64"}},
                                   "spec": {"unschedulable": False}}]}
            if kind == "service":
                raise ConfigError("Consumer Service is missing")
            raise AssertionError((verb, kind, rest))
        with mock.patch.object(INSTALL, "kube", side_effect=resource) as calls:
            INSTALL.preflight(args, merged, check_external_services=False)
            self.assertFalse(any(call.args[1:3] == ("get", "service") for call in calls.call_args_list))
            self.assertEqual(sum(call.args[2] == "pvc" for call in calls.call_args_list), 3)
            self.assertTrue(any(call.args[2] == "nodes" for call in calls.call_args_list))
            with self.assertRaises(ConfigError):
                INSTALL.preflight(args, merged)
        def wrong_port(_args, verb, kind, *rest):
            if kind == "service":
                return {"spec": {"ports": [{"port": 9999}]}}
            return resource(_args, verb, kind, *rest)
        with mock.patch.object(INSTALL, "kube", side_effect=wrong_port):
            with self.assertRaisesRegex(ConfigError, "service port mismatch"):
                INSTALL.preflight(args, merged)
        def ready_service(_args, verb, kind, *rest):
            if kind == "service":
                return {"spec": {"ports": [{"port": 5056 if rest[0] == "consumer-auth" else 5055}]}}
            return resource(_args, verb, kind, *rest)
        with mock.patch.object(INSTALL, "kube", side_effect=ready_service):
            INSTALL.preflight(args, merged)

    def test_deferred_preflight_flag_only_reaches_modeling(self):
        root = self.base / "modeling-cce-demo"
        (root / "deploy").mkdir(parents=True)
        (root / "deploy/install-cce.sh").write_text("#!/bin/sh\n")
        config = {"cluster": {"context": "synthetic"}, "stateDir": self.base, "timeoutSeconds": 30,
                  "modeling": {"namespace": "modeling-demo", "releaseName": "modeling",
                               "values": self.base / "values.yaml"}}
        package = (root, {"releaseVersion": "m2", "chartVersion": "2.0.0"})
        response = SimpleNamespace(returncode=0, stdout=json.dumps({"schemaVersion": "cce-operation/v1",
            "status": "succeeded", "releaseVersion": "m2", "chartVersion": "2.0.0"}))
        with mock.patch.object(JOINT.subprocess, "run", return_value=response) as call:
            JOINT.invoke("modeling", "preflight", config, package, "operation-demo",
                         self.base / "images.yaml", defer_external_services=True)
        self.assertIn("--defer-external-services", call.call_args.args[0])
        with self.assertRaises(ConfigError):
            JOINT.invoke("modeling", "install", config, package, "operation-demo",
                         self.base / "images.yaml", defer_external_services=True)

    def test_initial_joint_preflight_defers_only_when_consumer_release_and_services_are_absent(self):
        config = {"cluster": {"context": "synthetic"},
                  "consumer": {"namespace": "consumer-demo", "releaseName": "consumer"}}
        absent = SimpleNamespace(returncode=1, stdout="", stderr="Error: release: not found")
        services = SimpleNamespace(returncode=0, stdout=json.dumps({"items": []}), stderr="")
        with mock.patch.object(JOINT.subprocess, "run", side_effect=(absent, services)) as commands:
            self.assertTrue(JOINT.defer_consumer_services(config))
            self.assertEqual(commands.call_count, 2)
        present = SimpleNamespace(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(JOINT.subprocess, "run", return_value=present) as commands:
            self.assertFalse(JOINT.defer_consumer_services(config))
            commands.assert_called_once()
        partial = SimpleNamespace(returncode=0, stdout=json.dumps({"items": [
            {"metadata": {"name": "consumer-auth"}}]}), stderr="")
        with mock.patch.object(JOINT.subprocess, "run", side_effect=(absent, partial)):
            with self.assertRaisesRegex(ConfigError, "without its Helm release"):
                JOINT.defer_consumer_services(config)

    def test_joint_install_checks_consumer_services_before_modeling_install(self):
        config = {"stateDir": self.base, "cluster": {"context": "synthetic"},
                  "compatibilityFile": self.base / "compatibility.yaml"}
        releases = {}
        for side in JOINT.JOINT_SIDE_ORDER:
            config[side] = {"package": self.base / f"{side}.tar.gz", "releaseName": side,
                            "namespace": f"{side}-demo", "values": self.base / f"{side}-values.yaml",
                            "resolvedImages": self.base / f"{side}-images.yaml"}
            releases[side] = {"releaseVersion": "demo", "chartVersion": "0.1.0"}

        class FakeLease:
            def __init__(self, *_):
                pass
            def acquire(self):
                pass
            def check(self):
                pass
            def release(self):
                pass

        calls = []
        service_ready = True
        def fake_invoke(side, action, _config, package, *_args, **kwargs):
            calls.append((side, action, kwargs))
            if side == "modeling" and action == "preflight" and not service_ready:
                raise ConfigError("Consumer Service is missing or has an unexpected port")
            return {"schemaVersion": "cce-operation/v1", "status": "succeeded",
                    "releaseVersion": package[1]["releaseVersion"], "chartVersion": package[1]["chartVersion"]}

        original = sys.argv
        try:
            with mock.patch.object(JOINT, "load_config", return_value=config), \
                 mock.patch.object(JOINT, "safe_extract", side_effect=lambda path, *_: self.base / path.name), \
                 mock.patch.object(JOINT, "validate_release", side_effect=lambda _root, side: releases[side]), \
                 mock.patch.object(JOINT, "validate_combination", return_value={}), \
                 mock.patch.object(JOINT, "validate_cross_side"), \
                 mock.patch.object(JOINT, "read_yaml", side_effect=lambda path: {"global": {"clusterFlavor": "standard",
                     "access": {"elb": {"id": "synthetic"},
                                "listenerPort": 80 if "consumer" in str(path) else 8080}}}), \
                 mock.patch.object(JOINT, "effective_images", return_value=self.base / "effective.yaml"), \
                 mock.patch.object(JOINT, "operation_fingerprint", return_value="synthetic"), \
                 mock.patch.object(JOINT, "defer_consumer_services", return_value=True), \
                 mock.patch.object(JOINT, "Lease", FakeLease), \
                 mock.patch.object(JOINT, "invoke", side_effect=fake_invoke):
                sys.argv = ["joint-cce.py", "preflight", "--config", str(self.base / "joint.yaml")]
                with redirect_stdout(StringIO()):
                    self.assertEqual(JOINT.main(), 0)
                self.assertEqual([(side, action) for side, action, _ in calls],
                                 [("consumer", "preflight"), ("modeling", "preflight")])
                self.assertTrue(calls[1][2]["defer_external_services"])
                calls.clear()
                sys.argv[1] = "install"
                with redirect_stdout(StringIO()):
                    self.assertEqual(JOINT.main(), 0)
                self.assertEqual([(side, action) for side, action, _ in calls],
                                 [("consumer", "install"), ("modeling", "preflight"), ("modeling", "install")])
                self.assertTrue(calls[1][2]["lock_holder"])
                self.assertFalse(calls[2][2]["defer_external_services"])
                calls.clear()
                service_ready = False
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(JOINT.main(), 1)
                self.assertEqual([(side, action) for side, action, _ in calls],
                                 [("consumer", "install"), ("modeling", "preflight")])
                self.assertEqual(json.loads(output.getvalue())["phase"], "modeling")
        finally:
            sys.argv = original

    def test_upgrade_precheck_handles_delegated_and_packaged_transitions(self):
        path, installed, packages = self.upgrade_fixture()
        release = packages["modeling"][1]
        root = self.base / "modeling-cce-demo"
        (root / "deploy").mkdir(parents=True)
        shutil.copy2(path, root / "deploy/compatibility.yaml")
        record = JOINT.verified_joint_transition({"compatibilityFile": path}, packages,
                                                  "upgrade-demo", installed)
        record_path = self.base / "transition.json"
        record_path.write_text(json.dumps(record))
        backup_path = self.base / "backup.json"
        backup_path.write_text("{}")
        args = SimpleNamespace(backup_record=backup_path, transition_id="upgrade-demo",
                               transition_record=record_path, lock_holder="operation-demo")
        merged = {"global": {"targetArch": "amd64", "auth": {"issuer": "synthetic"}},
                  "modeling": {"workloads": {name: {"persistence": {"existingClaim": f"{name}-data"}}
                                             for name in UPGRADE.WORKLOADS}}}
        old_values = json.loads(json.dumps(merged))
        def precheck():
            with mock.patch.object(INSTALL, "check_delegated_lease"), \
                 mock.patch.object(INSTALL, "installed_release", return_value=installed["modeling"]), \
                 mock.patch.object(INSTALL, "installed_values", return_value=old_values), \
                 mock.patch.object(INSTALL, "bindings", return_value={"synthetic": "binding"}), \
                 mock.patch.object(INSTALL, "verified_snapshots"):
                return INSTALL.upgrade_precheck(args, root, release, merged)
        self.assertEqual(precheck(), {"synthetic": "binding"})
        args.transition_record = None
        self.assertEqual(precheck(), {"synthetic": "binding"})
        manifest = yaml.safe_load(path.read_text())
        manifest["transitions"][0]["modelingDataMigration"] = "required"
        (root / "deploy/compatibility.yaml").write_text(yaml.safe_dump(manifest))
        with self.assertRaises(ConfigError):
            precheck()

    def test_consumer_invocation_omits_unsupported_options(self):
        root = self.base / "consumer-cce-demo"
        (root / "deploy").mkdir(parents=True)
        (root / "deploy/install-cce.sh").write_text("#!/bin/sh\n")
        config = {"cluster": {"context": "synthetic"}, "stateDir": self.base, "timeoutSeconds": 30,
                  "consumer": {"namespace": "consumer-demo", "releaseName": "consumer",
                               "values": self.base / "values.yaml"}}
        package = (root, {"releaseVersion": "c2", "chartVersion": "2.0.0"})
        response = SimpleNamespace(returncode=0, stdout=json.dumps({"schemaVersion": "cce-operation/v1",
            "status": "succeeded", "releaseVersion": "c2", "chartVersion": "2.0.0"}))
        for action in ("plan", "preflight", "install", "upgrade", "status", "verify"):
            with mock.patch.object(JOINT.subprocess, "run", return_value=response) as call:
                JOINT.invoke("consumer", action, config, package, "operation-demo", self.base / "images.yaml",
                             backup_record="unused", transition_id="unused", lock_holder="operation-demo")
            command = call.call_args.args[0]
            for forbidden in ("--compatibility", "--backup-record", "--transition-id", "--transition-record"):
                self.assertNotIn(forbidden, command)
            if action == "verify":
                self.assertEqual(command[command.index("--lock-holder") + 1], "operation-demo")

    def test_second_operation_lease_failure_releases_only_first_lease(self):
        config = {"cluster": {"context": "synthetic"},
                  "consumer": {"releaseName": "consumer", "namespace": "a-demo"},
                  "modeling": {"releaseName": "modeling", "namespace": "z-demo"}}
        leases = {}
        def fake_get(lease):
            return leases.get((lease.namespace, lease.name))
        def fake_kubectl(_config, *argv, input_text=None):
            namespace = argv[argv.index("-n") + 1]
            if "create" in argv and namespace == "z-demo":
                raise JOINT.LockError("second lease is held")
            if "-f" in argv:
                document = json.loads(input_text)
                leases[(namespace, document["metadata"]["name"])] = document
            return ""
        with mock.patch.object(JOINT.Lease, "_get", fake_get), \
             mock.patch.object(JOINT, "kubectl", side_effect=fake_kubectl), \
             mock.patch.object(JOINT.threading, "Thread"):
            first = JOINT.Lease(config, "consumer", "operation-demo")
            first.acquire()
            second = JOINT.Lease(config, "modeling", "operation-demo")
            with self.assertRaises(JOINT.LockError):
                second.acquire()
            first.release()
        self.assertEqual(leases[("a-demo", "consumer-cce-operation")]["spec"]["holderIdentity"], "")
        self.assertNotIn(("z-demo", "modeling-cce-operation"), leases)

    def test_archive_rejects_symlink(self):
        archive = self.base / "bad.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            info = tarfile.TarInfo("modeling-cce-demo-amd64/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/tmp/elsewhere"
            output.addfile(info)
        with self.assertRaises(ConfigError):
            safe_extract(archive, self.base, "modeling")

    def test_external_jiuwenbox_digest_is_inherited_once(self):
        consumer_map = self.base / "consumer-images.yaml"
        modeling_map = self.base / "modeling-images.yaml"
        digest = "registry.example.com/jiuwenbox@sha256:" + "c" * 64
        consumer_map.write_text(yaml.safe_dump({"global": {"resolvedImages": {"jiuwenbox": digest}}}))
        modeling_map.write_text(yaml.safe_dump({"global": {"resolvedImages": {
            "modeling": "registry.example.com/modeling@sha256:" + "a" * 64,
            "modelingAgent": "registry.example.com/agent@sha256:" + "a" * 64,
            "ontoModeling": "registry.example.com/onto@sha256:" + "a" * 64,
        }}}))
        config = {"consumer": {"resolvedImages": consumer_map}, "modeling": {"resolvedImages": modeling_map}}
        result = JOINT.effective_images(config, self.base, "consumer", "operation-demo")
        self.assertEqual(yaml.safe_load(result.read_text())["global"]["resolvedImages"]["jiuwenbox"], digest)
        self.assertNotIn("jiuwenbox", yaml.safe_load(modeling_map.read_text())["global"]["resolvedImages"])
        modeling_map.write_text(yaml.safe_dump({"global": {"resolvedImages": {"jiuwenbox": "registry.example.com/other@sha256:" + "d" * 64}}}))
        with self.assertRaises(ConfigError):
            JOINT.effective_images(config, self.base, "consumer", "operation-demo")

    def test_upgrade_rejected_before_cluster_access(self):
        args = ["install-cce.py", "upgrade", "--context", "demo", "--namespace", "modeling-demo",
                "--release", "modeling"]
        old = sys.argv
        try:
            sys.argv = args
            with self.assertRaises(SystemExit) as raised:
                INSTALL.main()
        finally:
            sys.argv = old
        self.assertEqual(raised.exception.code, 2)

    def test_upgrade_cannot_be_enabled_by_backup_record_alone(self):
        backup_record = self.base / "backup-record.json"
        backup_record.write_text('{"status":"synthetic"}')
        old = sys.argv
        try:
            sys.argv = ["install-cce.py", "upgrade", "--context", "demo",
                        "--namespace", "modeling-demo", "--release", "modeling",
                        "--backup-record", str(backup_record)]
            with self.assertRaises(SystemExit) as raised:
                INSTALL.main()
        finally:
            sys.argv = old
        self.assertEqual(raised.exception.code, 2)

    def test_joint_upgrade_requires_both_backups_and_transition(self):
        self.assertEqual(JOINT.JOINT_SIDE_ORDER, ("consumer", "modeling"))
        old = sys.argv
        try:
            sys.argv = ["joint-cce.py", "upgrade", "--config", str(self.base / "joint.yaml"), "--scope", "both"]
            with self.assertRaises(SystemExit) as raised:
                JOINT.main()
        finally:
            sys.argv = old
        self.assertEqual(raised.exception.code, 2)

    def test_offline_package_layout_excludes_consumer_image(self):
        output = self.base / "release.tar.gz"

        def fake_run(*command, capture=False):
            if command[:3] == ("docker", "image", "inspect"):
                return "amd64" if command[-1] == "{{.Architecture}}" else "sha256:" + "b" * 64
            if command[:2] == ("docker", "save"):
                Path(command[command.index("--output") + 1]).write_bytes(b"synthetic image archive")
            if command[:2] == ("helm", "package"):
                chart = Path(command[2])
                destination = Path(command[command.index("--destination") + 1])
                with tarfile.open(destination / "modeling-0.1.0.tgz", "w:gz") as stream:
                    stream.add(chart, arcname="modeling")
            return None

        old = sys.argv
        try:
            sys.argv = ["package-cce.py", "--version", "demo", "--arch", "amd64", "--output", str(output)]
            for image_id in ("modeling", "modelingAgent", "ontoModeling"):
                sys.argv.extend(("--image", f"{image_id}=registry.example.com/{image_id}@sha256:" + "a" * 64))
            from contextlib import redirect_stdout
            from io import StringIO
            with mock.patch.object(PACKAGE, "run", side_effect=fake_run), \
                 mock.patch.object(PACKAGE, "resolve_helm", return_value=("helm", "helm-tgz")), \
                 redirect_stdout(StringIO()):
                PACKAGE.main()
        finally:
            sys.argv = old
        with tarfile.open(output, "r:gz") as stream:
            names = stream.getnames()
            self.assertIn("modeling-cce-demo-amd64/deploy/chart/Chart.yaml", names)
            self.assertIn("modeling-cce-demo-amd64/deploy/upgrade.py", names)
            release = yaml.safe_load(stream.extractfile("modeling-cce-demo-amd64/release.yaml"))
        self.assertEqual(set(release["images"]), {"modeling", "modelingAgent", "ontoModeling"})
        self.assertEqual(release["imageDependencies"]["jiuwenbox"]["sourceSide"], "consumer")
        unpacked = self.base / "unpacked"
        unpacked.mkdir()
        self.assertEqual(validate_release(safe_extract(output, unpacked, "modeling"), "modeling")["releaseVersion"], "demo")

    def test_local_package_does_not_pull_and_records_unknown_source_digest(self):
        output = self.base / "local-release.tar.gz"
        calls = []

        def fake_run(*command, capture=False):
            calls.append(command)
            if command[:3] == ("docker", "image", "inspect"):
                return "amd64" if command[-1] == "{{.Architecture}}" else "sha256:" + "b" * 64
            if command[:2] == ("docker", "save"):
                Path(command[command.index("--output") + 1]).write_bytes(b"synthetic local image archive")
            if command[:2] == ("helm", "package"):
                chart = Path(command[2])
                destination = Path(command[command.index("--destination") + 1])
                with tarfile.open(destination / "modeling-0.1.0.tgz", "w:gz") as stream:
                    stream.add(chart, arcname="modeling")

        old = sys.argv
        try:
            sys.argv = ["package-cce.py", "--version", "demo", "--arch", "amd64",
                        "--local", "--output", str(output)]
            from contextlib import redirect_stdout
            from io import StringIO
            with mock.patch.object(PACKAGE, "run", side_effect=fake_run), \
                 mock.patch.object(PACKAGE, "resolve_helm", return_value=("helm", "helm-tgz")), \
                 redirect_stdout(StringIO()):
                PACKAGE.main()
        finally:
            sys.argv = old
        self.assertFalse(any(command[:2] == ("docker", "pull") for command in calls))
        self.assertFalse(any(command[:2] == ("docker", "tag") for command in calls))
        self.assertEqual(len([command for command in calls if command[:2] == ("docker", "save")]), 3)
        unpacked = self.base / "unpacked-local"
        unpacked.mkdir()
        release = validate_release(safe_extract(output, unpacked, "modeling"), "modeling")
        self.assertTrue(all(record["sourceManifestDigest"] is None for record in release["images"].values()))
        self.assertEqual({record["archiveRef"] for record in release["images"].values()}, {
            "modeling-offline/modeling:demo", "modeling-offline/modeling-agent:demo",
            "modeling-offline/onto-modeling:demo",
        })

    def test_package_copies_chart_source_when_helm_is_unavailable(self):
        output = self.base / "no-helm.tar.gz"

        def fake_run(*command, capture=False):
            if command[:3] == ("docker", "image", "inspect"):
                return "amd64" if command[-1] == "{{.Architecture}}" else "sha256:" + "b" * 64
            if command[:2] == ("docker", "save"):
                Path(command[command.index("--output") + 1]).write_bytes(b"synthetic image archive")
            return None

        old = sys.argv
        try:
            sys.argv = ["package-cce.py", "--version", "demo", "--arch", "amd64", "--local", "--output", str(output)]
            with mock.patch.object(PACKAGE, "run", side_effect=fake_run), \
                 mock.patch.object(PACKAGE, "resolve_helm", return_value=(None, "chart-source")), \
                 redirect_stdout(StringIO()):
                PACKAGE.main()
        finally:
            sys.argv = old
        with tarfile.open(output, "r:gz") as stream:
            names = set(stream.getnames())
            self.assertIn("modeling-cce-demo-amd64/deploy/chart/Chart.yaml", names)
            release = yaml.safe_load(stream.extractfile("modeling-cce-demo-amd64/release.yaml"))
        self.assertEqual(release["chartDelivery"], "chart-source")

    def test_missing_helm_download_failure_uses_chart_source(self):
        with mock.patch.object(PACKAGE.shutil, "which", return_value=None), \
             mock.patch.object(PACKAGE.platform, "machine", return_value="x86_64"), \
             mock.patch.object(PACKAGE.urllib.request, "urlopen", side_effect=OSError("offline")), \
             mock.patch.dict(os.environ, {"CCE_HELM_VERSION": "v3.19.0"}), \
             mock.patch("sys.stderr", new_callable=StringIO):
            self.assertEqual(PACKAGE.resolve_helm(self.base), (None, "chart-source"))

    def test_missing_helm_downloads_temporary_working_binary(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            payload = b"#!/bin/sh\nprintf 'v3.19.0\\n'\n"
            item = tarfile.TarInfo("linux-amd64/helm")
            item.mode = 0o755
            item.size = len(payload)
            bundle.addfile(item, io.BytesIO(payload))
        with mock.patch.object(PACKAGE.shutil, "which", return_value=None), \
             mock.patch.object(PACKAGE.platform, "machine", return_value="x86_64"), \
             mock.patch.object(PACKAGE.urllib.request, "urlopen", return_value=io.BytesIO(archive.getvalue())), \
             mock.patch.dict(os.environ, {"CCE_HELM_VERSION": "v3.19.0"}):
            helm, delivery = PACKAGE.resolve_helm(self.base)
        self.assertEqual(delivery, "helm-tgz")
        self.assertEqual(subprocess.check_output((helm, "version", "--short"), text=True).strip(), "v3.19.0")

    def test_downloaded_helm_that_cannot_run_uses_chart_source(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            payload = b"not an executable Helm binary"
            item = tarfile.TarInfo("linux-amd64/helm")
            item.size = len(payload)
            bundle.addfile(item, io.BytesIO(payload))
        with mock.patch.object(PACKAGE.shutil, "which", return_value=None), \
             mock.patch.object(PACKAGE.platform, "machine", return_value="x86_64"), \
             mock.patch.object(PACKAGE.urllib.request, "urlopen", return_value=io.BytesIO(archive.getvalue())), \
             mock.patch.dict(os.environ, {"CCE_HELM_VERSION": "v3.19.0"}), \
             mock.patch("sys.stderr", new_callable=StringIO):
            self.assertEqual(PACKAGE.resolve_helm(self.base), (None, "chart-source"))

    def test_build_script_loads_locally_or_pushes_explicit_registry(self):
        root = self.base / "build-root"
        script_dir = root / "modeling/deploy/cce"
        script_dir.mkdir(parents=True)
        shutil.copy2(ROOT / "build-images.sh", script_dir / "build-images.sh")
        (root / "modeling/onto-modeling/vendor/wheels").mkdir(parents=True)
        requirements = root / "modeling/onto-modeling/backend/requirements.txt"
        requirements.parent.mkdir(parents=True)
        requirements.write_text("synthetic-package==1.0\n")
        (root / "modeling/onto-modeling/vendor/wheels/.prepared-amd64").write_text(checksum(requirements) + "\n")
        drawio = root / "modeling/onto-modeling/vendor/drawio/webapp"
        drawio.mkdir(parents=True)
        (drawio / "index.html").write_text("synthetic fixture")
        binary = self.base / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS"\n')
        docker.chmod(0o755)
        calls = self.base / "docker-calls"
        environment = {**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "DOCKER_CALLS": str(calls)}
        subprocess.run(["bash", str(script_dir / "build-images.sh"), "demo", "amd64"],
                       env=environment, check=True, capture_output=True, text=True)
        local_calls = calls.read_text().splitlines()
        self.assertEqual(sum("--load" in line for line in local_calls), 3)
        self.assertFalse(any("--push" in line or "imagetools" in line for line in local_calls))
        self.assertTrue(any("modeling-offline/onto-modeling:demo" in line for line in local_calls))
        calls.unlink()
        subprocess.run(["bash", str(script_dir / "build-images.sh"), "demo", "amd64", "registry.example.com/demo"],
                       env=environment, check=True, capture_output=True, text=True)
        registry_calls = calls.read_text().splitlines()
        self.assertEqual(sum("--push" in line for line in registry_calls), 3)
        self.assertEqual(sum("imagetools inspect" in line for line in registry_calls), 3)
        self.assertFalse(any("--load" in line for line in registry_calls))

    def test_build_script_prepares_missing_dependencies_then_reuses_cache(self):
        root = self.base / "build-root"
        script_dir = root / "modeling/deploy/cce"
        script_dir.mkdir(parents=True)
        shutil.copy2(ROOT / "build-images.sh", script_dir / "build-images.sh")
        requirements = root / "modeling/onto-modeling/backend/requirements.txt"
        requirements.parent.mkdir(parents=True)
        requirements.write_text("synthetic-package==1.0\n")
        binary = self.base / "bin"
        binary.mkdir()
        calls = self.base / "dependency-calls"
        for name, body in {
            "pip3": "printf 'pip:%s\\n' \"$*\" >> \"$DEPENDENCY_CALLS\"\n",
            "npm": "printf 'npm:%s\\n' \"$*\" >> \"$DEPENDENCY_CALLS\"\n"
                   "dest=''\nwhile [ \"$#\" -gt 0 ]; do\n"
                   "  if [ \"$1\" = --pack-destination ]; then dest=$2; break; fi\nshift\ndone\n"
                   "mkdir -p \"$dest\"\n"
                   "tar -czf \"$dest/yank-note-extension-drawio-2.7.0.tgz\" -C \"$FIXTURE_ROOT\" package\n",
            "docker": "printf 'docker:%s\\n' \"$*\" >> \"$DEPENDENCY_CALLS\"\n",
        }.items():
            executable = binary / name
            executable.write_text("#!/bin/sh\nset -e\n" + body)
            executable.chmod(0o755)
        fixture = self.base / "fixture/package/drawio/src/main/webapp"
        fixture.mkdir(parents=True)
        (fixture / "index.html").write_text("synthetic drawio fixture")
        environment = {**os.environ, "PATH": f"{binary}:{os.environ['PATH']}",
                       "DEPENDENCY_CALLS": str(calls), "FIXTURE_ROOT": str(self.base / "fixture")}
        command = ["bash", str(script_dir / "build-images.sh"), "demo", "amd64"]
        first = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_calls = calls.read_text().splitlines()
        self.assertEqual(sum(line.startswith("pip:") for line in first_calls), 1)
        self.assertEqual(sum(line.startswith("npm:") for line in first_calls), 1)
        self.assertEqual(sum(line.startswith("docker:buildx build") for line in first_calls), 3)
        self.assertEqual((root / "modeling/onto-modeling/vendor/wheels/.prepared-amd64").read_text().strip(), checksum(requirements))
        calls.unlink()
        second = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        second_calls = calls.read_text().splitlines()
        self.assertFalse(any(line.startswith("pip:") or line.startswith("npm:") for line in second_calls))
        self.assertEqual(sum(line.startswith("docker:buildx build") for line in second_calls), 3)
        requirements.write_text("synthetic-package==2.0\n")
        calls.unlink()
        third = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(sum(line.startswith("pip:") for line in calls.read_text().splitlines()), 1)

    def test_build_script_warns_and_continues_without_host_npm(self):
        root = self.base / "build-root"
        script_dir = root / "modeling/deploy/cce"
        script_dir.mkdir(parents=True)
        shutil.copy2(ROOT / "build-images.sh", script_dir / "build-images.sh")
        requirements = root / "modeling/onto-modeling/backend/requirements.txt"
        requirements.parent.mkdir(parents=True)
        requirements.write_text("synthetic-package==1.0\n")
        wheels = root / "modeling/onto-modeling/vendor/wheels"
        wheels.mkdir(parents=True)
        (wheels / ".prepared-amd64").write_text(checksum(requirements) + "\n")
        binary = self.base / "bin"
        binary.mkdir()
        for name in ("bash", "dirname", "mkdir", "sha256sum", "cut", "cat"):
            (binary / name).symlink_to(shutil.which(name))
        docker = binary / "docker"
        docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOCKER_CALLS\"\n")
        docker.chmod(0o755)
        calls = self.base / "docker-calls"
        environment = {**os.environ, "PATH": str(binary), "DOCKER_CALLS": str(calls)}
        result = subprocess.run(["bash", str(script_dir / "build-images.sh"), "demo", "amd64"],
                                env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host npm unavailable", result.stderr)
        self.assertFalse((root / "modeling/onto-modeling/vendor/drawio/webapp/index.html").exists())
        docker_calls = calls.read_text().splitlines()
        self.assertFalse(any(line.startswith("run") for line in docker_calls))
        self.assertEqual(sum(line.startswith("buildx build") for line in docker_calls), 3)
        calls.unlink()
        second = subprocess.run(["bash", str(script_dir / "build-images.sh"), "demo", "amd64"],
                                env=environment, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("host npm unavailable", second.stderr)
        self.assertEqual(sum(line.startswith("buildx build") for line in calls.read_text().splitlines()), 3)

        for name in ("mktemp", "rm"):
            (binary / name).symlink_to(shutil.which(name))
        npm = binary / "npm"
        npm.write_text("#!/bin/sh\nexit 9\n")
        npm.chmod(0o755)
        calls.unlink()
        failed_pack = subprocess.run(["bash", str(script_dir / "build-images.sh"), "demo", "amd64"],
                                     env=environment, capture_output=True, text=True)
        self.assertEqual(failed_pack.returncode, 0, failed_pack.stderr)
        self.assertIn("drawio preparation failed", failed_pack.stderr)
        self.assertEqual(sum(line.startswith("buildx build") for line in calls.read_text().splitlines()), 3)

    def test_build_offline_orchestrates_steps_and_default_output(self):
        script_dir = self.base / "modeling/deploy/cce"
        script_dir.mkdir(parents=True)
        wrapper = script_dir / "build-offline.sh"
        shutil.copy2(ROOT / "build-offline.sh", wrapper)
        calls = self.base / "steps.log"
        (script_dir / "build-images.sh").write_text(
            f"#!/bin/sh\nprintf 'build:%s\\n' \"$*\" >> {calls}\n"
        )
        (script_dir / "package-cce.py").write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            f"pathlib.Path({str(calls)!r}).open('a').write('package:' + ' '.join(sys.argv[1:]) + '\\n')\n"
        )
        for path in (wrapper, script_dir / "build-images.sh", script_dir / "package-cce.py"):
            path.chmod(0o755)
        result = subprocess.run(["bash", str(wrapper), "demo", "amd64"], cwd=self.base,
                                check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = calls.read_text().splitlines()
        self.assertEqual(lines[0], "build:demo amd64")
        self.assertIn("--local", lines[1])
        self.assertIn("modeling/dist/modeling-cce-demo-amd64.tar.gz", lines[1])
        custom_output = self.base / "release files" / "modeling demo.tar.gz"
        result = subprocess.run(["bash", str(wrapper), "demo", "arm64", str(custom_output)],
                                cwd=self.base, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--output {custom_output}", calls.read_text().splitlines()[-1])

    def test_build_offline_stops_before_package_when_build_fails(self):
        script_dir = self.base / "modeling/deploy/cce"
        script_dir.mkdir(parents=True)
        wrapper = script_dir / "build-offline.sh"
        shutil.copy2(ROOT / "build-offline.sh", wrapper)
        calls = self.base / "failure.log"
        (script_dir / "build-images.sh").write_text(f"#!/bin/sh\nprintf build >> {calls}\nexit 7\n")
        (script_dir / "package-cce.py").write_text(f"#!/bin/sh\nprintf package >> {calls}\n")
        for path in (wrapper, script_dir / "build-images.sh", script_dir / "package-cce.py"):
            path.chmod(0o755)
        result = subprocess.run(["bash", str(wrapper), "demo", "amd64"], check=False,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.read_text(), "build")

    def test_modeling_plan_accepts_verified_synthetic_release(self):
        root = self.package()
        deploy = root / "deploy"
        shutil.copytree(ROOT / "chart", deploy / "chart")
        shutil.copy2(ROOT / "endpoints.yaml", deploy / "endpoints.yaml")
        (root / "checksums.sha256").write_text("".join(
            f"{checksum(path)}  {path.relative_to(root)}\n" for path in sorted(root.rglob("*")) if path.is_file() and path.name != "checksums.sha256"))
        values = self.base / "values.yaml"
        values.write_text((ROOT / "values.customer.example.yaml").read_text())
        image_map = self.base / "images.yaml"
        image_map.write_text((ROOT / "resolved-images.example.yaml").read_text())
        compatibility = self.base / "compatibility.yaml"
        compatibility.write_text(yaml.safe_dump({"schemaVersion": "cce-compatibility/v1", "combinations": [{
            "id": "synthetic-demo", "consumerReleaseVersion": "demo", "consumerChartVersion": "0.1.0",
            "modelingReleaseVersion": "demo", "modelingChartVersion": "0.1.0", "platform": "linux-amd64",
            "authApiVersion": "v1", "consumerApiVersion": "v1",
            "operationSchemaVersion": "cce-operation/v1", "endpointSchemaVersion": "cce-endpoints/v1",
        }]}))
        output_dir = self.base / "state"
        old = sys.argv
        try:
            sys.argv = ["install-cce.py", "plan", "--context", "synthetic", "--namespace", "modeling-demo",
                "--release", "modeling", "--package-root", str(root), "--values", str(values),
                "--resolved-images", str(image_map),
                "--state-dir", str(output_dir)]
            from contextlib import redirect_stdout
            from io import StringIO
            output = StringIO()
            with mock.patch.object(INSTALL, "run", return_value="apiVersion: v1\nkind: ConfigMap\n"), redirect_stdout(output):
                code = INSTALL.main()
        finally:
            sys.argv = old
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["endpoints"]), 4)
        self.assertEqual(Path(result["artifacts"][0]).read_text(), "apiVersion: v1\nkind: ConfigMap\n")


if __name__ == "__main__":
    unittest.main()
