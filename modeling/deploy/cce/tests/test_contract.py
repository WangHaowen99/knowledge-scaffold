"""Synthetic CCE package and cross-release contract checks."""

import argparse
import base64
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO

import yaml


ROOT = Path(__file__).resolve().parents[1]
def load_module(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


sys.modules["common"] = load_module("common", ROOT / "common.py")
from common import (CompatibilityError, ConfigError, checksum, read_yaml, release_chart_from_list, safe_extract, validate_release,
                    validate_compatibility_manifest, verify_checksums)  # noqa: E402


def load_script(name):
    return load_module(name.replace("-", "_"), ROOT / name)


UPGRADE = load_module("upgrade", ROOT / "upgrade.py")
JOINT = load_script("joint-cce.py")
INSTALL = load_script("install-cce.py")
PACKAGE = load_script("package-cce.py")


def run_test_command(command, **options):
    """Run a synthetic fixture with a fixed shell binary and argv."""
    command = list(command)
    if command[0] == "bash":
        command[0] = "/bin/bash"
    if not Path(command[0]).is_absolute():
        raise AssertionError("test executable must have an absolute path")
    return sys.modules["common"].run_program(command, **options)


class CceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cce-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def package(self):
        root = self.base / "modeling-cce-demo-amd64"
        (root / "images").mkdir(parents=True)
        records = {}
        for image_id in ("ontoModeling", "connectorGateway", "knowledgeHub", "evaluation"):
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

    def test_upgrade_transition_requires_consumer_then_modeling_and_declared_migration(self):
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
        JOINT.verified_joint_transition(config, packages, "upgrade-demo", installed)
        manifest["transitions"][0]["modelingDataMigration"] = "unknown"
        path.write_text(yaml.safe_dump(manifest))
        with self.assertRaises(ConfigError):
            JOINT.verified_joint_transition(config, packages, "upgrade-demo", installed)

    def test_same_version_candidate_is_detected_and_requires_verification(self):
        path, installed, packages = self.upgrade_fixture()
        manifest = yaml.safe_load(path.read_text())
        old = manifest["combinations"][0]
        fields = {key: value for key, value in old.items() if key != "id"}
        config = {"compatibilityFile": path, "stateDir": self.base / "state",
                  "consumer": {"package": self.base / "consumer.tar.gz", "values": self.base / "consumer.yaml"},
                  "modeling": {"package": self.base / "modeling.tar.gz", "values": self.base / "modeling.yaml"}}
        for side in JOINT.JOINT_SIDE_ORDER:
            config.get(side, {}).get("values").write_text("global: {}\n")
        with mock.patch.object(JOINT, "safe_extract", return_value=self.base), \
             mock.patch.object(JOINT, "validate_release", return_value={}), \
             mock.patch.object(JOINT, "candidate_combination", return_value=fields), \
             mock.patch.object(JOINT, "validate_cross_side"), \
             mock.patch.object(JOINT, "installed_modeling_workloads", return_value=UPGRADE.WORKLOADS), \
             mock.patch.object(JOINT, "installed_identity", side_effect=lambda _config, side: installed.get(side)):
            original = path.read_text()
            preview = JOINT.prepare_compatibility(config, upgrade=True, confirmed=False,
                                                  no_migration_confirmed=False)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(preview["oldCombination"]["id"], old["id"])
            self.assertTrue(preview["sameVersion"])
            with self.assertRaisesRegex(ConfigError, "no migration"):
                JOINT.prepare_compatibility(config, upgrade=True, confirmed=True,
                                            no_migration_confirmed=False)
            written = JOINT.prepare_compatibility(config, upgrade=True, confirmed=True,
                                                   no_migration_confirmed=True)
            repeated = JOINT.prepare_compatibility(config, upgrade=True, confirmed=True,
                                                    no_migration_confirmed=True)
            with mock.patch.object(JOINT, "installed_modeling_workloads", return_value=UPGRADE.LEGACY_WORKLOADS):
                legacy_preview = JOINT.prepare_compatibility(config, upgrade=True, confirmed=False,
                                                              no_migration_confirmed=False)
                self.assertEqual(legacy_preview["modelingDataMigration"], "required")
                JOINT.prepare_compatibility(config, upgrade=True, confirmed=True,
                                            no_migration_confirmed=False)
        self.assertEqual(written["transitionId"], preview["transitionId"])
        self.assertEqual(repeated["transitionId"], written["transitionId"])
        transitions = yaml.safe_load(path.read_text())["transitions"]
        self.assertEqual(len(transitions), 2)
        same_transition = transitions[-1]
        self.assertEqual(same_transition["modelingDataMigration"], "required")
        self.assertEqual((same_transition["from"], same_transition["intermediateCombinationId"],
                          same_transition["to"]), (old["id"],) * 3)
        packages["consumer"][1].update(releaseVersion="c1", chartVersion="1.0.0")
        packages["modeling"][1].update(releaseVersion="m1", chartVersion="1.0.0")
        self.assertEqual(JOINT.unique_target_transition(config, old, installed), written["transitionId"])
        record = JOINT.verified_joint_transition(config, packages, written["transitionId"], installed)
        self.assertEqual(record["transition"]["id"], written["transitionId"])
        malformed = yaml.safe_load(path.read_text())
        malformed["transitions"][-1]["intermediateCombinationId"] = "middle"
        path.write_text(yaml.safe_dump(malformed))
        with self.assertRaisesRegex(ConfigError, "same intermediate combination"):
            JOINT.verified_joint_transition(config, packages, written["transitionId"], installed)

    def test_completed_release_requires_operation_and_target_images(self):
        images = self.base / "images.yaml"
        reference = "registry.example.com/modeling@sha256:" + "a" * 64
        images.write_text(yaml.safe_dump({"global": {"resolvedImages": {"modeling": reference}}}))
        config = {"cluster": {"context": "synthetic"},
                  "modeling": {"namespace": "modeling-demo", "releaseName": "modeling"}}
        package = (self.base, {"releaseVersion": "demo", "chartVersion": "1.0.0"})
        values = {"global": {"releaseVersion": "demo", "operationId": "operation-demo",
                             "resolvedImages": {"modeling": reference}}}

        def response(*command):
            if "status" in command:
                return json.dumps({"info": {"status": "deployed"}})
            return json.dumps(values)

        with mock.patch.object(JOINT, "run", side_effect=response), \
             mock.patch.object(JOINT, "release_chart", return_value="modeling-1.0.0"):
            self.assertTrue(JOINT.completed_release_matches(config, "modeling", package,
                                                              "operation-demo", images))
            self.assertFalse(JOINT.completed_release_matches(config, "modeling", package,
                                                               "another-operation", images))
            values["global"]["resolvedImages"]["modeling"] = "registry.example.com/modeling@sha256:" + "b" * 64
            self.assertFalse(JOINT.completed_release_matches(config, "modeling", package,
                                                               "operation-demo", images))

    def test_compatibility_is_derived_from_packages_and_installed_releases(self):
        consumer_root = self.base / "consumer-cce-demo"
        modeling_root = self.base / "modeling-cce-demo"
        (consumer_root / "deploy").mkdir(parents=True)
        modeling_root.mkdir()
        (consumer_root / "deploy/endpoints.yaml").write_text(yaml.safe_dump({
            "schemaVersion": "cce-endpoints/v1", "endpoints": [
                {"id": "auth", "port": 5056, "apiVersion": "v1"},
                {"id": "consumer-api", "port": 5055, "apiVersion": "v1"},
            ]}))
        consumer_values = self.base / "consumer-values.yaml"
        modeling_values = self.base / "modeling-values.yaml"
        consumer_values.write_text(yaml.safe_dump({"global": {"auth": {
            "issuer": "https://auth.example.com", "audience": "demo"}}}))
        modeling_values.write_text(yaml.safe_dump({
            "global": {"auth": {"issuer": "https://auth.example.com", "audience": "demo"}},
            "externalAuth": {"serviceName": "consumer-auth", "namespace": "consumer-demo", "servicePort": 5056},
            "externalConsumer": {"serviceName": "consumer-backend", "namespace": "consumer-demo", "servicePort": 5055},
        }))
        compatibility = self.base / "compatibility-verified.yaml"
        config = {"compatibilityFile": compatibility, "stateDir": self.base / "state",
                  "consumer": {"package": self.base / "consumer.tar.gz", "releaseName": "consumer",
                               "namespace": "consumer-demo", "values": consumer_values},
                  "modeling": {"package": self.base / "modeling.tar.gz", "releaseName": "modeling",
                               "namespace": "modeling-demo", "values": modeling_values}}
        releases = {"consumer": {"releaseVersion": "c1", "chartVersion": "1.0.0",
                                 "platform": "linux-amd64", "images": {"jiuwenbox": {}}},
                    "modeling": {"releaseVersion": "m1", "chartVersion": "1.0.0",
                                 "platform": "linux-amd64"}}

        def extracted(_archive, _destination, side):
            return consumer_root if side == "consumer" else modeling_root

        with mock.patch.object(JOINT, "safe_extract", side_effect=extracted), \
             mock.patch.object(JOINT, "validate_release", side_effect=lambda _root, side: releases.get(side)):
            preview = JOINT.prepare_compatibility(config, upgrade=False, confirmed=False,
                                                   no_migration_confirmed=False)
            self.assertEqual(preview["status"], "preview")
            self.assertFalse(compatibility.exists())
            created = JOINT.prepare_compatibility(config, upgrade=False, confirmed=True,
                                                   no_migration_confirmed=False)
            self.assertEqual(created["status"], "written")
            old_id = created["targetCombinationId"]
            releases["consumer"].update(releaseVersion="c2", chartVersion="2.0.0")
            releases["modeling"].update(releaseVersion="m2", chartVersion="2.0.0")
            installed = {"consumer": {"releaseVersion": "c1", "chartVersion": "1.0.0"},
                         "modeling": {"releaseVersion": "m1", "chartVersion": "1.0.0"}}
            original = compatibility.read_text()
            with mock.patch.object(JOINT, "installed_identity", side_effect=lambda _config, side: installed.get(side)), \
                 mock.patch.object(JOINT, "installed_modeling_workloads", return_value=UPGRADE.WORKLOADS):
                upgrade_preview = JOINT.prepare_compatibility(config, upgrade=True, confirmed=False,
                                                               no_migration_confirmed=False)
                self.assertEqual(compatibility.read_text(), original)
                with self.assertRaisesRegex(ConfigError, "no migration"):
                    JOINT.prepare_compatibility(config, upgrade=True, confirmed=True,
                                                no_migration_confirmed=False)
                self.assertEqual(compatibility.read_text(), original)
                written = JOINT.prepare_compatibility(config, upgrade=True, confirmed=True,
                                                       no_migration_confirmed=True)
        self.assertEqual(written["transitionId"], upgrade_preview["transitionId"])
        document = validate_compatibility_manifest(yaml.safe_load(compatibility.read_text()))
        transition = document["transitions"][0]
        self.assertEqual(transition["from"], old_id)
        self.assertEqual(transition["to"], written["targetCombinationId"])
        middle = next(item for item in document["combinations"]
                      if item["id"] == transition["intermediateCombinationId"])
        self.assertEqual((middle["consumerReleaseVersion"], middle["modelingReleaseVersion"]),
                         ("c2", "m1"))

    def test_missing_manifest_rebuilds_old_combination_from_installed_contract(self):
        path, installed, _ = self.upgrade_fixture()
        path.unlink()
        consumer_root = self.base / "consumer-cce-demo"
        modeling_root = self.base / "modeling-cce-demo"
        (consumer_root / "deploy").mkdir(parents=True)
        modeling_root.mkdir()
        (consumer_root / "deploy/endpoints.yaml").write_text(yaml.safe_dump({
            "schemaVersion": "cce-endpoints/v1", "endpoints": [
                {"id": "auth", "port": 5056, "apiVersion": "v2"},
                {"id": "consumer-api", "port": 5055, "apiVersion": "v2"}],
        }))
        consumer_values = self.base / "consumer-values.yaml"
        modeling_values = self.base / "modeling-values.yaml"
        consumer_values.write_text("global: {}\n")
        modeling_values.write_text("global: {}\n")
        config = {"compatibilityFile": path, "stateDir": self.base / "state",
                  "cluster": {"context": "synthetic"},
                  "consumer": {"package": self.base / "consumer.tar.gz", "releaseName": "consumer",
                               "namespace": "consumer-demo", "values": consumer_values},
                  "modeling": {"package": self.base / "modeling.tar.gz", "releaseName": "modeling",
                               "namespace": "modeling-demo", "values": modeling_values}}
        releases = {"consumer": {"releaseVersion": "c2", "chartVersion": "2.0.0",
                                 "platform": "linux-amd64", "images": {"jiuwenbox": {}}},
                    "modeling": {"releaseVersion": "m2", "chartVersion": "2.0.0",
                                 "platform": "linux-amd64"}}
        consumer_state = {"releaseName": "consumer", "namespace": "consumer-demo",
                          "releaseVersion": "c1", "chartVersion": "1.0.0", "platform": "linux-amd64",
                          "phase": "ready", "endpoints": [
                              {"id": "auth", "serviceName": "consumer-auth", "namespace": "consumer-demo",
                               "port": 5056, "apiVersion": "v1"},
                              {"id": "consumer-api", "serviceName": "consumer-backend", "namespace": "consumer-demo",
                               "port": 5055, "apiVersion": "v1"}]}

        def extracted(_archive, _destination, side):
            return consumer_root if side == "consumer" else modeling_root

        def cluster_resource(_config, *command):
            if "configmap" in command:
                return json.dumps({"data": {"state.json": json.dumps(consumer_state)}})
            return json.dumps({"spec": {"ports": [{"port": 5056 if "consumer-auth" in command else 5055}]}})

        with mock.patch.object(JOINT, "safe_extract", side_effect=extracted), \
             mock.patch.object(JOINT, "validate_release", side_effect=lambda _root, side: releases[side]), \
             mock.patch.object(JOINT, "validate_cross_side"), \
             mock.patch.object(JOINT, "installed_identity_without_manifest", side_effect=lambda _config, side: installed.get(side)), \
             mock.patch.object(JOINT, "installed_modeling_workloads", return_value=UPGRADE.WORKLOADS), \
             mock.patch.object(JOINT, "kubectl", side_effect=cluster_resource):
            preview = JOINT.prepare_compatibility(config, upgrade=True, confirmed=False,
                                                  no_migration_confirmed=False)
            self.assertFalse(path.exists())
            self.assertTrue(preview["reconstructedOldCombination"])
            self.assertEqual(preview["oldCombination"]["consumerReleaseVersion"], "c1")
            self.assertEqual(preview["oldCombination"]["authApiVersion"], "v1")
            self.assertEqual(preview["targetCombination"]["authApiVersion"], "v2")
            with self.assertRaisesRegex(ConfigError, "no migration"):
                JOINT.prepare_compatibility(config, upgrade=True, confirmed=True,
                                            no_migration_confirmed=False)
            self.assertFalse(path.exists())
            written = JOINT.prepare_compatibility(config, upgrade=True, confirmed=True,
                                                   no_migration_confirmed=True)
        document = validate_compatibility_manifest(yaml.safe_load(path.read_text()))
        self.assertEqual(written["transitionId"], preview["transitionId"])
        self.assertEqual(len(document["combinations"]), 3)
        self.assertEqual(document["transitions"][0]["from"], preview["oldCombination"]["id"])
        self.assertEqual(document["transitions"][0]["to"], preview["targetCombinationId"])

    def test_missing_manifest_same_version_still_checks_installed_contract(self):
        path, installed, _ = self.upgrade_fixture()
        path.unlink()
        config = {"compatibilityFile": path, "stateDir": self.base / "state",
                  "consumer": {"package": self.base / "consumer.tar.gz", "values": self.base / "consumer.yaml"},
                  "modeling": {"package": self.base / "modeling.tar.gz", "values": self.base / "modeling.yaml"}}
        config["consumer"]["values"].write_text("global: {}\n")
        config["modeling"]["values"].write_text("global: {}\n")
        fields = {"consumerReleaseVersion": "c1", "consumerChartVersion": "1.0.0",
                  "modelingReleaseVersion": "m1", "modelingChartVersion": "1.0.0",
                  "platform": "linux-amd64", "operationSchemaVersion": "cce-operation/v1",
                  "endpointSchemaVersion": "cce-endpoints/v1", "authApiVersion": "v1",
                  "consumerApiVersion": "v1"}
        with mock.patch.object(JOINT, "safe_extract", return_value=self.base), \
             mock.patch.object(JOINT, "validate_release", return_value={}), \
             mock.patch.object(JOINT, "candidate_combination", return_value=fields), \
             mock.patch.object(JOINT, "validate_cross_side"), \
             mock.patch.object(JOINT, "installed_identity_without_manifest",
                               side_effect=lambda _config, side: installed.get(side)), \
             mock.patch.object(JOINT, "installed_consumer_contract", side_effect=CompatibilityError(
                 "old contract unavailable")) as contract:
            with self.assertRaisesRegex(CompatibilityError, "old contract unavailable"):
                JOINT.prepare_compatibility(config, upgrade=True, confirmed=False,
                                            no_migration_confirmed=False)
            contract.assert_called_once()
            self.assertFalse(path.exists())

    def test_rebuilding_old_contract_rejects_state_mismatch(self):
        config = {"cluster": {"context": "synthetic"}, "consumer": {"namespace": "consumer-demo",
                  "releaseName": "consumer"}}
        installed = {"consumer": {"releaseVersion": "c1", "chartVersion": "1.0.0"},
                     "modeling": {"releaseVersion": "m1", "chartVersion": "1.0.0"}}
        target = {"platform": "linux-amd64"}
        state = {"releaseName": "consumer", "namespace": "consumer-demo", "releaseVersion": "wrong",
                 "chartVersion": "1.0.0", "platform": "linux-amd64", "phase": "ready", "endpoints": []}
        with mock.patch.object(JOINT, "kubectl", return_value=json.dumps({"data": {"state.json": json.dumps(state)}})):
            with self.assertRaisesRegex(ConfigError, "differs from Helm"):
                JOINT.installed_consumer_contract(config, installed, target)

    def test_rebuilding_external_auth_contract_uses_current_owner_uid_and_deployment_owner(self):
        config = {"cluster": {"context": "synthetic"}, "consumer": {"namespace": "consumer-demo",
                  "releaseName": "consumer"}}
        installed = {"consumer": {"releaseVersion": "c2", "chartVersion": "2.0.0"},
                     "modeling": {"releaseVersion": "m1", "chartVersion": "1.0.0"}}
        auth_ref = {"apiVersion": "auth-v1", "serviceName": "auth-service",
                    "namespace": "consumer-demo", "servicePort": 5056}
        identity = {"issuer": "https://auth.example.com", "audience": "synthetic"}
        state = {"releaseName": "consumer", "namespace": "consumer-demo", "releaseVersion": "c2",
                 "chartVersion": "2.0.0", "platform": "linux-amd64", "phase": "ready",
                 "externalAuth": auth_ref, "identity": {"auth": identity},
                 "authOwnership": {"step": "completed", "serviceUID": "auth-uid",
                                   "targetRelease": "independent-auth"},
                 "endpoints": [
                     {"id": "auth", "serviceName": "auth-service", "namespace": "consumer-demo",
                      "port": 5056, "apiVersion": "auth-v1"},
                     {"id": "consumer-api", "serviceName": "consumer-backend", "namespace": "consumer-demo",
                      "port": 5055, "apiVersion": "v1"}]}
        auth_state = {"phase": "ready", "identity": identity, "serviceUID": "auth-uid"}
        owner = "independent-auth"
        service_uid = "auth-uid"

        def resource(_config, *command):
            kind = command[command.index("get") + 1]
            name = command[command.index("get") + 2]
            if kind == "configmap":
                body = auth_state if name == "independent-auth-cce-state" else state
                return json.dumps({"data": {"state.json": json.dumps(body)}})
            if kind == "service":
                metadata = {"uid": service_uid, "annotations": {"meta.helm.sh/release-name": owner,
                            "meta.helm.sh/release-namespace": "consumer-demo"}} if name == "auth-service" else {}
                return json.dumps({"metadata": metadata, "spec": {"ports": [{"port": 5056 if name == "auth-service" else 5055}]}})
            if kind == "deployment":
                return json.dumps({"metadata": {"annotations": {"meta.helm.sh/release-name": owner}}})
            raise AssertionError(command)

        with mock.patch.object(JOINT, "kubectl", side_effect=resource), \
             mock.patch.object(JOINT, "run", return_value=json.dumps({"info": {"status": "deployed"}})):
            contract = JOINT.installed_consumer_contract(config, installed, {"platform": "linux-amd64"})
            self.assertEqual(contract["authApiVersion"], "auth-v1")
            service_uid = "replacement-uid"
            with self.assertRaisesRegex(CompatibilityError, "owner identity"):
                JOINT.installed_consumer_contract(config, installed, {"platform": "linux-amd64"})
            auth_state["serviceUID"] = "replacement-uid"
            self.assertEqual(JOINT.installed_consumer_contract(config, installed,
                             {"platform": "linux-amd64"})["authApiVersion"], "auth-v1")
            owner = "wrong-owner"
            with self.assertRaisesRegex(CompatibilityError, "Service identity"):
                JOINT.installed_consumer_contract(config, installed, {"platform": "linux-amd64"})

    def test_target_only_manifest_uses_cluster_old_contract(self):
        config = {"compatibilityFile": self.base / "compatibility.yaml"}
        installed = {"consumer": {"releaseVersion": "c1", "chartVersion": "1.0.0"},
                     "modeling": {"releaseVersion": "m1", "chartVersion": "1.0.0"}}
        with mock.patch.object(JOINT, "installed_identity", side_effect=CompatibilityError(
                "installed consumer Chart version is absent in compatibility manifest")), \
             mock.patch.object(JOINT, "installed_identity_without_manifest",
                               side_effect=lambda _config, side: installed.get(side)):
            self.assertEqual(JOINT._installed_for_compatibility(config, True), installed)
        with mock.patch.object(JOINT, "installed_identity", side_effect=CompatibilityError(
                "installed modeling Chart version is absent in compatibility manifest")), \
             mock.patch.object(JOINT, "installed_identity_without_manifest",
                               side_effect=lambda _config, side: installed.get(side)):
            self.assertEqual(JOINT._installed_for_compatibility(config, True), installed)
        with mock.patch.object(JOINT, "installed_identity", side_effect=CompatibilityError(
                "installed modeling Chart version is ambiguous in compatibility manifest")), \
             mock.patch.object(JOINT, "installed_identity_without_manifest") as fallback:
            with self.assertRaisesRegex(CompatibilityError, "ambiguous"):
                JOINT._installed_for_compatibility(config, True)
            fallback.assert_not_called()
        with mock.patch.object(JOINT, "installed_identity", side_effect=CompatibilityError(
                "another compatibility failure")), \
             mock.patch.object(JOINT, "installed_identity_without_manifest") as fallback:
            with self.assertRaisesRegex(CompatibilityError, "another compatibility failure"):
                JOINT._installed_for_compatibility(config, True)
            fallback.assert_not_called()

    def test_old_contract_rejects_actual_service_port_mismatch(self):
        config = {"cluster": {"context": "synthetic"}, "consumer": {"namespace": "consumer-demo",
                  "releaseName": "consumer"}}
        installed = {"consumer": {"releaseVersion": "c1", "chartVersion": "1.0.0"},
                     "modeling": {"releaseVersion": "m1", "chartVersion": "1.0.0"}}
        state = {"releaseName": "consumer", "namespace": "consumer-demo", "releaseVersion": "c1",
                 "chartVersion": "1.0.0", "platform": "linux-amd64", "phase": "ready", "endpoints": [
                     {"id": "auth", "serviceName": "consumer-auth", "namespace": "consumer-demo",
                      "port": 5056, "apiVersion": "v1"},
                     {"id": "consumer-api", "serviceName": "consumer-backend", "namespace": "consumer-demo",
                      "port": 5055, "apiVersion": "v1"}]}
        def resource(_config, *command):
            if "configmap" in command:
                return json.dumps({"data": {"state.json": json.dumps(state)}})
            return json.dumps({"spec": {"ports": [{"port": 9999}]}})
        with mock.patch.object(JOINT, "kubectl", side_effect=resource):
            with self.assertRaisesRegex(CompatibilityError, "Service port differs"):
                JOINT.installed_consumer_contract(config, installed, {"platform": "linux-amd64"})

    def test_installed_consumer_identity_uses_helm_metadata_without_values(self):
        path, installed, _ = self.upgrade_fixture()
        config = {"compatibilityFile": path, "cluster": {"context": "synthetic"},
                  "consumer": {"namespace": "consumer-demo", "releaseName": "consumer"}}
        def response(*command):
            if "status" in command:
                return json.dumps({"name": "consumer", "info": {"status": "deployed"}})
            self.assertIn("metadata", command)
            return json.dumps({"name": "consumer", "namespace": "consumer-demo",
                               "appVersion": "c1", "version": "1.0.0"})
        with mock.patch.object(JOINT, "run", side_effect=response) as run:
            self.assertEqual(JOINT.installed_identity(config, "consumer"), installed["consumer"])
        self.assertEqual(len(run.call_args_list), 2)
        self.assertNotIn("values", str(run.call_args_list))

    def test_installed_consumer_identity_rejects_invalid_metadata_and_unknown_chart(self):
        path, _, _ = self.upgrade_fixture()
        config = {"compatibilityFile": path, "cluster": {"context": "synthetic"},
                  "consumer": {"namespace": "consumer-demo", "releaseName": "consumer"}}
        status = {"info": {"status": "deployed"}}
        metadata = {"name": "consumer", "namespace": "consumer-demo",
                    "appVersion": "c1", "version": "1.0.0"}

        def response(*command):
            return json.dumps(status if "status" in command else metadata)

        with mock.patch.object(JOINT, "run", side_effect=response):
            metadata["namespace"] = "other-demo"
            with self.assertRaisesRegex(ConfigError, "metadata is invalid"):
                JOINT.installed_identity(config, "consumer")
            metadata["namespace"] = "consumer-demo"
            metadata["version"] = "9.0.0"
            with self.assertRaisesRegex(ConfigError, "absent in compatibility manifest"):
                JOINT.installed_identity(config, "consumer")
            status["info"]["status"] = "failed"
            with self.assertRaisesRegex(ConfigError, "not deployed"):
                JOINT.installed_identity(config, "consumer")

    def test_completed_consumer_release_matches_helm_metadata_without_values(self):
        config = {"cluster": {"context": "synthetic"},
                  "consumer": {"namespace": "consumer-demo", "releaseName": "consumer"}}
        package = (self.base, {"releaseVersion": "c1", "chartVersion": "1.0.0"})
        metadata = {"name": "consumer", "namespace": "consumer-demo",
                    "appVersion": "c1", "version": "1.0.0"}

        def response(*command):
            if "status" in command:
                return json.dumps({"info": {"status": "deployed"}})
            self.assertIn("metadata", command)
            return json.dumps(metadata)

        with mock.patch.object(JOINT, "run", side_effect=response), \
             mock.patch.object(JOINT, "release_chart", return_value="knowledge-consumer-1.0.0"):
            self.assertTrue(JOINT.completed_release_matches(config, "consumer", package,
                                                              "operation-demo", self.base / "unused.yaml"))
            metadata["appVersion"] = "c2"
            self.assertFalse(JOINT.completed_release_matches(config, "consumer", package,
                                                               "operation-demo", self.base / "unused.yaml"))

    def test_installed_modeling_reads_chart_from_helm_list(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        with mock.patch.object(INSTALL, "release_status", return_value={
                 "name": "modeling", "info": {"status": "deployed"}}), \
             mock.patch.object(INSTALL, "installed_values", return_value={
                 "global": {"releaseVersion": "v1.0"}}), \
             mock.patch.object(INSTALL, "run", return_value=json.dumps([
                 {"name": "modeling-old", "chart": "modeling-9.0.0"},
                 {"name": "modeling", "chart": "modeling-0.1.0"},
             ])):
            self.assertEqual(INSTALL.installed_release(args), {
                "releaseVersion": "v1.0", "chartVersion": "0.1.0"})

    def test_helm_list_requires_exact_release_name(self):
        with self.assertRaises(ConfigError):
            release_chart_from_list([{"name": "modeling-old", "chart": "modeling-0.1.0"}], "modeling")

    def test_upgrade_rejects_changed_claim_and_requires_ready_snapshots(self):
        args = SimpleNamespace(release="modeling", namespace="demo", context="demo")
        merged = {"modeling": {"workloads": {name: {"persistence": {"existingClaim": f"{name}-data"}}
                                           for name in UPGRADE.LEGACY_WORKLOADS}}}

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

        before = UPGRADE.bindings(resource, args, merged, stopped=True,
                                  workloads=UPGRADE.LEGACY_WORKLOADS)
        record = {"schemaVersion": "cce-backup/v1", "context": "demo", "namespace": "demo",
                  "releaseName": "modeling", "stoppedAt": "2026-10-15T07:59:00Z",
                  "dataEvidence": {"core": 3, "agent": 2, "onto": 1},
                  "claims": {name: {**before[name], "snapshotName": f"{name}-snapshot",
                                    "snapshotUid": f"snapshot-uid-{name}"} for name in UPGRADE.LEGACY_WORKLOADS}}
        path = self.base / "backup.json"
        path.write_text(json.dumps(record))
        UPGRADE.verified_snapshots(resource, args, path, before)
        changed = json.loads(json.dumps(merged))
        changed["modeling"]["workloads"]["onto"]["persistence"]["existingClaim"] = "other-data"
        with self.assertRaises(ConfigError):
            UPGRADE.bindings(resource, args, changed, stopped=True,
                             workloads=UPGRADE.LEGACY_WORKLOADS)
        record["claims"]["core"]["snapshotUid"] = "wrong-uid"
        path.write_text(json.dumps(record))
        with self.assertRaises(ConfigError):
            UPGRADE.verified_snapshots(resource, args, path, before)

    def test_upgrade_data_counts_must_not_decrease(self):
        backup = self.base / "backup.json"
        backup.write_text(json.dumps({"dataEvidence": {"core": 3, "agent": 2, "onto": 1,
                                                        "hub": 2, "evaluation": 4}}))
        args = SimpleNamespace(context="demo", namespace="demo", release="modeling")
        calls = []

        def command(*argv):
            workload = next(name for name in UPGRADE.WORKLOADS if f"deployment/modeling-{name}" in argv)
            calls.append(workload)
            return {"onto": "1\n", "hub": "2\n", "evaluation": "4\n"}[workload]

        UPGRADE.verify_persisted_data(command, args, backup)
        self.assertEqual(calls, list(UPGRADE.WORKLOADS))
        backup.write_text(json.dumps({"dataEvidence": {"core": 4, "agent": 2, "onto": 2,
                                                        "hub": 2, "evaluation": 4}}))
        with self.assertRaises(ConfigError):
            UPGRADE.verify_persisted_data(command, args, backup)

    def test_upgrade_keeps_pods_stopped_until_pvc_binding_is_checked(self):
        args = SimpleNamespace(context="demo", namespace="modeling-demo", release="modeling",
                               values=self.base / "values.yaml", resolved_images=self.base / "images.yaml",
                               operation_id="synthetic-op", timeout_seconds=120,
                               backup_record=self.base / "backup.json")
        calls = []
        before = {"onto": {"volumeName": "pv-onto"}}

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
        self.assertIn("--reset-values", upgrade_calls[0])
        self.assertIn("modeling.workloads.onto.replicas=0", upgrade_calls[0])
        self.assertIn("hub.replicas=0", upgrade_calls[0])
        self.assertIn("evaluation.replicas=0", upgrade_calls[0])
        self.assertIn("--wait", upgrade_calls[1])
        self.assertNotIn("--wait", upgrade_calls[0])
        self.assertEqual([call.kwargs["stopped"] for call in current_bindings.call_args_list], [True, False])
        wait_stopped.assert_called_once_with(args)
        calls.clear()
        with mock.patch.object(INSTALL, "run", side_effect=fake_run), \
             mock.patch.object(INSTALL, "wait_old_pods_gone"), \
             mock.patch.object(INSTALL, "bindings", return_value={"onto": {"volumeName": "other-pv"}}), \
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
            calls.append((side, action, kwargs))
            if side == "consumer" and action == "upgrade" and self.consumer_fails:
                raise RuntimeError("synthetic Consumer upgrade failure")
            if side == "consumer" and action == "verify" and self.consumer_verify_fails:
                raise JOINT.ChildOperationError(side, action, {
                    "schemaVersion": "cce-operation/v1", "status": "failed", "phase": "verify",
                    "errorCode": "RESOURCE_NOT_READY", "message": "synthetic Consumer verification failure",
                    "retryable": True})
            return {"schemaVersion": "cce-operation/v1", "status": "succeeded",
                    "releaseVersion": package[1]["releaseVersion"], "chartVersion": package[1]["chartVersion"]}

        original = sys.argv
        try:
            sys.argv = ["joint-cce.py", "upgrade", "--config", str(self.base / "deployment.yaml"),
                        "--scope", "both"]
            from contextlib import redirect_stdout
            from io import StringIO
            with mock.patch.object(JOINT, "load_config", return_value=config), \
                 mock.patch.object(JOINT, "safe_extract", side_effect=lambda path, *_: self.base / path.name.removesuffix(".tar.gz")), \
                 mock.patch.object(JOINT, "validate_release", side_effect=lambda _root, side: releases[side]), \
                 mock.patch.object(JOINT, "validate_combination"), \
                 mock.patch.object(JOINT, "unique_target_transition", return_value="upgrade-demo"), \
                 mock.patch.object(JOINT, "validate_cross_side"), \
                 mock.patch.object(JOINT, "read_yaml", side_effect=lambda path: {"global": {"clusterFlavor": "standard", "access": {
                     "elb": {"id": "synthetic"}, "listenerPort": 443 if "consumer" in str(path) else 8443}}}), \
                 mock.patch.object(JOINT, "effective_images", return_value=self.base / "effective.yaml"), \
                 mock.patch.object(JOINT, "operation_fingerprint", return_value="synthetic"), \
                 mock.patch.object(JOINT, "checksum", return_value="synthetic"), \
                 mock.patch.object(JOINT, "installed_identity", side_effect=lambda _config, side: installed.get(side)), \
                 mock.patch.object(JOINT, "verified_joint_transition") as verified, \
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
                normal_fingerprint = hashlib.sha256(b"syntheticupgrade-demoauto:2:15.0").hexdigest()
                self.assertEqual(json.loads((self.base / f"{operation_id}.json").read_text())["fingerprint"],
                                 normal_fingerprint)
                self.assertEqual([kwargs.get("lock_holder") for side, action, kwargs in calls if action == "verify"],
                                 [operation_id, operation_id])
                self.assertTrue(next(kwargs for side, action, kwargs in calls
                                     if side == "modeling" and action == "upgrade")["auto_backup"])
                self.assertFalse(next(kwargs for side, action, kwargs in calls
                                      if side == "modeling" and action == "preflight")["backup_record"])
                calls.clear()
                installed["consumer"].update(releaseVersion="c2", chartVersion="2.0.0")
                installed["modeling"].update(releaseVersion="m2", chartVersion="2.0.0")
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(JOINT.main(), 0, output.getvalue())
                same_operation_id = json.loads(output.getvalue())["operationId"]
                self.assertEqual(json.loads((self.base / f"{same_operation_id}.json").read_text())["fingerprint"],
                                 normal_fingerprint)
                verified.assert_called_with(config, mock.ANY, "upgrade-demo", installed)
                self.assertEqual([(side, action) for side, action, _ in calls],
                                 [("consumer", "preflight"), ("modeling", "preflight"),
                                  ("consumer", "upgrade"), ("consumer", "verify"),
                                  ("modeling", "upgrade"), ("modeling", "verify")])
                installed["consumer"].update(releaseVersion="c1", chartVersion="1.0.0")
                installed["modeling"].update(releaseVersion="m1", chartVersion="1.0.0")
                calls.clear()
                self.consumer_fails = True
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(JOINT.main(), 1)
                self.assertEqual([(side, action) for side, action, _ in calls],
                                 [("consumer", "preflight"), ("modeling", "preflight"), ("consumer", "upgrade")])
                failed = json.loads(output.getvalue())
                self.assertEqual(failed["phase"], "consumer")
                self.assertEqual(failed["errorCode"], "RESOURCE_NOT_READY")
                calls.clear()
                self.consumer_fails = False
                self.consumer_verify_fails = True
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(JOINT.main(), 1)
                self.assertEqual([(side, action) for side, action, _ in calls],
                                 [("consumer", "preflight"), ("modeling", "preflight"), ("consumer", "upgrade"),
                                  ("consumer", "verify")])
                failed = json.loads(output.getvalue())
                self.assertEqual(failed["phase"], "consumer")
                self.assertEqual(failed["failedAction"], "verify")
                self.assertEqual(failed["errorCode"], "RESOURCE_NOT_READY")
                self.assertFalse(failed["retryable"])
                self.assertEqual(failed["results"]["consumer"]["operationResult"]["status"], "succeeded")
        finally:
            sys.argv = original

    def test_package_validates_and_detects_tampering(self):
        root = self.package()
        self.assertEqual(validate_release(root, "modeling")["platform"], "linux-amd64")
        (root / "images/ontoModeling.tar").write_bytes(b"changed")
        with self.assertRaises(ConfigError):
            validate_release(root, "modeling")

    def test_cce_chart_declares_onto_sandbox_gateway_and_association(self):
        workloads = (ROOT / "chart/templates/workloads.yaml").read_text()
        services = (ROOT / "chart/templates/services.yaml").read_text()
        values = yaml.safe_load((ROOT / "values.customer.example.yaml").read_text())
        self.assertIn("-sandbox-gateway", workloads)
        self.assertIn("app.sandbox_gateway", workloads)
        self.assertIn("ONTO_WEB_SANDBOX_GATEWAY_URL", workloads)
        self.assertNotIn("ONTO_WEB_MODELING_ASSOCIATION_URL", workloads)
        self.assertNotIn("ONTO_WEB_MODELING_ASSOCIATION_TOKEN", workloads)
        self.assertNotIn("name: {{ include \"modeling.name\" . }}-core", workloads)
        self.assertIn("-sandbox-gateway", services)
        self.assertEqual(values["modeling"]["workloads"]["sandboxGateway"]["resources"]["requests"]["memory"], "256Mi")

    def test_cce_chart_declares_connector_gateway(self):
        """资料转文本与数据源采集（ADR-0073、ADR-0104）：单独镜像、非 root、只读根，Onto 经 Service 名访问。"""
        workloads = (ROOT / "chart/templates/workloads.yaml").read_text()
        services = (ROOT / "chart/templates/services.yaml").read_text()
        defaults = yaml.safe_load((ROOT / "chart/values.yaml").read_text())
        self.assertIn("-connector-gateway", workloads)
        self.assertIn(".Values.global.resolvedImages.connectorGateway", workloads)
        self.assertIn("ONTO_WEB_CONNECTOR_GATEWAY_URL", workloads)
        self.assertIn("-connector-gateway:8120", workloads)
        self.assertIn("/api/v1/connector/issuer-key", workloads)
        self.assertIn("ONTO_CONNECTOR_EGRESS_DENY", workloads)
        self.assertIn("-connector-gateway", services)
        self.assertIn("connectorGateway", defaults["global"]["resolvedImages"])
        self.assertEqual(defaults["modeling"]["workloads"]["connectorGateway"]["resources"]["limits"]["memory"], "1536Mi")

    def test_modeling_auth_admin_secret_is_optional_and_injected_for_management(self):
        defaults = yaml.safe_load((ROOT / "chart/values.yaml").read_text())
        values = yaml.safe_load((ROOT / "values.customer.example.yaml").read_text())
        schema = json.loads((ROOT / "chart/values.schema.json").read_text())
        import jsonschema
        merged = INSTALL.merge(defaults, values)
        merged["global"]["resolvedImages"] = {name: "registry.example.com/app@sha256:" + "a" * 64
            for name in ("ontoModeling", "connectorGateway", "knowledgeHub", "evaluation", "jiuwenbox")}
        jsonschema.validate(merged, schema)
        values["modeling"]["secrets"]["authAdminToken"] = {"name": "modeling-auth-client", "key": "admin-token"}
        merged = INSTALL.merge(defaults, values)
        merged["global"]["resolvedImages"] = {name: "registry.example.com/app@sha256:" + "a" * 64
            for name in ("ontoModeling", "connectorGateway", "knowledgeHub", "evaluation", "jiuwenbox")}
        jsonschema.validate(merged, schema)
        template = (ROOT / "chart/templates/workloads.yaml").read_text()
        self.assertIn("ONTO_WEB_AUTH_ADMIN_TOKEN", template)
        self.assertIn(".Values.modeling.secrets.authAdminToken.name", template)

    def test_modeling_verify_checks_sandbox_and_connector_gateways(self):
        self.assertEqual(INSTALL.GATEWAYS, (("sandbox-gateway", 8110), ("connector-gateway", 8120)))
        args = SimpleNamespace(context="example", namespace="modeling-test", release="modeling", timeout_seconds=10)
        calls = []
        with mock.patch.object(INSTALL, "run", side_effect=lambda *argv: calls.append(argv) or ""):
            INSTALL.verify(args, self.base)
        rollouts = [item[-2] for item in calls if "rollout" in item]
        self.assertIn("deployment/modeling-sandbox-gateway", rollouts)
        self.assertIn("deployment/modeling-connector-gateway", rollouts)
        health = [item[-1] for item in calls if "exec" in item and "deployment/modeling-onto" in item]
        self.assertEqual(health, ["http://modeling-sandbox-gateway:8110/health",
                                  "http://modeling-connector-gateway:8120/health"])

    def test_modeling_verify_retries_gateway_during_endpoint_recreation(self):
        args = SimpleNamespace(context="example", namespace="modeling-test", release="modeling",
                               timeout_seconds=10)
        calls = []
        clock = iter((0, 1, 2))
        attempts = 0

        def command(*argv):
            nonlocal attempts
            calls.append(argv)
            if "exec" in argv and "deployment/modeling-onto" in argv:
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("gateway endpoint not ready")
            return ""

        with mock.patch.object(INSTALL, "run", side_effect=command), \
                mock.patch.object(INSTALL.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(INSTALL.time, "sleep") as pause:
            INSTALL.verify(args, self.base)
        gateway_checks = []
        for item in calls:
            if "exec" in item and "deployment/modeling-onto" in item:
                gateway_checks.append(item)
        self.assertEqual(len(gateway_checks), 3)  # 沙箱网关重试一次，连接器网关一次
        pause.assert_called_once_with(2)

    def test_evaluation_listener_can_use_a_custom_external_port(self):
        package = self.base / "modeling-package"
        chart = package / "deploy/chart"
        chart.mkdir(parents=True)
        for name in ("values.yaml", "values.schema.json"):
            shutil.copy2(ROOT / "chart" / name, chart / name)
        values = yaml.safe_load((ROOT / "values.customer.example.yaml").read_text())
        listeners = values["global"]["access"]["listeners"]
        self.assertEqual(listeners[1]["listenerPort"], 5180)
        listeners[1]["listenerPort"] = 15180
        values["modeling"]["config"]["evalPublicUrl"] = "https://eval.example.com:15180/evaluation/"
        values_path = self.base / "modeling-values.yaml"
        values_path.write_text(yaml.safe_dump(values))
        images = {key: "registry.example.com/app@sha256:" + "a" * 64
                  for key in ("ontoModeling", "connectorGateway", "knowledgeHub", "evaluation", "jiuwenbox")}
        images_path = self.base / "modeling-images.yaml"
        images_path.write_text(yaml.safe_dump({"global": {"resolvedImages": images}}))
        release = {"platform": "linux-amd64", "images": {
            key: {} for key in ("ontoModeling", "connectorGateway", "knowledgeHub", "evaluation")},
            "imageDependencies": {"jiuwenbox": {}}}
        args = SimpleNamespace(values=values_path, resolved_images=images_path,
                               release="modeling", namespace="modeling-test")
        with mock.patch.object(INSTALL, "validate_release", return_value=release):
            _, merged = INSTALL.load_inputs(args, package)
        self.assertEqual(merged["global"]["access"]["listeners"][1]["listenerPort"], 15180)
        helm = shutil.which("helm")
        self.assertIsNotNone(helm)
        rendered = subprocess.run(  # NOSONAR(S4721): fixed Helm argv and synthetic values
            [helm, "template", "modeling", str(ROOT / "chart"), "-f", str(values_path),
             "-f", str(images_path)], capture_output=True, text=True, check=True,
            shell=False,
        )
        ingress = [item for item in yaml.safe_load_all(rendered.stdout)
                   if isinstance(item, dict) and item.get("kind") == "Ingress"]
        evaluation = next(item for item in ingress if item["metadata"]["name"] == "modeling-evaluation")
        self.assertEqual(evaluation["metadata"]["annotations"]["kubernetes.io/elb.port"], "15180")
        self.assertEqual(evaluation["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"],
                         {"number": 5180})

    def test_installer_bytecode_does_not_invalidate_extracted_package(self):
        root = self.package()
        cache = root / "deploy/__pycache__"
        cache.mkdir(parents=True)
        (cache / "common.cpython-310.pyc").write_bytes(b"synthetic bytecode")
        (cache / "upgrade.cpython-310.pyc").write_bytes(b"synthetic bytecode")
        verify_checksums(root)
        validate_release(root, "modeling")
        with self.assertRaisesRegex(ConfigError, "package file list differs"):
            verify_checksums(root, allow_runtime_bytecode=False)
        (root / "deploy/extra.txt").write_text("unexpected file")
        with self.assertRaisesRegex(ConfigError, "package file list differs"):
            verify_checksums(root)

    def test_archive_with_unlisted_bytecode_is_rejected(self):
        root = self.package()
        cache = root / "deploy/__pycache__"
        cache.mkdir(parents=True)
        (cache / "common.cpython-310.pyc").write_bytes(b"synthetic bytecode")
        archive = self.base / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as output:  # NOSONAR: synthetic test archive
            output.add(root, arcname=root.name)
        with self.assertRaisesRegex(ConfigError, "package file list differs"):
            safe_extract(archive, self.base / "extracted", "modeling")

    def test_archive_expansion_has_a_size_limit(self):
        archive = self.base / "oversize.tar.gz"
        with tarfile.open(archive, "w:gz") as output:  # NOSONAR: synthetic test archive
            item = tarfile.TarInfo("modeling-cce-demo-amd64/payload")
            item.size = 5
            output.addfile(item, io.BytesIO(b"12345"))
        with mock.patch("common.MAX_ARCHIVE_BYTES", 4):
            with self.assertRaisesRegex(ConfigError, "expanded size limit"):
                safe_extract(archive, self.base / "extracted", "modeling")

    def test_yaml_reader_rejects_duplicate_keys_and_python_tags(self):
        path = self.base / "synthetic.yaml"
        path.write_text("version: 1\nversion: 2\n")
        with self.assertRaisesRegex(ConfigError, "duplicate YAML key"):
            read_yaml(path)
        path.write_text("value: !!python/object/apply:os.system ['true']\n")
        with self.assertRaises(ConfigError):
            read_yaml(path)

    def test_installer_entry_disables_bytecode_generation(self):
        script = (ROOT / "install-cce.sh").read_text()
        joint = (ROOT.parents[2] / "deploy/install-consumer-modeling.sh").read_text()
        self.assertIn('exec python3 -B "$(dirname "$0")/install-cce.py"', script)
        self.assertIn('exec python3 -B "${script_dir}/joint-cce.py"', joint)
        self.assertIn('exec python3 -B "${script_dir}/../modeling/deploy/cce/joint-cce.py"', joint)

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
        release["images"]["ontoModeling"]["platform"] = "linux/arm64"
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

    def test_joint_external_auth_uses_independent_service_reference(self):
        root = self.base / "consumer-cce-external"
        (root / "deploy").mkdir(parents=True)
        (root / "deploy/endpoints.yaml").write_text(yaml.safe_dump({"schemaVersion": "cce-endpoints/v1",
            "endpoints": [{"id": "auth", "port": 5056, "apiVersion": "v1"},
                          {"id": "consumer-api", "port": 5055, "apiVersion": "v1"}]}))
        packages = {"consumer": (root, {"releaseVersion": "c2", "chartVersion": "2.0.0",
                                           "platform": "linux-amd64", "images": {"jiuwenbox": {}},
                                           "authDeploymentMode": "external"}),
                    "modeling": (self.base, {"releaseVersion": "m2", "chartVersion": "2.0.0",
                                               "platform": "linux-amd64"})}
        ref = {"apiVersion": "auth-v1", "serviceName": "auth-service",
               "namespace": "consumer-demo", "servicePort": 5056}
        consumer_values = {"sharedAuth": {"enabled": False}, "externalAuth": ref,
                           "global": {"auth": {"issuer": "https://auth.example.com", "audience": "demo"}}}
        modeling_values = {"global": {"auth": consumer_values["global"]["auth"]},
                           "externalAuth": dict(ref),
                           "externalConsumer": {"serviceName": "consumer-backend",
                                                "namespace": "consumer-demo", "servicePort": 5055}}
        config = {"consumer": {"releaseName": "consumer", "namespace": "consumer-demo"}}
        candidate = JOINT.candidate_combination(packages, consumer_values)
        self.assertEqual(candidate["authApiVersion"], "auth-v1")
        JOINT.validate_cross_side(config, packages, consumer_values, modeling_values, candidate)
        modeling_values["externalAuth"]["apiVersion"] = "v1"
        with self.assertRaisesRegex(ConfigError, "independent Consumer contract"):
            JOINT.validate_cross_side(config, packages, consumer_values, modeling_values, candidate)
        modeling_values["externalAuth"]["apiVersion"] = "auth-v1"
        modeling_values["externalAuth"]["serviceName"] = "other-auth"
        with self.assertRaisesRegex(ConfigError, "independent Consumer contract"):
            JOINT.validate_cross_side(config, packages, consumer_values, modeling_values, candidate)

    def test_joint_integration_refs_match_consumer_credential_owners(self):
        config = {"consumer": {"namespace": "consumer-demo"},
                  "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}
        consumer = {"global": {"integration": {
            "serviceTokenSecret": {"name": "modeling-source", "key": "token"},
            "evaluationTokenSecret": {"name": "evaluation-source", "key": "token"}}},
            "hub": {"enabled": True, "baseUrl": "https://modeling-hub.modeling-demo.svc.cluster.local:8200",
                    "tokenSecret": {"name": "hub-source", "key": "token"}},
            "flywheel": {"evaluationUrl": "https://modeling-evaluation.modeling-demo.svc.cluster.local:5180",
                         "tokenSecret": {"name": "feedback-source", "key": "token"}}}
        def local(name, source):
            return {"name": name, "key": "token", "sourceRef": {
                "namespace": "consumer-demo", "name": source, "key": "token"}}
        modeling = {"hub": {"secrets": {"consumerToken": local("hub", "hub-source")}},
                    "evaluation": {"feedback": {"enabled": True}, "secrets": {
                        "consumerToken": local("evaluation", "evaluation-source"),
                        "flywheelToken": local("feedback", "feedback-source")}}}
        JOINT.validate_integration_refs(config, consumer, modeling)
        for section, key in (("hub", "consumerToken"), ("evaluation", "consumerToken"),
                             ("evaluation", "flywheelToken")):
            changed = json.loads(json.dumps(modeling))
            changed[section]["secrets"][key]["sourceRef"]["name"] = "wrong-source"
            with self.assertRaisesRegex(ConfigError, "sourceRef differs"):
                JOINT.validate_integration_refs(config, consumer, changed)
        missing = json.loads(json.dumps(modeling))
        missing["evaluation"]["secrets"]["flywheelToken"] = {"name": "", "key": ""}
        with self.assertRaisesRegex(ConfigError, "flywheelToken"):
            JOINT.validate_integration_refs(config, consumer, missing)
        wrong_address = json.loads(json.dumps(consumer))
        wrong_address["flywheel"]["evaluationUrl"] += "/evaluation/api"
        with self.assertRaisesRegex(ConfigError, "Modeling Service"):
            JOINT.validate_integration_refs(config, wrong_address, modeling)
        del wrong_address["flywheel"]["evaluationUrl"]
        with self.assertRaisesRegex(ConfigError, "evaluationUrl"):
            JOINT.validate_integration_refs(config, wrong_address, modeling)

    def test_joint_defaults_consumer_owned_secrets_without_changing_input_values(self):
        config = {"consumer": {"namespace": "consumer-demo"},
                  "modeling": {"releaseName": "modeling", "namespace": "modeling-demo",
                               "values": self.base / "modeling-values.yaml"}}
        consumer = {"global": {"integration": {
            "serviceTokenSecret": {"name": "integration-source", "key": "service-token"},
            "evaluationTokenSecret": {"name": "evaluation-source", "key": "evaluation-token"}}},
            "hub": {"enabled": True, "tokenSecret": {"name": "hub-source", "key": "hub-token"}},
            "flywheel": {"tokenSecret": {"name": "flywheel-source", "key": "flywheel-token"}}}
        original = {"hub": {"secrets": {"consumerToken": {"name": "", "key": ""}}},
                    "evaluation": {"feedback": {"enabled": True}, "secrets": {
                        "consumerToken": {"name": "", "key": ""},
                        "flywheelToken": {"name": "", "key": ""}}}}
        config["modeling"]["values"].write_text(yaml.safe_dump(original))
        effective, values = JOINT.effective_modeling_values(
            config, consumer, self.base / "modeling-effective-values.yaml")
        self.assertEqual(yaml.safe_load(config["modeling"]["values"].read_text()), original)
        for path, local_name, source_name, source_key in (
            (("hub", "secrets", "consumerToken"), "modeling-hub-consumer", "hub-source", "hub-token"),
            (("evaluation", "secrets", "consumerToken"), "modeling-evaluation-consumer", "evaluation-source", "evaluation-token"),
            (("evaluation", "secrets", "flywheelToken"), "modeling-flywheel-consumer", "flywheel-source", "flywheel-token"),
        ):
            reference = values[path[0]][path[1]][path[2]]
            self.assertEqual(reference["name"], local_name)
            self.assertEqual(reference["sourceRef"], {"namespace": "consumer-demo", "name": source_name, "key": source_key})
        self.assertEqual(read_yaml(effective["modeling"]["values"]), values)
        disabled = json.loads(json.dumps(original))
        disabled["evaluation"]["feedback"]["enabled"] = False
        consumer["hub"]["enabled"] = False
        self.assertEqual(JOINT.default_consumer_secret_refs(config, consumer, disabled)["hub"]["secrets"]["consumerToken"],
                         {"name": "", "key": ""})
        self.assertEqual(JOINT.default_consumer_secret_refs(config, consumer, disabled)["evaluation"]["secrets"]["flywheelToken"],
                         {"name": "", "key": ""})

    def test_retired_integration_secret_is_optional_and_legacy_backup_remains_usable(self):
        schema = json.loads((ROOT / "chart/values.schema.json").read_text())
        values = INSTALL.merge(yaml.safe_load((ROOT / "chart/values.yaml").read_text()),
                               yaml.safe_load((ROOT / "values.customer.example.yaml").read_text()))
        values["global"]["resolvedImages"] = {name: "registry.example.com/app@sha256:" + "a" * 64
            for name in ("ontoModeling", "connectorGateway", "knowledgeHub", "evaluation", "jiuwenbox")}
        import jsonschema
        jsonschema.validate(values, schema)
        self.assertNotIn("integration", values["global"])
        legacy_values = json.loads(json.dumps(values))
        legacy_values["global"]["integration"] = {"serviceTokenSecret": {"name": "old", "key": "token"}}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(legacy_values, schema)
        consumer = {"global": {"integration": {"serviceTokenSecret": {"name": "consumer-owned", "key": "token"}}}}
        config = {"consumer": {"namespace": "consumer-demo"},
                  "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}
        self.assertEqual(JOINT.default_consumer_secret_refs(config, consumer, values), values)
        old = {"global": {"integration": {"serviceTokenSecret": {"name": "modeling-integration", "key": "token"}}}}
        active = {"modeling-notify": "uid-notify", "modeling-hub-internal": "uid-hub"}
        recorded = {**active, "modeling-integration": "uid-retired"}
        self.assertTrue(INSTALL._secret_identities_match(recorded, active, old))
        self.assertFalse(INSTALL._secret_identities_match({**recorded, "modeling-notify": "changed"}, active, old))
        self.assertFalse(INSTALL._secret_identities_match({**recorded, "unrelated": "uid"}, active, old))
        self.assertFalse(INSTALL._secret_identities_match(recorded, active, {}))

    def test_modeling_preflight_checks_existing_resources_when_consumer_services_are_deferred(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        merged = INSTALL.merge(
            yaml.safe_load((ROOT / "chart/values.yaml").read_text()),
            yaml.safe_load((ROOT / "values.customer.example.yaml").read_text()),
        )
        merged["modeling"]["secrets"]["authAdminToken"] = {"name": "", "key": ""}
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
                if rest[0] == merged["externalAuth"]["serviceName"]:
                    return {"spec": {"ports": [{"port": 5056}]}}
                raise ConfigError("Consumer Service is missing")
            raise AssertionError((verb, kind, rest))
        with mock.patch.object(INSTALL, "kube", side_effect=resource) as calls, \
             mock.patch.object(INSTALL, "validate_auth_owner"), \
             mock.patch.object(INSTALL, "probe_auth_from_modeling"), \
             mock.patch.object(INSTALL, "_optional_json", return_value=None):
            INSTALL.preflight(args, merged, check_external_services=False)
            self.assertTrue(any(call.args[1:3] == ("get", "service") for call in calls.call_args_list))
            self.assertEqual(sum(call.args[2] == "pvc" for call in calls.call_args_list), 3)
            self.assertTrue(any(call.args[2] == "nodes" for call in calls.call_args_list))
            with self.assertRaises(ConfigError):
                INSTALL.preflight(args, merged)
        def wrong_port(_args, verb, kind, *rest):
            if kind == "service":
                return {"spec": {"ports": [{"port": 9999}]}}
            return resource(_args, verb, kind, *rest)
        def present_secret(_args, *argv):
            return None
        with mock.patch.object(INSTALL, "kube", side_effect=wrong_port), \
             mock.patch.object(INSTALL, "_optional_json", side_effect=present_secret):
            with self.assertRaisesRegex(ConfigError, "service port mismatch"):
                INSTALL.preflight(args, merged)
        def ready_service(_args, verb, kind, *rest):
            if kind == "service":
                return {"spec": {"ports": [{"port": 5056 if rest[0] == merged["externalAuth"]["serviceName"] else 5055}]}}
            return resource(_args, verb, kind, *rest)
        with mock.patch.object(INSTALL, "kube", side_effect=ready_service), \
             mock.patch.object(INSTALL, "validate_auth_owner"), \
             mock.patch.object(INSTALL, "probe_auth_from_modeling"), \
             mock.patch.object(INSTALL, "_optional_json", side_effect=present_secret):
            INSTALL.preflight(args, merged)

    def test_first_install_can_wait_for_missing_auth_and_consumer_services(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        merged = INSTALL.merge(
            yaml.safe_load((ROOT / "chart/values.yaml").read_text()),
            yaml.safe_load((ROOT / "values.customer.example.yaml").read_text()),
        )
        merged["modeling"]["secrets"]["authAdminToken"] = {"name": "", "key": ""}

        def resource(_args, verb, kind, *rest):
            if kind == "namespace":
                return {"status": {"phase": "Active"}}
            if kind == "pvc":
                return {"status": {"phase": "Bound"},
                        "spec": {"volumeMode": "Filesystem", "accessModes": ["ReadWriteOnce"]}}
            if kind == "secret":
                return {"type": "kubernetes.io/dockerconfigjson"}
            if kind == "nodes":
                return {"items": [{"metadata": {"labels": {"kubernetes.io/arch": "amd64"}},
                                   "spec": {"unschedulable": False}}]}
            raise AssertionError((verb, kind, rest))

        def optional(_args, *rest):
            return None

        with mock.patch.object(INSTALL, "kube", side_effect=resource), \
             mock.patch.object(INSTALL, "_optional_json", side_effect=optional), \
             mock.patch.object(INSTALL, "_evaluation_has_data", return_value=False):
            self.assertEqual(INSTALL.preflight(args, merged, allow_missing_dependencies=True),
                             ["externalAuth", "externalConsumer"])
            with self.assertRaises(AssertionError):
                INSTALL.preflight(args, merged)

        def optional_wrong_port(_args, *rest):
            if rest[-3:] == ("get", "service", merged["externalAuth"]["serviceName"]):
                return {"spec": {"ports": [{"port": 9090}]}}
            return optional(_args, *rest)

        with mock.patch.object(INSTALL, "kube", side_effect=resource), \
             mock.patch.object(INSTALL, "_optional_json", side_effect=optional_wrong_port), \
             mock.patch.object(INSTALL, "_evaluation_has_data", return_value=False):
            with self.assertRaisesRegex(ConfigError, "service port mismatch"):
                INSTALL.preflight(args, merged, allow_missing_dependencies=True)

    def test_waiting_dependency_state_is_not_reported_ready_or_upgradeable(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo",
                               release="modeling", operation_id="install-demo")
        state = {"schemaVersion": "cce-modeling-dependencies/v1",
                 "stage": "waiting-dependencies", "pendingDependencies": ["externalAuth"]}
        with mock.patch.object(INSTALL, "release_status", return_value={"info": {"status": "deployed"}}), \
             mock.patch.object(INSTALL, "installed_release", return_value={
                 "releaseVersion": "demo", "chartVersion": "0.1.0"}), \
             mock.patch.object(INSTALL, "_cluster_operation_state", return_value=state), \
             mock.patch.object(INSTALL, "_pvc_bindings", return_value={}):
            result = INSTALL._diagnose(args, {"releaseVersion": "demo", "chartVersion": "0.1.0"}, {})
            self.assertFalse(result["ready"])
            self.assertIn("externalAuth", result["issues"][-1])
            with self.assertRaisesRegex(ConfigError, "verify Auth and Consumer"):
                INSTALL._require_upgrade_state_ready(args)

    def test_dependency_record_moves_from_waiting_to_complete_only_after_verify(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo",
                               release="modeling", operation_id="install-demo")
        merged = {"externalAuth": {"apiVersion": "auth-v1", "serviceName": "auth", "namespace": "shared", "servicePort": 5056},
                  "externalConsumer": {"serviceName": "consumer", "namespace": "shared", "servicePort": 5055}}
        state = None
        installed = {**merged, "global": {"operationId": "install-demo"}}

        def save(_args, document):
            nonlocal state
            state = json.loads(json.dumps(document))

        with mock.patch.object(INSTALL, "_cluster_operation_state", side_effect=lambda _args: state), \
             mock.patch.object(INSTALL, "installed_values", return_value=installed), \
             mock.patch.object(INSTALL, "_write_cluster_state", side_effect=save):
            INSTALL._record_dependencies(args, merged, ["externalAuth", "externalConsumer"],
                                         service_verified=False)
            self.assertEqual(state["stage"], "waiting-dependencies")
            INSTALL._verify_dependency_record(args, merged)
            INSTALL._record_dependencies(args, merged, [], service_verified=False)
            self.assertEqual(state["stage"], "verifying-service")
            INSTALL._record_dependencies(args, merged, [])
            self.assertEqual(state["stage"], "complete")
            self.assertEqual(state["pendingDependencies"], [])
            INSTALL._require_upgrade_state_ready(args)

    def test_independent_auth_owner_uses_current_owner_and_service_uid(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo")
        merged = {"global": {"auth": {"issuer": "https://auth.example.com", "audience": "demo"}},
                  "externalAuth": {"apiVersion": "auth-v1", "serviceName": "auth-service",
                                   "namespace": "consumer-demo", "servicePort": 5056},
                  "externalConsumer": {"serviceName": "consumer-backend", "namespace": "consumer-demo",
                                       "servicePort": 5055}}
        service = {"metadata": {"name": "auth-service", "uid": "service-uid", "annotations": {
            "meta.helm.sh/release-name": "auth-owner",
            "meta.helm.sh/release-namespace": "consumer-demo"}}}
        auth_state = {"phase": "ready", "identity": merged["global"]["auth"],
                      "serviceUID": "service-uid"}
        owner_values = {"authOnly": True, "serviceName": "auth-service", "workloadName": "auth-service",
                        "global": {"auth": merged["global"]["auth"]}}

        def resource(_args, verb, kind, name, *_rest):
            if kind == "configmap":
                if name != "auth-owner-cce-state":
                    raise AssertionError("Modeling must not read Consumer Auth ownership state")
                return {"data": {"state.json": json.dumps(auth_state)}}
            if kind == "deployment":
                return {"metadata": {"annotations": {"meta.helm.sh/release-name": "auth-owner"}}}
            raise AssertionError((verb, kind, name))

        with mock.patch.object(INSTALL, "kube", side_effect=resource), \
             mock.patch.object(INSTALL, "run", side_effect=lambda *command: json.dumps(
                 {"info": {"status": "deployed"}} if "status" in command else
                 owner_values)):
            INSTALL.validate_auth_owner(args, merged, service)
            owner_values["serviceName"] = "other-service"
            with self.assertRaisesRegex(ConfigError, "installed values differ"):
                INSTALL.validate_auth_owner(args, merged, service)
            owner_values["serviceName"] = "auth-service"
            auth_state["serviceUID"] = "another-uid"
            with self.assertRaisesRegex(ConfigError, "Service UID differs"):
                INSTALL.validate_auth_owner(args, merged, service)
            auth_state["serviceUID"] = "service-uid"
            service["metadata"]["uid"] = "replacement-uid"
            with self.assertRaisesRegex(ConfigError, "Service UID differs"):
                INSTALL.validate_auth_owner(args, merged, service)
            auth_state["serviceUID"] = "replacement-uid"
            INSTALL.validate_auth_owner(args, merged, service)

    def test_legacy_auth_reference_checks_consumer_ownership(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo")
        merged = {"global": {"auth": {"issuer": "https://auth.example.com", "audience": "demo"}},
                  "externalAuth": {"apiVersion": "v1", "serviceName": "consumer-auth",
                                   "namespace": "consumer-demo", "servicePort": 5056},
                  "externalConsumer": {"serviceName": "consumer-backend", "namespace": "consumer-demo",
                                       "servicePort": 5055}}
        service = {"metadata": {"uid": "legacy-uid", "annotations": {
            "meta.helm.sh/release-name": "consumer"}}}
        state = {"phase": "ready", "identity": {"auth": merged["global"]["auth"]},
                 "endpoints": [{"id": "auth", "serviceName": "consumer-auth",
                                "namespace": "consumer-demo", "port": 5056, "apiVersion": "v1"}]}
        with mock.patch.object(INSTALL, "kube", return_value={"data": {"state.json": json.dumps(state)}}):
            INSTALL.validate_auth_owner(args, merged, service)
            service["metadata"]["annotations"]["meta.helm.sh/release-name"] = "other"
            with self.assertRaisesRegex(ConfigError, "does not belong"):
                INSTALL.validate_auth_owner(args, merged, service)

    def test_modeling_auth_admin_token_must_match_auth_owner(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo")
        merged = {"modeling": {"secrets": {"authAdminToken":
            {"name": "modeling-auth-client", "key": "admin-token"}}}}
        owner_values = {"sharedAuth": {"adminTokenSecret":
            {"name": "auth-owner-token", "key": "token"}}}
        values = {"modeling-auth-client": "c2FtZS10b2tlbg==",
                  "auth-owner-token": "c2FtZS10b2tlbg=="}
        def secret(_args, name, namespace=None):
            value = values.get(name)
            self.assertIsNotNone(value)
            return {"type": "Opaque", "data": {"admin-token" if name == "modeling-auth-client"
                                                  else "token": value}}
        with mock.patch.object(INSTALL, "_read_secret", side_effect=secret):
            INSTALL.validate_auth_admin_token(args, merged, "auth", "consumer-demo", owner_values)
            values["modeling-auth-client"] = "ZGlmZmVyZW50"
            with self.assertRaisesRegex(ConfigError, "differs from Auth owner"):
                INSTALL.validate_auth_admin_token(args, merged, "auth", "consumer-demo", owner_values)

    def test_auth_probe_uses_modeling_namespace_and_removes_temporary_pod(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        merged = {"externalAuth": {"serviceName": "auth-service", "namespace": "consumer-demo",
                                   "servicePort": 5056},
                  "global": {"targetArch": "amd64", "resolvedImages": {"ontoModeling": "example/onto:demo"},
                             "auth": {"issuer": "https://auth.example.com", "audience": "demo"},
                             "imageDelivery": {"pullSecretNames": []}}}
        pods = []
        calls = []
        def create(command, payload):
            pods.append(json.loads(payload))
        def execute(*command):
            calls.append(command)
            return ""
        with mock.patch.object(INSTALL, "_run_with_input", side_effect=create), \
             mock.patch.object(INSTALL, "run", side_effect=execute), \
             mock.patch.object(INSTALL, "run_program", return_value=SimpleNamespace(returncode=0)) as probe:
            INSTALL.probe_auth_from_modeling(args, merged)
        self.assertEqual(pods[0]["metadata"]["namespace"], "modeling-demo")
        self.assertIn("auth-service.consumer-demo.svc.cluster.local:5056",
                      pods[0]["spec"]["containers"][0]["env"][0]["value"])
        self.assertEqual([item[5] for item in calls], ["wait", "delete"])
        self.assertNotIn("-i", probe.call_args.args[0])
        self.assertNotIn("input", probe.call_args.kwargs)

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
            {"metadata": {"name": "consumer-backend"}}]}), stderr="")
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
                self.assertEqual(json.loads(output.getvalue())["phase"], "modeling-preflight")
        finally:
            sys.argv = original

    def test_three_release_upgrade_preflights_both_sides_before_auth_change(self):
        config = {"stateDir": self.base, "cluster": {"context": "synthetic"},
                  "compatibilityFile": self.base / "compatibility.yaml",
                  "auth": {"package": self.base / "auth.tar.gz", "releaseName": "auth",
                           "namespace": "consumer-demo", "values": self.base / "auth-values.yaml",
                           "image": "registry.example.com/auth@sha256:" + "a" * 64}}
        installed = {"consumer": {"releaseVersion": "old", "chartVersion": "0.1.0"},
                     "modeling": {"releaseVersion": "old", "chartVersion": "0.1.0"}}
        releases = {}
        for side in JOINT.JOINT_SIDE_ORDER:
            config[side] = {"package": self.base / f"{side}.tar.gz", "releaseName": side,
                            "namespace": f"{side}-demo", "values": self.base / f"{side}-values.yaml",
                            "resolvedImages": self.base / f"{side}-images.yaml"}
            releases[side] = {"releaseVersion": "new", "chartVersion": "0.2.0", "platform": "linux-amd64"}
        calls = []

        class FakeLease:
            def __init__(self, *_): pass
            def acquire(self): pass
            def check(self): pass
            def release(self): pass

        def fake_invoke(side, action, _config, package, *_args, **_kwargs):
            calls.append((side, action, _args[0]))
            return {"schemaVersion": "cce-operation/v1", "status": "succeeded",
                    "releaseVersion": package[1]["releaseVersion"],
                    "chartVersion": package[1]["chartVersion"]}

        original = sys.argv
        try:
            with mock.patch.object(JOINT, "load_config", return_value=config), \
                 mock.patch.object(JOINT, "safe_extract", side_effect=lambda path, *_: self.base / path.name), \
                 mock.patch.object(JOINT, "prepare_auth_release", return_value=(self.base, {})), \
                 mock.patch.object(JOINT, "validate_release", side_effect=lambda _root, side: releases[side]), \
                 mock.patch.object(JOINT, "validate_combination", return_value={"id": "target"}), \
                 mock.patch.object(JOINT, "validate_auth_coordinates"), \
                 mock.patch.object(JOINT, "validate_cross_side"), \
                 mock.patch.object(JOINT, "read_yaml", side_effect=lambda path: {"global": {
                     "clusterFlavor": "standard", "access": {"elb": {"id": "synthetic"},
                         "listenerPort": 80 if "consumer" in str(path) else 8080}}}), \
                 mock.patch.object(JOINT, "effective_images", return_value=self.base / "effective.yaml"), \
                 mock.patch.object(JOINT, "operation_fingerprint", return_value="synthetic"), \
                 mock.patch.object(JOINT, "checksum", return_value="synthetic"), \
                 mock.patch.object(JOINT, "installed_identity", side_effect=lambda _config, side: installed[side]), \
                 mock.patch.object(JOINT, "unique_target_transition", return_value="transition-demo"), \
                 mock.patch.object(JOINT, "verified_joint_transition", return_value=None), \
                 mock.patch.object(JOINT, "ensure_auth_migration", return_value={"status": "succeeded", "phase": "already-independent"}), \
                 mock.patch.object(JOINT, "Lease", FakeLease), \
                 mock.patch.object(JOINT, "invoke", side_effect=fake_invoke), \
                 mock.patch.object(JOINT, "ensure_auth_release", side_effect=lambda *_args, **_kwargs: (
                     calls.append(("auth", "upgrade")) or {"status": "succeeded"})):
                sys.argv = ["joint-cce.py", "upgrade", "--scope", "both",
                            "--config", str(self.base / "joint.yaml")]
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(JOINT.main(), 0, output.getvalue())
                self.assertEqual([(side, action) for side, action, *_ in calls],
                                 [("consumer", "preflight"), ("modeling", "preflight"),
                                  ("auth", "upgrade"), ("consumer", "upgrade"),
                                  ("consumer", "verify"), ("modeling", "upgrade"),
                                  ("modeling", "verify")])
                calls.clear()
                def migrated(_config, _package, _operation_id, journal, _path, *, resume):
                    calls.append(("auth", "migrate", _operation_id))
                    journal["completed"].append("auth-migrated")
                    return {"status": "succeeded", "phase": "migrated"}
                with mock.patch.object(JOINT, "ensure_auth_migration", side_effect=migrated):
                    with redirect_stdout(StringIO()) as output:
                        self.assertEqual(JOINT.main(), 0, output.getvalue())
                self.assertEqual([(side, action) for side, action, *_ in calls],
                                 [("auth", "migrate"),
                                  ("consumer", "preflight"), ("modeling", "preflight"),
                                  ("auth", "upgrade"), ("consumer", "upgrade"),
                                  ("consumer", "verify"), ("modeling", "upgrade"),
                                  ("modeling", "verify")])
                migration_id = calls[0][2]
                self.assertEqual(calls[1][2], f"{migration_id}-consumer")
                self.assertEqual(calls[4][2], f"{migration_id}-consumer")
                self.assertEqual(calls[5][2], f"{migration_id}-consumer")
                self.assertEqual(calls[2][2], migration_id)
                self.assertEqual(calls[6][2], migration_id)
        finally:
            sys.argv = original

    def test_three_release_config_requires_matching_auth_coordinates(self):
        auth_values = self.base / "auth-values.yaml"
        auth_values.write_text(yaml.safe_dump({"global": {"auth": {"issuer": "https://auth.example.com",
            "audience": "synthetic"}}, "sharedAuth": {"enabled": True}}))
        config = {"auth": {"releaseName": "auth", "namespace": "consumer-demo",
                           "values": auth_values}, "consumer": {"releaseName": "consumer"}}
        reference = {"serviceName": "auth-auth", "namespace": "consumer-demo",
                     "servicePort": 5056, "apiVersion": "auth-v1"}
        consumer = {"global": {"auth": {"issuer": "https://auth.example.com", "audience": "synthetic"}},
                    "sharedAuth": {"enabled": False}, "externalAuth": reference}
        modeling = {"global": consumer["global"], "externalAuth": reference}
        JOINT.validate_auth_coordinates(config, consumer, modeling)
        modeling["externalAuth"] = {**reference, "serviceName": "another-auth"}
        with self.assertRaisesRegex(ConfigError, "modeling externalAuth differs"):
            JOINT.validate_auth_coordinates(config, consumer, modeling)
        migrated = {**reference, "serviceName": "consumer-auth"}
        consumer["externalAuth"] = migrated
        modeling["externalAuth"] = migrated
        config["consumer"] = {"releaseName": "consumer"}
        JOINT.validate_auth_coordinates(config, consumer, modeling)

    def test_auth_release_state_accepts_migrated_service_and_uses_owner_record(self):
        image = "registry.example.com/auth@sha256:" + "a" * 64
        identity = {"issuer": "https://auth.example.com", "audience": "synthetic"}
        config = {"cluster": {"context": "synthetic"},
                  "auth": {"releaseName": "auth", "namespace": "consumer-demo",
                           "values": self.base / "auth-values.yaml", "image": image},
                  "consumer": {"releaseName": "consumer", "namespace": "consumer-demo",
                               "values": self.base / "consumer-values.yaml"}}
        reference = {"serviceName": "consumer-auth", "namespace": "consumer-demo",
                     "servicePort": 5056, "apiVersion": "auth-v1"}
        (self.base / "auth-values.yaml").write_text(yaml.safe_dump({"global": {"auth": identity}}))
        (self.base / "consumer-values.yaml").write_text(yaml.safe_dump({"externalAuth": reference}))
        owner = {"phase": "ready", "identity": identity, "authPin": {"image": image}}
        owner_values = {"authOnly": True, "serviceName": "consumer-auth",
                        "workloadName": "consumer-auth", "global": {"auth": identity}}
        service = {"metadata": {"uid": "original-service-uid", "annotations": {
                    "meta.helm.sh/release-name": "auth", "meta.helm.sh/release-namespace": "consumer-demo"}},
                   "spec": {"ports": [{"port": 5056}]}}
        deployment = {"metadata": {"annotations": {"meta.helm.sh/release-name": "auth"}},
                      "spec": {"template": {"spec": {"containers": [{"name": "auth-server", "image": image}]}}}}

        def fake_kubectl(_config, *_args, **_kwargs):
            kind, name = _args[3:5]
            objects = {("configmap", "auth-cce-state"): {"data": {"state.json": json.dumps(owner)}},
                       ("service", "consumer-auth"): service,
                       ("deployment", "consumer-auth"): deployment}
            item = objects.get((kind, name))
            self.assertIsNotNone(item)
            return json.dumps(item)

        deployed = SimpleNamespace(returncode=0, stdout=json.dumps({"info": {"status": "deployed"}}), stderr="")
        with mock.patch.object(JOINT, "run_program", return_value=deployed), \
             mock.patch.object(JOINT, "run", side_effect=lambda *_: json.dumps(owner_values)), \
             mock.patch.object(JOINT, "kubectl", side_effect=fake_kubectl):
            self.assertEqual(JOINT.auth_release_state(config)["image"], image)
            owner_values["serviceName"] = "other-service"
            with self.assertRaisesRegex(ConfigError, "installed values are invalid"):
                JOINT.auth_release_state(config)
            owner_values["serviceName"] = "consumer-auth"
            self.assertEqual(JOINT.require_auth_ready(config)["image"], image)
            with self.assertRaisesRegex(ConfigError, "configured image"):
                JOINT.require_auth_ready(config, "registry.example.com/auth@sha256:" + "b" * 64)
            owner["serviceUID"] = "original-service-uid"
            self.assertEqual(JOINT.auth_release_state(config)["image"], image)
            service["metadata"]["uid"] = "replacement-service-uid"
            with self.assertRaisesRegex(ConfigError, "Service changed"):
                JOINT.auth_release_state(config)
            owner["serviceUID"] = "replacement-service-uid"
            self.assertEqual(JOINT.auth_release_state(config)["image"], image)
            service["metadata"]["uid"] = ""
            with self.assertRaisesRegex(ConfigError, "Service changed"):
                JOINT.auth_release_state(config)

    def test_auth_install_resume_accepts_only_completed_target_owner(self):
        image = "registry.example.com/auth@sha256:" + "a" * 64
        config = {"auth": {"image": image}}
        owner = {"state": {"authPin": {"image": image}}, "image": image}
        with mock.patch.object(JOINT, "auth_release_state", return_value=owner), \
             mock.patch.object(JOINT, "invoke_auth") as invoke:
            self.assertEqual(JOINT.ensure_auth_release("install", config, self.base, "operation-demo",
                                                       resume=True)["phase"], "already-ready")
            invoke.assert_not_called()
            with self.assertRaisesRegex(ConfigError, "already exists"):
                JOINT.ensure_auth_release("install", config, self.base, "new-operation")

    def test_auth_migration_runs_once_and_journals_completed_owner(self):
        config = {"cluster": {"context": "synthetic"},
                  "auth": {"releaseName": "auth", "namespace": "consumer-demo"},
                  "consumer": {"releaseName": "consumer", "namespace": "consumer-demo",
                               "values": self.base / "consumer-values.yaml"}}
        (self.base / "consumer-values.yaml").write_text(yaml.safe_dump({"externalAuth": {
            "serviceName": "consumer-auth"}}))
        journal = {"completed": [], "phase": "prepared"}
        path = self.base / "joint-journal.json"
        states = iter([("legacy", {"identity": {"authClaim": "auth-data"}}),
                       ("independent", {"authOwnership": {"operationId": "joint-demo"}})])
        absent = SimpleNamespace(returncode=1, stdout="", stderr="release: not found")
        service = {"metadata": {"uid": "service-uid", "annotations": {
            "meta.helm.sh/release-name": "consumer"}}, "spec": {"ports": [{"port": 5056}]}}
        deployment = {"metadata": {"annotations": {"meta.helm.sh/release-name": "consumer"}}}
        with mock.patch.object(JOINT, "consumer_auth_mode", side_effect=lambda _: next(states)), \
             mock.patch.object(JOINT, "run_program", return_value=absent), \
             mock.patch.object(JOINT, "kubectl", side_effect=[json.dumps(service), json.dumps(deployment)]), \
             mock.patch.object(JOINT, "require_auth_ready"), \
             mock.patch.object(JOINT, "invoke_auth", return_value={"status": "succeeded"}) as child:
            self.assertEqual(JOINT.ensure_auth_migration(config, self.base, "joint-demo", journal,
                                                          path, resume=False)["phase"], "migrated")
        self.assertEqual(child.call_args.args[:2], ("migrate", config))
        self.assertEqual(journal["completed"], ["auth-migrated"])
        self.assertEqual(json.loads(path.read_text())["phase"], "auth-migration-completed")

    def test_auth_migration_resume_uses_child_resume_only_for_recorded_flow(self):
        config = {"auth": {"releaseName": "auth"}, "consumer": {"releaseName": "consumer",
                  "values": self.base / "consumer-values.yaml"}}
        (self.base / "consumer-values.yaml").write_text(yaml.safe_dump({"externalAuth": {
            "serviceName": "consumer-auth"}}))
        journal = {"completed": [], "phase": "auth-migration-started"}
        path = self.base / "joint-journal.json"
        state = {"authOwnership": {"operationId": "joint-demo", "targetRelease": "auth",
                                   "step": "detaching"}}
        states = iter([("migrating", state), ("independent", state)])
        with mock.patch.object(JOINT, "consumer_auth_mode", side_effect=lambda _: next(states)), \
             mock.patch.object(JOINT, "require_auth_ready"), \
             mock.patch.object(JOINT, "invoke_auth", return_value={"status": "succeeded"}) as child:
            JOINT.ensure_auth_migration(config, self.base, "joint-demo", journal, path, resume=True)
        self.assertTrue(child.call_args.kwargs["resume"])
        with mock.patch.object(JOINT, "consumer_auth_mode", return_value=("migrating", state)), \
             mock.patch.object(JOINT, "invoke_auth") as child:
            with self.assertRaisesRegex(ConfigError, "original joint --resume"):
                JOINT.ensure_auth_migration(config, self.base, "another-demo",
                                            {"completed": [], "phase": "prepared"}, path, resume=False)
            child.assert_not_called()

    def test_auth_child_migrate_uses_consumer_release_and_boolean_resume(self):
        package = self.base / "onto-auth-test-amd64"
        (package / "cce").mkdir(parents=True)
        (package / "cce/auth-cce.py").write_text("# synthetic\n")
        config = {"auth": {"releaseName": "auth", "namespace": "consumer-demo"},
                  "consumer": {"releaseName": "consumer"},
                  "cluster": {"context": "synthetic"}, "stateDir": self.base}
        response = SimpleNamespace(returncode=0,
            stdout="AUTH_OWNERSHIP_MIGRATION=PASS source-detached target-adopted\n", stderr="")
        with mock.patch.object(JOINT.subprocess, "run", return_value=response) as command:
            self.assertEqual(JOINT.invoke_auth("migrate", config, package, "joint-demo",
                                               resume=True)["phase"], "migrated")
        args = command.call_args.args[0]
        self.assertEqual(args[args.index("--consumer-release") + 1], "consumer")
        self.assertIn("--resume", args)
        self.assertNotIn("--auth-image", args)

    def test_auth_contract_change_requires_verified_intermediate(self):
        transition = {"from": {"authApiVersion": "v1"},
                      "intermediate": {"authApiVersion": "v1"},
                      "to": {"authApiVersion": "auth-v1"}}
        with self.assertRaisesRegex(CompatibilityError, "verified auth-v1 intermediate"):
            JOINT.require_auth_contract_transition({"auth": {"releaseName": "auth"}}, transition)
        transition["intermediate"]["authApiVersion"] = "auth-v1"
        JOINT.require_auth_contract_transition({"auth": {"releaseName": "auth"}}, transition)

    def test_three_release_config_loader_accepts_auth_package_and_rejects_wrong_namespace(self):
        config = {"schemaVersion": "cce-joint/v1", "cluster": {"context": "synthetic"},
                  "stateDir": "./state", "compatibilityFile": "./compatibility.yaml",
                  "auth": {"package": "./auth.tar.gz", "releaseName": "auth", "namespace": "consumer-demo",
                           "values": "./auth-values.yaml",
                           "image": "registry.example.com/auth@sha256:" + "a" * 64},
                  "consumer": {"package": "./consumer.tar.gz", "releaseName": "consumer",
                               "namespace": "consumer-demo", "values": "./consumer-values.yaml",
                               "resolvedImages": "./consumer-images.yaml"},
                  "modeling": {"package": "./modeling.tar.gz", "releaseName": "modeling",
                               "namespace": "modeling-demo", "values": "./modeling-values.yaml",
                               "resolvedImages": "./modeling-images.yaml"}}
        path = self.base / "joint.yaml"
        path.write_text(yaml.safe_dump(config))
        loaded = JOINT.load_config(path)
        self.assertEqual(loaded["auth"]["package"], self.base / "auth.tar.gz")
        config["auth"]["namespace"] = "another-demo"
        path.write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(ConfigError, "Consumer namespace"):
            JOINT.load_config(path)

    def test_auth_child_install_and_upgrade_use_distinct_output_contracts(self):
        package = self.base / "onto-auth-test-amd64"
        (package / "cce").mkdir(parents=True)
        (package / "cce/auth-cce.py").write_text("# synthetic\n")
        config = {"auth": {"releaseName": "auth", "namespace": "consumer-demo",
                           "image": "registry.example.com/auth@sha256:" + "a" * 64,
                           "values": self.base / "auth-values.yaml"},
                  "cluster": {"context": "synthetic"}, "stateDir": self.base,
                  "timeoutSeconds": 30}
        response = SimpleNamespace(returncode=0, stdout="AUTH_CCE_FRESH_INSTALL=PASS ready\n", stderr="")
        with mock.patch.object(JOINT.subprocess, "run", return_value=response) as command:
            self.assertEqual(JOINT.invoke_auth("install", config, package, "operation-demo")["status"], "succeeded")
            self.assertIn("--package-dir", command.call_args.args[0])
            self.assertIn("--values", command.call_args.args[0])
            self.assertFalse(command.call_args.kwargs["shell"])
        response.stdout = json.dumps({"phase": "auth-ready"})
        with mock.patch.object(JOINT.subprocess, "run", return_value=response) as command:
            self.assertEqual(JOINT.invoke_auth("upgrade", config, package, "operation-demo")["phase"], "auth-ready")
            self.assertNotIn("--values", command.call_args.args[0])
        response.stdout = json.dumps({"status": "failed", "errorCode": "RESOURCE_NOT_READY"})
        with mock.patch.object(JOINT.subprocess, "run", return_value=response):
            with self.assertRaises(JOINT.ChildOperationError):
                JOINT.invoke_auth("upgrade", config, package, "operation-demo")

    def test_three_release_install_runs_auth_before_consumer_and_modeling(self):
        config = {"stateDir": self.base, "cluster": {"context": "synthetic"},
                  "compatibilityFile": self.base / "compatibility.yaml",
                  "auth": {"package": self.base / "auth.tar.gz", "releaseName": "auth",
                           "namespace": "consumer-demo", "values": self.base / "auth-values.yaml",
                           "image": "registry.example.com/auth@sha256:" + "a" * 64}}
        releases = {}
        for side in JOINT.JOINT_SIDE_ORDER:
            config[side] = {"package": self.base / f"{side}.tar.gz", "releaseName": side,
                            "namespace": f"{side}-demo", "values": self.base / f"{side}-values.yaml",
                            "resolvedImages": self.base / f"{side}-images.yaml"}
            releases[side] = {"releaseVersion": "demo", "chartVersion": "0.1.0", "platform": "linux-amd64"}
        calls = []

        class FakeLease:
            def __init__(self, *_): pass
            def acquire(self): pass
            def check(self): pass
            def release(self): pass

        def fake_invoke(side, action, _config, package, *_args, **_kwargs):
            calls.append((side, action))
            return {"schemaVersion": "cce-operation/v1", "status": "succeeded",
                    "releaseVersion": package[1]["releaseVersion"],
                    "chartVersion": package[1]["chartVersion"]}

        original = sys.argv
        try:
            with mock.patch.object(JOINT, "load_config", return_value=config), \
                 mock.patch.object(JOINT, "safe_extract", side_effect=lambda path, *_: self.base / path.name), \
                 mock.patch.object(JOINT, "prepare_auth_release", return_value=(self.base, {})), \
                 mock.patch.object(JOINT, "validate_release", side_effect=lambda _root, side: releases[side]), \
                 mock.patch.object(JOINT, "validate_combination", return_value={}), \
                 mock.patch.object(JOINT, "validate_auth_coordinates"), \
                 mock.patch.object(JOINT, "validate_cross_side"), \
                 mock.patch.object(JOINT, "read_yaml", side_effect=lambda path: {"global": {
                     "clusterFlavor": "standard", "access": {"elb": {"id": "synthetic"},
                         "listenerPort": 80 if "consumer" in str(path) else 8080}}}), \
                 mock.patch.object(JOINT, "effective_images", return_value=self.base / "effective.yaml"), \
                 mock.patch.object(JOINT, "operation_fingerprint", return_value="synthetic"), \
                 mock.patch.object(JOINT, "consumer_release_exists", return_value=False), \
                 mock.patch.object(JOINT, "release_exists", return_value=False), \
                 mock.patch.object(JOINT, "Lease", FakeLease), \
                 mock.patch.object(JOINT, "wait_for_consumer_rollout", side_effect=lambda *_: calls.append(("consumer", "rollout"))), \
                 mock.patch.object(JOINT, "invoke", side_effect=fake_invoke), \
                 mock.patch.object(JOINT, "ensure_auth_release", side_effect=lambda *_args, **_kwargs: (
                     calls.append(("auth", "install")) or {"status": "succeeded"})):
                sys.argv = ["joint-cce.py", "install", "--config", str(self.base / "joint.yaml")]
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(JOINT.main(), 0, output.getvalue())
                self.assertEqual(calls, [("auth", "install"), ("consumer", "install"),
                                         ("consumer", "rollout"),
                                         ("consumer", "verify"),
                                         ("modeling", "preflight"), ("modeling", "install")])
        finally:
            sys.argv = original

    def test_joint_install_waits_for_consumer_rollout_with_one_timeout_budget(self):
        values = self.base / "consumer-values.yaml"
        values.write_text(yaml.safe_dump({"solver": {"enabled": True}}))
        config = {"cluster": {"context": "synthetic"}, "timeoutSeconds": 30,
                  "consumer": {"releaseName": "consumer", "namespace": "consumer-demo",
                               "values": values}}
        calls = []
        lease = SimpleNamespace(check=lambda: calls.append("lease"))

        def rollout(command, **options):
            calls.append((command, options["timeout"]))
            return SimpleNamespace(returncode=0)

        with mock.patch.object(JOINT, "run_program", side_effect=rollout), \
             mock.patch.object(JOINT.time, "monotonic", side_effect=[0, 1, 11, 21]):
            JOINT.wait_for_consumer_rollout(config, [lease])
        commands = [item for item in calls if isinstance(item, tuple)]
        self.assertEqual([item[0][-2] for item in commands],
                         ["deployment/consumer-runtime", "deployment/consumer-frontend",
                          "deployment/consumer-ortools-solver"])
        self.assertEqual([item[0][-1] for item in commands],
                         ["--timeout=29s", "--timeout=19s", "--timeout=9s"])
        self.assertEqual(calls.count("lease"), 3)

    def test_joint_install_rollout_timeout_blocks_consumer_verify(self):
        values = self.base / "consumer-values.yaml"
        values.write_text("solver: {enabled: false}\n")
        config = {"cluster": {"context": "synthetic"}, "timeoutSeconds": 5,
                  "consumer": {"releaseName": "consumer", "namespace": "consumer-demo",
                               "values": values}}
        failed = SimpleNamespace(returncode=1)
        with mock.patch.object(JOINT, "run_program", return_value=failed), \
             mock.patch.object(JOINT.time, "monotonic", side_effect=[0, 1]):
            with self.assertRaises(JOINT.ChildOperationError) as caught:
                JOINT.wait_for_consumer_rollout(config, [])
        self.assertEqual(caught.exception.payload["errorCode"], "RESOURCE_NOT_READY")
        self.assertIn("consumer-runtime", caught.exception.payload["message"])
        with mock.patch.object(JOINT, "run_program") as command, \
             mock.patch.object(JOINT.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaises(JOINT.ChildOperationError):
                JOINT.wait_for_consumer_rollout(config, [])
        command.assert_not_called()

    def test_three_release_auth_failure_blocks_business_install(self):
        config = {"stateDir": self.base, "cluster": {"context": "synthetic"},
                  "compatibilityFile": self.base / "compatibility.yaml",
                  "auth": {"package": self.base / "auth.tar.gz", "releaseName": "auth",
                           "namespace": "consumer-demo", "values": self.base / "auth-values.yaml",
                           "image": "registry.example.com/auth@sha256:" + "a" * 64}}
        releases = {}
        for side in JOINT.JOINT_SIDE_ORDER:
            config[side] = {"package": self.base / f"{side}.tar.gz", "releaseName": side,
                            "namespace": f"{side}-demo", "values": self.base / f"{side}-values.yaml",
                            "resolvedImages": self.base / f"{side}-images.yaml"}
            releases[side] = {"releaseVersion": "demo", "chartVersion": "0.1.0", "platform": "linux-amd64"}
        original = sys.argv
        try:
            with mock.patch.object(JOINT, "load_config", return_value=config), \
                 mock.patch.object(JOINT, "safe_extract", side_effect=lambda path, *_: self.base / path.name), \
                 mock.patch.object(JOINT, "prepare_auth_release", return_value=(self.base, {})), \
                 mock.patch.object(JOINT, "validate_release", side_effect=lambda _root, side: releases[side]), \
                 mock.patch.object(JOINT, "validate_combination", return_value={}), \
                 mock.patch.object(JOINT, "validate_auth_coordinates"), \
                 mock.patch.object(JOINT, "validate_cross_side"), \
                 mock.patch.object(JOINT, "read_yaml", side_effect=lambda path: {"global": {
                     "clusterFlavor": "standard", "access": {"elb": {"id": "synthetic"},
                         "listenerPort": 80 if "consumer" in str(path) else 8080}}}), \
                 mock.patch.object(JOINT, "effective_images", return_value=self.base / "effective.yaml"), \
                 mock.patch.object(JOINT, "operation_fingerprint", return_value="synthetic"), \
                 mock.patch.object(JOINT, "consumer_release_exists", return_value=False), \
                 mock.patch.object(JOINT, "release_exists", return_value=False), \
                 mock.patch.object(JOINT, "ensure_auth_release", side_effect=JOINT.ChildOperationError(
                     "auth", "install", {"status": "failed", "errorCode": "RESOURCE_NOT_READY"})), \
                 mock.patch.object(JOINT, "Lease"), \
                 mock.patch.object(JOINT, "invoke") as business:
                sys.argv = ["joint-cce.py", "install", "--config", str(self.base / "joint.yaml")]
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(JOINT.main(), 1)
                business.assert_not_called()
                self.assertEqual(json.loads(output.getvalue())["failedSide"], "auth")
        finally:
            sys.argv = original
    def test_upgrade_precheck_handles_delegated_and_packaged_transitions(self):
        path, installed, packages = self.upgrade_fixture()
        manifest = yaml.safe_load(path.read_text())
        manifest["transitions"][0]["modelingDataMigration"] = "required"
        path.write_text(yaml.safe_dump(manifest))
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
                               transition_record=record_path, lock_holder="operation-demo", release="modeling")
        merged = {"global": {"targetArch": "amd64", "auth": {"issuer": "synthetic"}},
                  "modeling": {"workloads": {"onto": {"persistence": {"existingClaim": "onto-data"}}}},
                  "hub": {"persistence": {"existingClaim": "hub-data"},
                          "secrets": {"internal": {"name": "modeling-hub-internal",
                                                   "modelingTokenKey": "modeling-token",
                                                   "evaluationTokenKey": "evaluation-token"}}},
                  "evaluation": {"persistence": {"existingClaim": "evaluation-data"},
                                 "secrets": {"encryptionKey": {"name": "modeling-evaluation-internal",
                                                               "key": "encryption-key"}}}}
        old_values = json.loads(json.dumps(merged))
        for name in ("core", "agent"):
            old_values["modeling"]["workloads"][name] = {
                "persistence": {"existingClaim": f"{name}-data"}}
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
        manifest["transitions"][0]["modelingDataMigration"] = "none"
        (root / "deploy/compatibility.yaml").write_text(yaml.safe_dump(manifest))
        with self.assertRaises(ConfigError):
            precheck()

    def test_joint_compatibility_detects_core_migration_from_installed_values(self):
        config = {"cluster": {"context": "synthetic"},
                  "modeling": {"namespace": "modeling-demo", "releaseName": "modeling"}}
        values = {"modeling": {"workloads": {"core": {"persistence": {"existingClaim": "core-data"}},
                                              "agent": {"persistence": {"existingClaim": "agent-data"}},
                                              "onto": {"persistence": {"existingClaim": "onto-data"}}}}}
        with mock.patch.object(JOINT, "run", return_value=json.dumps(values)) as execute:
            self.assertEqual(JOINT.installed_modeling_workloads(config), UPGRADE.LEGACY_WORKLOADS)
        self.assertIn("get", execute.call_args.args)
        values["modeling"]["workloads"].pop("agent")
        with mock.patch.object(JOINT, "run", return_value=json.dumps(values)):
            with self.assertRaisesRegex(ConfigError, "Core and Agent"):
                JOINT.installed_modeling_workloads(config)

    def test_core_migration_job_checks_source_count_and_releases_claim(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling",
                               operation_id="upgrade-demo", timeout_seconds=30,
                               backup_record=self.base / "backup.json")
        args.backup_record.write_text(json.dumps({"dataEvidence": {"core": 2}}))
        merged = {"global": {"targetArch": "amd64", "resolvedImages": {"ontoModeling": "example/onto:demo"},
                             "imageDelivery": {"pullSecretNames": []}},
                  "modeling": {"workloads": {"onto": {"nodeSelector": {}}}}}
        before = {"core": {"claimName": "core-data"}, "onto": {"claimName": "onto-data"}}
        summary = {"schemaVersion": "core-project-migration/v1", "sourceProjects": 2,
                   "migrated": 1, "skipped": 1, "excluded": 1}
        created = []
        calls = []

        def create(_command, payload):
            created.append(json.loads(payload))

        def command(*argv):
            calls.append(argv)
            return json.dumps(summary) if "logs" in argv else ""

        with mock.patch.object(INSTALL, "_optional_json", return_value=None), \
             mock.patch.object(INSTALL, "_run_with_input", side_effect=create), \
             mock.patch.object(INSTALL, "run", side_effect=command):
            self.assertEqual(INSTALL.migrate_core_projects(args, merged, before), summary)
        job = created[0]
        pod = job["spec"]["template"]["spec"]
        self.assertTrue(pod["volumes"][0]["persistentVolumeClaim"]["readOnly"])
        self.assertEqual(pod["volumes"][1]["persistentVolumeClaim"]["claimName"], "onto-data")
        self.assertIn("--summary-file", pod["containers"][0]["command"])
        self.assertEqual([argv[5] for argv in calls], ["wait", "logs", "delete"])
        self.assertIsNone(args.migration_job)

        with mock.patch.object(INSTALL, "_optional_json", return_value=job), \
             mock.patch.object(INSTALL, "_run_with_input") as create_again, \
             mock.patch.object(INSTALL, "run", side_effect=command):
            self.assertEqual(INSTALL.migrate_core_projects(args, merged, before), summary)
        create_again.assert_not_called()

        job["spec"]["template"]["spec"]["containers"] = []
        with mock.patch.object(INSTALL, "_optional_json", return_value=job):
            with self.assertRaisesRegex(ConfigError, "differs"):
                INSTALL.migrate_core_projects(args, merged, before)

        summary["sourceProjects"] = 1
        with mock.patch.object(INSTALL, "_optional_json", return_value=None), \
             mock.patch.object(INSTALL, "_run_with_input", side_effect=create), \
             mock.patch.object(INSTALL, "run", side_effect=command):
            with self.assertRaisesRegex(ConfigError, "count differs"):
                INSTALL.migrate_core_projects(args, merged, before)
        self.assertIsNotNone(args.migration_job)

    def test_core_migration_failure_prevents_target_start(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling",
                               operation_id="upgrade-demo", timeout_seconds=30,
                               backup_record=self.base / "backup.json", values=self.base / "values.yaml",
                               resolved_images=self.base / "images.yaml")
        merged = {"global": {"targetArch": "amd64"}}
        before = {"core": {"claimName": "core-data"}, "onto": {"claimName": "onto-data"}}
        with mock.patch.object(INSTALL, "run") as command, \
             mock.patch.object(INSTALL, "wait_old_pods_gone"), \
             mock.patch.object(INSTALL, "bindings", return_value=before), \
             mock.patch.object(INSTALL, "assert_same_bindings"), \
             mock.patch.object(INSTALL, "_record_upgrade_stage"), \
             mock.patch.object(INSTALL, "migrate_core_projects", side_effect=ConfigError("migration failed")):
            with self.assertRaisesRegex(ConfigError, "migration failed"):
                INSTALL.apply_upgrade(args, self.base, {"releaseVersion": "m2"}, merged, before)
        self.assertEqual(command.call_count, 1)
        self.assertIn("modeling.workloads.onto.replicas=0", command.call_args.args)

    def test_standalone_upgrade_does_not_require_transition(self):
        release = {"releaseVersion": "m2", "chartVersion": "2.0.0", "platform": "linux-amd64"}
        backup_path = self.base / "backup.json"
        backup_path.write_text("{}")
        args = SimpleNamespace(
            backup_record=backup_path, transition_id=None, transition_record=None,
            lock_holder="operation-demo", standalone_upgrade=True, release="modeling",
        )
        merged = {
            "global": {"targetArch": "amd64", "auth": {"issuer": "synthetic"}},
            "modeling": {"workloads": {
                name: {"persistence": {"existingClaim": f"{name}-data"}}
                for name in UPGRADE.WORKLOADS
            }},
            "hub": {"persistence": {"existingClaim": "hub-data"},
                    "secrets": {"internal": {"name": "modeling-hub-internal",
                                             "modelingTokenKey": "modeling-token",
                                             "evaluationTokenKey": "evaluation-token"}}},
            "evaluation": {"persistence": {"existingClaim": "evaluation-data"},
                           "secrets": {"encryptionKey": {"name": "modeling-evaluation-internal",
                                                         "key": "encryption-key"}}},
        }
        old_values = json.loads(json.dumps(merged))
        with mock.patch.object(INSTALL, "check_delegated_lease"), \
             mock.patch.object(INSTALL, "installed_release", return_value={"releaseVersion": "m1", "chartVersion": "1.0.0"}), \
             mock.patch.object(INSTALL, "installed_values", return_value=old_values), \
             mock.patch.object(INSTALL, "bindings", return_value={"synthetic": "binding"}), \
             mock.patch.object(INSTALL, "verified_snapshots"), \
             mock.patch.object(INSTALL, "transition", side_effect=AssertionError("transition must not be read")):
            self.assertEqual(
                INSTALL.upgrade_precheck(args, self.base, release, merged),
                {"synthetic": "binding"},
            )
            args.transition_id = "any-value"
            self.assertEqual(INSTALL.upgrade_precheck(args, self.base, release, merged),
                             {"synthetic": "binding"})

    def test_automatic_backup_without_transition_keeps_pvc_evidence(self):
        args = SimpleNamespace(
            context="synthetic", namespace="modeling-demo", release="modeling",
            operation_id="upgrade-demo", transition_id=None,
            state_dir=self.base, timeout_seconds=10, min_free_percent=15,
            backup_record=None,
        )
        merged = {"global": {"targetArch": "amd64", "auth": {"issuer": "synthetic"}},
                  "modeling": {"workloads": {"onto": {"persistence": {"existingClaim": "onto-data"}}}},
                  "hub": {"persistence": {"existingClaim": "hub-data"}},
                  "evaluation": {"persistence": {"existingClaim": "evaluation-data"}}}
        claim_bindings = {
            name: {"claimName": f"{name}-data", "claimUid": f"uid-{name}",
                   "volumeName": f"pv-{name}"}
            for name in UPGRADE.WORKLOADS
        }
        old_values = {**merged}
        with mock.patch.object(INSTALL, "installed_release", return_value={"releaseVersion": "m1"}), \
             mock.patch.object(INSTALL, "installed_values", return_value=old_values), \
             mock.patch.object(INSTALL, "bindings", return_value=claim_bindings), \
             mock.patch.object(INSTALL, "_identity_record", return_value={}), \
             mock.patch.object(INSTALL, "_check_capacity"), \
             mock.patch.object(INSTALL, "_optional_json", return_value={"items": [{"metadata": {"name": "synthetic-class"}}]}), \
             mock.patch.object(INSTALL, "_count_persisted_data", return_value={"onto": 1, "hub": 2, "evaluation": 4}), \
             mock.patch.object(INSTALL, "_stop_for_backup"), \
             mock.patch.object(INSTALL, "_run_with_input", return_value=""), \
             mock.patch.object(INSTALL, "_ensure_volume_snapshot",
                               side_effect=lambda _args, name, _claim, *_rest: {
                                   "snapshotName": f"snapshot-{name}", "snapshotUid": f"snapshot-uid-{name}"}), \
             mock.patch.object(INSTALL, "transition", side_effect=AssertionError("transition must not be read")):
            record_path = INSTALL.prepare_auto_backup(args, self.base, {"releaseVersion": "m2"}, merged)
        record = json.loads(record_path.read_text())
        self.assertEqual(record["dataEvidence"], {"onto": 1, "hub": 2, "evaluation": 4})
        self.assertEqual(set(record["claims"]), set(UPGRADE.WORKLOADS))
        self.assertEqual(args.backup_record, record_path)

    def test_snapshot_class_precheck_failure_does_not_record_or_stop_upgrade(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling",
                               operation_id="upgrade-demo", state_dir=self.base,
                               min_free_percent=15, backup_record=None)
        merged = {"global": {"targetArch": "amd64", "auth": {"issuer": "synthetic"}},
                  "modeling": {"workloads": {"onto": {"persistence": {"existingClaim": "onto-data"}}}},
                  "hub": {"persistence": {"existingClaim": "hub-data"}},
                  "evaluation": {"persistence": {"existingClaim": "evaluation-data"}}}
        with mock.patch.object(INSTALL, "installed_release", return_value={"releaseVersion": "m1"}), \
             mock.patch.object(INSTALL, "installed_values", return_value=merged), \
             mock.patch.object(INSTALL, "bindings", return_value={}), \
             mock.patch.object(INSTALL, "_check_capacity"), \
             mock.patch.object(INSTALL, "_optional_json", return_value={"items": []}), \
             mock.patch.object(INSTALL, "_record_upgrade_stage") as record_stage, \
             mock.patch.object(INSTALL, "_stop_for_backup") as stop_writers:
            with self.assertRaisesRegex(ConfigError, "no unambiguous VolumeSnapshotClass"):
                INSTALL.prepare_auto_backup(args, self.base, {"releaseVersion": "m2"}, merged)
        record_stage.assert_not_called()
        stop_writers.assert_not_called()

    def test_snapshot_waits_for_detach_and_retries_transient_evs_error(self):
        args = SimpleNamespace(
            context="synthetic", namespace="modeling-demo", release="modeling",
            operation_id="operation-demo", snapshot_group="operation-demo", snapshot_class="synthetic-class",
        )
        volume_attachment_calls = 0
        snapshot_states = iter([
            None,
            {"spec": {"source": {"persistentVolumeClaimName": "core-data"}},
             "metadata": {"labels": {"modeling.cce/release": "modeling",
                                        "modeling.cce/backup-group": "operation-demo"}},
             "status": {"error": {"message": "EVS.1070: volume is detaching"}}},
            None,
            None,
            {"metadata": {"uid": "snapshot-uid", "creationTimestamp": "2026-10-21T00:00:00Z",
                           "labels": {"modeling.cce/release": "modeling",
                                       "modeling.cce/backup-group": "operation-demo"}},
             "spec": {"source": {"persistentVolumeClaimName": "core-data"}},
             "status": {"readyToUse": True, "boundVolumeSnapshotContentName": "content"}},
        ])
        applied = []

        def optional(_args, *rest):
            nonlocal volume_attachment_calls
            if rest == ("get", "volumeattachment"):
                volume_attachment_calls += 1
                return {"items": [{"spec": {"source": {"persistentVolumeName": "pv-core"},
                                              "nodeName": "node-a"},
                                   "status": {"attached": True}}]} if volume_attachment_calls == 1 else {"items": []}
            return next(snapshot_states)

        with mock.patch.object(INSTALL, "_optional_json", side_effect=optional), \
             mock.patch.object(INSTALL, "_run_with_input", side_effect=lambda _command, body: applied.append(body)), \
             mock.patch.object(INSTALL.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")), \
             mock.patch.object(INSTALL.time, "sleep"):
            result = INSTALL._ensure_volume_snapshot(args, "core", "core-data", "pv-core", time.monotonic() + 10)

        self.assertEqual(result, {"snapshotName": INSTALL._snapshot_name(args, "core"), "snapshotUid": "snapshot-uid"})
        self.assertEqual(volume_attachment_calls, 5)
        self.assertEqual(len(applied), 2)

    def test_snapshot_does_not_retry_permanent_error(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling",
                               operation_id="operation-demo", snapshot_group="operation-demo")
        failed = {"metadata": {"labels": {"modeling.cce/release": "modeling",
                                         "modeling.cce/backup-group": "operation-demo"}},
                  "spec": {"source": {"persistentVolumeClaimName": "core-data"}},
                  "status": {"error": {"message": "snapshot permission denied"}}}
        with mock.patch.object(INSTALL, "_optional_json", side_effect=lambda _args, *rest: (
                {"items": []} if rest == ("get", "volumeattachment") else failed)), \
             mock.patch.object(INSTALL, "_discard_snapshot_for_retry") as discard:
            with self.assertRaisesRegex(ConfigError, "snapshot permission denied"):
                INSTALL._ensure_volume_snapshot(args, "core", "core-data", "pv-core", time.monotonic() + 10)
        discard.assert_not_called()

    def test_snapshot_failure_restores_all_workloads_and_keeps_original_error(self):
        root = self.package()
        replicas = {name: 1 for name in UPGRADE.WORKLOADS}

        def fake_run(*command):
            if "scale" in command:
                workload = command[command.index("deployment") + 1].removeprefix("modeling-")
                replicas[workload] = int(command[-1].split("=")[1])
            elif "rollout" in command:
                workload = command[command.index("status") + 1].removeprefix("deployment/modeling-")
                self.assertEqual(replicas[workload], 1)
            return ""

        def fake_kube(_args, *_command):
            workload = _command[2].removeprefix("modeling-")
            return {"spec": {"replicas": replicas[workload]}}

        def fail_after_stop(args, *_):
            INSTALL._stop_for_backup(args)
            self.assertEqual(set(replicas.values()), {0})
            raise ConfigError("synthetic EVS detaching")

        old = sys.argv
        try:
            sys.argv = ["install-cce.py", "upgrade", "--context", "synthetic",
                        "--namespace", "modeling-demo", "--release", "modeling",
                        "--state-dir", str(self.base), "--values", str(self.base / "values.yaml"),
                        "--resolved-images", str(self.base / "images.yaml"),
                        "--operation-id", "operation-demo", "--auto-backup",
                        "--package-root", str(root)]
            with mock.patch.object(INSTALL, "load_inputs", return_value=(
                     {"releaseVersion": "m2", "chartVersion": "2.0.0"},
                     {"modeling": {"workloads": {
                         name: {"replicas": 1} for name in UPGRADE.WORKLOADS}}})), \
                 mock.patch.object(INSTALL, "endpoint_results", return_value=[]), \
                 mock.patch.object(INSTALL, "preflight"), \
                 mock.patch.object(INSTALL, "installed_values", return_value={
                     "modeling": {"workloads": {"onto": {"persistence": {"existingClaim": "onto-data"}}}},
                     "hub": {"persistence": {"existingClaim": "hub-data"}},
                     "evaluation": {"persistence": {"existingClaim": "evaluation-data"}}}), \
                 mock.patch.object(INSTALL, "ensure_application_secrets"), \
                 mock.patch.object(INSTALL, "check_delegated_lease"), \
                 mock.patch.object(INSTALL, "_require_upgrade_state_ready"), \
                 mock.patch.object(INSTALL, "release_status", return_value={"version": 6, "info": {"status": "deployed"}}), \
                 mock.patch.object(INSTALL.OperationLease, "acquire"), \
                 mock.patch.object(INSTALL.OperationLease, "release"), \
                 mock.patch.object(INSTALL, "wait_old_pods_gone"), \
                 mock.patch.object(INSTALL, "prepare_auto_backup", side_effect=fail_after_stop), \
                 mock.patch.object(INSTALL, "run", side_effect=fake_run), \
                 mock.patch.object(INSTALL, "_run_with_input", return_value=""), \
                 mock.patch.object(INSTALL, "kube", side_effect=fake_kube), \
                 redirect_stdout(StringIO()) as output:
                self.assertEqual(INSTALL.main(), 1)
        finally:
            sys.argv = old
        result = json.loads(output.getvalue())
        self.assertEqual(result["phase"], "automatic-backup")
        self.assertIn("synthetic EVS detaching", result["message"])
        self.assertEqual(result["recovery"]["status"], "ready")
        self.assertEqual(set(replicas.values()), {1})

    def test_failed_helm_stage_rolls_back_before_restarting_workloads(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling",
                               timeout_seconds=10, previous_helm_revision=6, helm_update_started=True)
        replicas = {name: 0 for name in UPGRADE.WORKLOADS}
        commands = []

        def fake_run(*command):
            commands.append(command)
            if "rollback" in command:
                self.assertEqual(set(replicas.values()), {0})
            elif "scale" in command:
                workload = command[command.index("deployment") + 1].removeprefix("modeling-")
                replicas[workload] = 1
            return ""

        with mock.patch.object(INSTALL, "release_status", return_value={"version": 7}), \
             mock.patch.object(INSTALL, "run", side_effect=fake_run), \
             mock.patch.object(INSTALL, "kube", side_effect=lambda _args, *_command: {
                 "spec": {"replicas": replicas[_command[2].removeprefix("modeling-")]}}):
            recovered = INSTALL.restore_workloads_after_failed_upgrade(args)
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(set(replicas.values()), {1})
        self.assertEqual(commands[0][:5], ("helm", "--kube-context", "synthetic", "--namespace", "modeling-demo"))
        self.assertIn("rollback", commands[0])

    def test_failed_helm_rollback_does_not_start_target_workloads(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling",
                               timeout_seconds=10, previous_helm_revision=6, helm_update_started=True)
        commands = []

        def fake_run(*command):
            commands.append(command)
            if "rollback" in command:
                raise RuntimeError("synthetic rollback failure")
            return ""

        with mock.patch.object(INSTALL, "release_status", return_value={"version": 7}), \
             mock.patch.object(INSTALL, "run", side_effect=fake_run), \
             mock.patch.object(INSTALL, "kube") as kube:
            recovered = INSTALL.restore_workloads_after_failed_upgrade(args)
        self.assertEqual(recovered["status"], "degraded")
        self.assertIn("rollback", commands[0])
        self.assertFalse(any("scale" in command for command in commands))
        kube.assert_not_called()

    def test_unfinished_upgrade_state_blocks_new_upgrade(self):
        args = SimpleNamespace(operation_id="new-operation")
        with mock.patch.object(INSTALL, "_cluster_operation_state", return_value={
                "action": "upgrade", "stage": "manual-review", "operationId": "old-operation"}):
            with self.assertRaisesRegex(ConfigError, "unfinished Modeling operation"):
                INSTALL._require_upgrade_state_ready(args)
        with mock.patch.object(INSTALL, "_cluster_operation_state", return_value={
                "action": "upgrade", "stage": "old-service-restored"}):
            INSTALL._require_upgrade_state_ready(args)

    def test_upgrade_stage_write_failure_warns_without_interrupting_operation(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling",
                               operation_id="upgrade-demo", state_dir=self.base)
        with mock.patch.object(INSTALL, "_write_cluster_state", side_effect=RuntimeError("synthetic write failure")), \
             mock.patch.object(INSTALL, "_write_private_json", side_effect=OSError("synthetic disk failure")):
            INSTALL._record_upgrade_stage(args, "target-verified")
        self.assertEqual(args.upgrade_stage, "target-verified")
        self.assertEqual(len(args.stage_warnings), 2)

    def test_private_operation_json_is_written_and_temp_file_closed(self):
        path = self.base / "state" / "operation.json"
        INSTALL._write_private_json(path, {"stage": "prepared"})
        self.assertEqual(json.loads(path.read_text()), {"stage": "prepared"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        INSTALL._write_private_json(path, {"stage": "complete"})
        self.assertEqual(json.loads(path.read_text()), {"stage": "complete"})

    def test_resume_allows_only_same_operation_before_helm_change(self):
        args = SimpleNamespace(operation_id="upgrade-demo", resume="upgrade-demo",
                               release="modeling", state_dir=self.base)
        state = {"action": "upgrade", "stage": "preparing-backup", "operationId": "upgrade-demo"}
        operation_state = INSTALL._operation_state_path(args)
        operation_state.write_text(json.dumps(state))
        with mock.patch.object(INSTALL, "_cluster_operation_state", return_value=state), \
             mock.patch.object(INSTALL, "installed_values", return_value={"global": {"operationId": "old"}}):
            INSTALL._require_upgrade_state_ready(args)
            state["stage"] = "backup-ready"
            with self.assertRaisesRegex(ConfigError, "stages differ"):
                INSTALL._require_upgrade_state_ready(args)
            operation_state.write_text(json.dumps(state))
            with self.assertRaisesRegex(ConfigError, "backup record is missing"):
                INSTALL._require_upgrade_state_ready(args)
            backup = self.base / f"modeling-{INSTALL._operation_slug(args.operation_id)}-modeling-backup.json"
            backup.write_text('{"schemaVersion":"cce-backup/v1"}')
            INSTALL._require_upgrade_state_ready(args)
            state["operationId"] = "another-operation"
            with self.assertRaisesRegex(ConfigError, "unfinished Modeling operation"):
                INSTALL._require_upgrade_state_ready(args)
            state["operationId"] = args.operation_id
            state["stage"] = "helm-staging"
            with self.assertRaisesRegex(ConfigError, "unfinished Modeling operation"):
                INSTALL._require_upgrade_state_ready(args)
        state["stage"] = "preparing-backup"
        operation_state.write_text(json.dumps(state))
        with mock.patch.object(INSTALL, "_cluster_operation_state", return_value=state), \
             mock.patch.object(INSTALL, "installed_values", return_value={"global": {"operationId": args.operation_id}}):
            with self.assertRaisesRegex(ConfigError, "outcome is uncertain"):
                INSTALL._require_upgrade_state_ready(args)

    def test_delegated_upgrade_creates_backup_before_helm_and_marks_snapshots_after_success(self):
        root = self.package()
        backup_path = self.base / "modeling-backup.json"
        backup_path.write_text('{"schemaVersion":"cce-backup/v1"}')
        calls = []

        def prepare(args, *_):
            calls.append("backup")
            args.backup_record = backup_path
            args.snapshot_group = "synthetic-group"
            return backup_path

        def apply(*_):
            calls.append("helm")

        old = sys.argv
        try:
            sys.argv = ["install-cce.py", "upgrade", "--context", "synthetic",
                        "--namespace", "modeling-demo", "--release", "modeling",
                        "--state-dir", str(self.base), "--values", str(self.base / "values.yaml"),
                        "--resolved-images", str(self.base / "images.yaml"),
                        "--operation-id", "operation-demo", "--lock-holder", "operation-demo",
                        "--transition-id", "upgrade-demo", "--auto-backup",
                        "--package-root", str(root)]
            with mock.patch.object(INSTALL, "load_inputs", return_value=(
                     {"releaseVersion": "m2", "chartVersion": "2.0.0"},
                     {"modeling": {"workloads": {
                         name: {"replicas": 1} for name in UPGRADE.WORKLOADS}}})), \
                 mock.patch.object(INSTALL, "endpoint_results", return_value=[]), \
                 mock.patch.object(INSTALL, "preflight"), \
                 mock.patch.object(INSTALL, "installed_values", return_value={
                     "modeling": {"workloads": {"onto": {"persistence": {"existingClaim": "onto-data"}}}},
                     "hub": {"persistence": {"existingClaim": "hub-data"}},
                     "evaluation": {"persistence": {"existingClaim": "evaluation-data"}}}), \
                 mock.patch.object(INSTALL, "ensure_application_secrets"), \
                 mock.patch.object(INSTALL, "check_delegated_lease"), \
                 mock.patch.object(INSTALL, "_require_upgrade_state_ready"), \
                 mock.patch.object(INSTALL, "release_status", return_value={"info": {"status": "deployed"}}), \
                 mock.patch.object(INSTALL, "prepare_auto_backup", side_effect=prepare) as backup, \
                 mock.patch.object(INSTALL, "upgrade_precheck", return_value={}) as precheck, \
                 mock.patch.object(INSTALL, "apply_upgrade", side_effect=apply) as upgrade, \
                 mock.patch.object(INSTALL, "_label_snapshot_group", side_effect=lambda *_: calls.append("successful")), \
                 mock.patch.object(INSTALL, "_cleanup_old_successful_snapshots", return_value=[]), \
                 redirect_stdout(StringIO()) as output:
                self.assertEqual(INSTALL.main(), 0)
                backup.side_effect = ConfigError("synthetic snapshot failure")
                with redirect_stdout(StringIO()) as failed:
                    self.assertEqual(INSTALL.main(), 1)
                self.assertEqual(json.loads(failed.getvalue())["phase"], "automatic-backup")
                self.assertEqual(upgrade.call_count, 1)
        finally:
            sys.argv = old
        self.assertEqual(calls, ["backup", "helm", "successful"])
        self.assertEqual(precheck.call_args.args[0].backup_record, backup_path)
        self.assertIn(str(backup_path), json.loads(output.getvalue())["artifacts"])

    def test_joint_modeling_upgrade_passes_auto_backup_options(self):
        root = self.base / "modeling-cce-demo"
        (root / "deploy").mkdir(parents=True)
        (root / "deploy/install-cce.sh").write_text("#!/bin/sh\n")
        config = {"cluster": {"context": "synthetic"}, "stateDir": self.base,
                  "modeling": {"namespace": "modeling-demo", "releaseName": "modeling",
                               "values": self.base / "values.yaml"}}
        package = (root, {"releaseVersion": "m2", "chartVersion": "2.0.0"})
        response = SimpleNamespace(returncode=0, stdout=json.dumps({"schemaVersion": "cce-operation/v1",
            "status": "succeeded", "releaseVersion": "m2", "chartVersion": "2.0.0"}))
        with mock.patch.object(JOINT.subprocess, "run", return_value=response) as call:
            JOINT.invoke("modeling", "upgrade", config, package, "operation-demo", self.base / "images.yaml",
                         transition_id="upgrade-demo", lock_holder="operation-demo", auto_backup=True,
                         snapshot_retention=3, min_free_percent=20)
        command = call.call_args.args[0]
        self.assertIn("--auto-backup", command)
        self.assertEqual(command[command.index("--snapshot-retention") + 1], "3")
        self.assertEqual(command[command.index("--min-free-percent") + 1], "20")
        self.assertNotIn("--backup-record", command)

    def test_standalone_operation_lease_document_and_expiry(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        lease = INSTALL.OperationLease(args, "operation-demo")
        document = json.loads(lease._document())
        self.assertEqual(document["metadata"]["name"], "modeling-cce-operation")
        self.assertEqual(document["spec"]["holderIdentity"], "operation-demo")
        self.assertTrue(INSTALL.OperationLease._active(document))
        document["spec"]["renewTime"] = "1970-01-01T00:00:00Z"
        self.assertFalse(INSTALL.OperationLease._active(document))

    def test_auto_snapshot_names_are_dns_safe_and_deterministic(self):
        args = SimpleNamespace(release="modeling", operation_id="modeling-upgrade/2026-10-20")
        first = INSTALL._snapshot_name(args, "onto")
        second = INSTALL._snapshot_name(args, "onto")
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 63)
        self.assertRegex(first, r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

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
                             backup_record="unused", transition_id="unused", lock_holder="operation-demo",
                             auto_backup=True)
            command = call.call_args.args[0]
            for forbidden in ("--compatibility", "--backup-record", "--transition-id", "--transition-record", "--auto-backup"):
                self.assertNotIn(forbidden, command)
            if action == "verify":
                self.assertEqual(command[command.index("--lock-holder") + 1], "operation-demo")

    def test_joint_invoke_keeps_child_failure_result_without_stderr(self):
        root = self.base / "consumer-cce-demo"
        (root / "deploy").mkdir(parents=True)
        (root / "deploy/install-cce.sh").write_text("#!/bin/sh\n")
        config = {"cluster": {"context": "synthetic"}, "stateDir": self.base,
                  "consumer": {"namespace": "consumer-demo", "releaseName": "consumer",
                               "values": self.base / "values.yaml"}}
        package = (root, {"releaseVersion": "c2", "chartVersion": "2.0.0"})
        child = {"schemaVersion": "cce-operation/v1", "status": "failed", "phase": "recovery",
                 "errorCode": "RECOVERY_REQUIRED", "message": "inspect recovery state",
                 "retryable": False, "recovery": {"status": "degraded", "issues": ["synthetic issue"]}}
        response = SimpleNamespace(returncode=1, stdout=json.dumps(child), stderr="synthetic private stderr")
        with mock.patch.object(JOINT.subprocess, "run", return_value=response):
            with self.assertRaises(JOINT.ChildOperationError) as caught:
                JOINT.invoke("consumer", "upgrade", config, package, "operation-demo", self.base / "images.yaml")
        self.assertEqual(caught.exception.payload, child)
        self.assertNotIn("synthetic private stderr", str(caught.exception))

    def test_joint_invoke_invalid_child_response_has_safe_error(self):
        root = self.base / "consumer-cce-demo"
        (root / "deploy").mkdir(parents=True)
        (root / "deploy/install-cce.sh").write_text("#!/bin/sh\n")
        config = {"cluster": {"context": "synthetic"}, "stateDir": self.base,
                  "consumer": {"namespace": "consumer-demo", "releaseName": "consumer",
                               "values": self.base / "values.yaml"}}
        package = (root, {"releaseVersion": "c2", "chartVersion": "2.0.0"})
        for stdout in ("not-json", "[]"):
            response = SimpleNamespace(returncode=1, stdout=stdout, stderr="synthetic private stderr")
            with mock.patch.object(JOINT.subprocess, "run", return_value=response):
                with self.assertRaises(JOINT.ChildOperationError) as caught:
                    JOINT.invoke("consumer", "preflight", config, package, "operation-demo", self.base / "images.yaml")
            self.assertEqual(caught.exception.payload["errorCode"], "CHILD_RESPONSE_INVALID")
            self.assertNotIn("synthetic private stderr", str(caught.exception))

    def test_joint_invoke_version_mismatch_has_child_context(self):
        root = self.base / "modeling-cce-demo"
        (root / "deploy").mkdir(parents=True)
        (root / "deploy/install-cce.sh").write_text("#!/bin/sh\n")
        config = {"cluster": {"context": "synthetic"}, "stateDir": self.base,
                  "modeling": {"namespace": "modeling-demo", "releaseName": "modeling",
                               "values": self.base / "values.yaml"}}
        package = (root, {"releaseVersion": "m2", "chartVersion": "2.0.0"})
        response = SimpleNamespace(returncode=0, stdout=json.dumps({"schemaVersion": "cce-operation/v1",
            "status": "succeeded", "releaseVersion": "wrong", "chartVersion": "2.0.0"}), stderr="")
        with mock.patch.object(JOINT.subprocess, "run", return_value=response):
            with self.assertRaises(JOINT.ChildOperationError) as caught:
                JOINT.invoke("modeling", "verify", config, package, "operation-demo", self.base / "images.yaml")
        self.assertEqual(caught.exception.side, "modeling")
        self.assertEqual(caught.exception.action, "verify")
        self.assertEqual(caught.exception.payload["errorCode"], "CHILD_RESPONSE_INVALID")

    def test_modeling_uninstall_keeps_external_resources_and_resumes_after_helm_removal(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling",
                               operation_id="remove-demo", state_dir=self.base, resume=None,
                               lock_holder="remove-demo", timeout_seconds=30)
        release = {"releaseVersion": "m2", "chartVersion": "2.0.0"}
        status = {"info": {"status": "deployed"}}
        commands = []
        with mock.patch.object(INSTALL, "validate_release", return_value=release), \
             mock.patch.object(INSTALL, "release_status", side_effect=[status, None, None]), \
             mock.patch.object(INSTALL, "installed_release", return_value=release), \
             mock.patch.object(INSTALL, "_cluster_operation_state", return_value=None), \
             mock.patch.object(INSTALL, "check_delegated_lease"), \
             mock.patch.object(INSTALL, "run", side_effect=lambda *command: commands.append(command)):
            self.assertEqual(INSTALL._uninstall(args, self.base), release)
            args.resume = args.operation_id
            self.assertEqual(INSTALL._uninstall(args, self.base), release)
        self.assertEqual(len(commands), 1)
        self.assertIn("uninstall", commands[0])
        self.assertFalse(any("delete" in command for command in commands))
        record = self.base / f"modeling-{INSTALL._operation_slug(args.operation_id)}-uninstall.json"
        self.assertTrue(record.is_file())

    def test_joint_uninstall_removes_modeling_before_consumer_and_can_resume(self):
        config = {"stateDir": self.base, "cluster": {"context": "synthetic"}, "timeoutSeconds": 30}
        for side in JOINT.JOINT_SIDE_ORDER:
            config[side] = {"releaseName": side, "namespace": f"{side}-demo"}
        existing = {"modeling": True, "consumer": True}
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
        def remove(_config, side):
            calls.append(side)
            if side == "consumer" and len(calls) == 2:
                raise RuntimeError("synthetic transient failure")
            existing[side] = False
        with mock.patch.object(JOINT, "_uninstall_release_identity",
                               side_effect=lambda _config, side: {"releaseVersion": side + "-v2", "chartVersion": "2.0.0"}
                               if existing[side] else None), \
             mock.patch.object(JOINT, "_assert_uninstall_state_ready"), \
             mock.patch.object(JOINT, "_assert_uninstall_manifest_safe"), \
             mock.patch.object(JOINT, "_assert_uninstall_release_chart"), \
             mock.patch.object(JOINT, "_cleanup_uninstall_residuals"), \
             mock.patch.object(JOINT, "_uninstall_side", side_effect=remove), \
             mock.patch.object(JOINT, "_delete_uninstall_state") as cleanup, \
             mock.patch.object(JOINT, "Lease", FakeLease), \
             mock.patch.object(JOINT, "invoke", side_effect=AssertionError("package invocation attempted")):
            with self.assertRaisesRegex(RuntimeError, "synthetic transient failure"):
                JOINT.uninstall_releases(config, "remove-demo", False, {"results": {}, "phase": "input"})
            journal = json.loads((self.base / "remove-demo.json").read_text())
            self.assertEqual(journal["completed"], ["modeling"])
            result = {"results": {}, "phase": "input"}
            JOINT.uninstall_releases(config, "remove-demo", True, result)
        self.assertEqual(calls, ["modeling", "consumer", "consumer"])
        self.assertEqual([call.args[1] for call in cleanup.call_args_list], ["modeling", "consumer"])
        self.assertEqual(result.get("status"), "succeeded")
        self.assertEqual(result["results"]["modeling"]["phase"], "already-uninstalled")
        self.assertEqual(json.loads((self.base / "remove-demo.json").read_text())["completed"],
                         ["modeling", "consumer"])

    def test_joint_uninstall_config_needs_no_package_paths(self):
        path = self.base / "uninstall.yaml"
        path.write_text(yaml.safe_dump({"schemaVersion": "cce-joint/v1",
            "cluster": {"context": "synthetic"}, "stateDir": "./state",
            "consumer": {"releaseName": "consumer", "namespace": "consumer-demo"},
            "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}))
        config = JOINT.load_uninstall_config(path)
        self.assertEqual(config["stateDir"], self.base / "state")
        self.assertEqual(set(config["consumer"]), {"releaseName", "namespace"})
        self.assertEqual(set(config["modeling"]), {"releaseName", "namespace"})

    def test_joint_uninstall_with_auth_removes_it_last_and_resumes(self):
        config = {"stateDir": self.base, "cluster": {"context": "synthetic"}, "timeoutSeconds": 30,
                  "auth": {"releaseName": "auth", "namespace": "consumer-demo"},
                  "consumer": {"releaseName": "consumer", "namespace": "consumer-demo"},
                  "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}
        existing = {side: True for side in ("auth", *JOINT.JOINT_SIDE_ORDER)}
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
        def remove(_config, side):
            calls.append(side)
            if side == "auth" and calls.count("auth") == 1:
                raise RuntimeError("synthetic Auth uninstall interruption")
            existing[side] = False
        with mock.patch.object(JOINT, "_uninstall_release_identity",
                               side_effect=lambda _config, side: {"releaseVersion": side + "-v2",
                               "chartVersion": "0.1.0"} if existing[side] else None), \
             mock.patch.object(JOINT, "_assert_uninstall_state_ready"), \
             mock.patch.object(JOINT, "_assert_uninstall_manifest_safe"), \
             mock.patch.object(JOINT, "_assert_uninstall_release_chart"), \
             mock.patch.object(JOINT, "_cleanup_uninstall_residuals"), \
             mock.patch.object(JOINT, "_delete_uninstall_state"), \
             mock.patch.object(JOINT, "_uninstall_side", side_effect=remove), \
             mock.patch.object(JOINT, "Lease", FakeLease):
            with self.assertRaisesRegex(RuntimeError, "synthetic Auth uninstall interruption"):
                JOINT.uninstall_releases(config, "remove-with-auth", False, {"results": {}, "phase": "input"})
            result = {"results": {}, "phase": "input"}
            JOINT.uninstall_releases(config, "remove-with-auth", True, result)
        self.assertEqual(calls, ["modeling", "consumer", "auth", "auth"])
        self.assertEqual(json.loads((self.base / "remove-with-auth.json").read_text())["completed"],
                         ["modeling", "consumer", "auth"])
        self.assertEqual(result["results"]["auth"]["phase"], "uninstalled")

    def test_joint_uninstall_resumes_after_auth_helm_release_was_removed(self):
        config = {"stateDir": self.base, "cluster": {"context": "synthetic"}, "timeoutSeconds": 30,
                  "auth": {"releaseName": "auth", "namespace": "consumer-demo"},
                  "consumer": {"releaseName": "consumer", "namespace": "consumer-demo"},
                  "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}
        existing = {side: True for side in ("auth", *JOINT.JOINT_SIDE_ORDER)}
        calls = []
        cleanup_attempts = []
        class FakeLease:
            def __init__(self, *_):
                pass
            def acquire(self):
                pass
            def check(self):
                pass
            def release(self):
                pass
        def remove(_config, side):
            calls.append(side)
            existing[side] = False
        def cleanup(_config, side):
            cleanup_attempts.append(side)
            if side == "auth" and cleanup_attempts.count("auth") == 1:
                raise RuntimeError("synthetic post-Helm interruption")
        with mock.patch.object(JOINT, "_uninstall_release_identity",
                               side_effect=lambda _config, side: {"releaseVersion": side + "-v2",
                               "chartVersion": "0.1.0"} if existing[side] else None), \
             mock.patch.object(JOINT, "_assert_uninstall_state_ready"), \
             mock.patch.object(JOINT, "_assert_uninstall_manifest_safe"), \
             mock.patch.object(JOINT, "_assert_uninstall_release_chart"), \
             mock.patch.object(JOINT, "_cleanup_uninstall_residuals", side_effect=cleanup), \
             mock.patch.object(JOINT, "_delete_uninstall_state"), \
             mock.patch.object(JOINT, "_uninstall_side", side_effect=remove), \
             mock.patch.object(JOINT, "Lease", FakeLease):
            with self.assertRaisesRegex(RuntimeError, "synthetic post-Helm interruption"):
                JOINT.uninstall_releases(config, "remove-auth-resume", False,
                                         {"results": {}, "phase": "input"})
            result = {"results": {}, "phase": "input"}
            JOINT.uninstall_releases(config, "remove-auth-resume", True, result)
        self.assertEqual(calls, ["modeling", "consumer", "auth"])
        self.assertEqual(result["results"]["auth"]["phase"], "uninstalled")

    def test_joint_uninstall_auth_manifest_requires_only_its_workload_and_service(self):
        config = {"cluster": {"context": "synthetic"},
                  "auth": {"releaseName": "auth", "namespace": "consumer-demo"},
                  "consumer": {"releaseName": "consumer", "namespace": "consumer-demo"}}
        manifest = ("kind: Deployment\nmetadata: {name: consumer-auth}\n"
                    "spec: {template: {spec: {containers: [{name: auth-server}]}}}\n---\n"
                    "kind: Service\nmetadata: {name: consumer-auth}\n"
                    "spec: {ports: [{port: 5056}]}\n")
        values = json.dumps({"authOnly": True, "workloadName": "consumer-auth",
                             "serviceName": "consumer-auth"})
        with mock.patch.object(JOINT, "run", side_effect=[manifest, values]), \
             mock.patch.object(JOINT, "_uninstall_cluster_state", return_value={}):
            JOINT._assert_uninstall_manifest_safe(config, "auth")
        with mock.patch.object(JOINT, "run", side_effect=[manifest +
                "---\nkind: PersistentVolumeClaim\nmetadata: {name: auth-data}\n", values]), \
             mock.patch.object(JOINT, "_uninstall_cluster_state", return_value={}):
            with self.assertRaisesRegex(ConfigError, "outside the uninstall allowlist"):
                JOINT._assert_uninstall_manifest_safe(config, "auth")

    def test_joint_auth_uninstall_requires_external_consumer(self):
        config = {"cluster": {"context": "synthetic"},
                  "auth": {"releaseName": "auth", "namespace": "consumer-demo"},
                  "consumer": {"releaseName": "consumer", "namespace": "consumer-demo"}}
        with mock.patch.object(JOINT, "_uninstall_cluster_state", return_value={"phase": "ready"}), \
             mock.patch.object(JOINT, "run", return_value=json.dumps({"sharedAuth": {"enabled": True}})):
            with self.assertRaisesRegex(ConfigError, "migrate Consumer-owned Auth"):
                JOINT._assert_uninstall_state_ready(config, "consumer")

    def test_joint_auth_uninstall_checks_independent_owner(self):
        config = {"cluster": {"context": "synthetic"},
                  "auth": {"releaseName": "auth", "namespace": "consumer-demo"}}
        values = json.dumps({"authOnly": True, "workloadName": "auth-auth"})
        owner = {"metadata": {"uid": "service-demo", "annotations": {
            "meta.helm.sh/release-name": "auth", "meta.helm.sh/release-namespace": "consumer-demo"}}}
        with mock.patch.object(JOINT, "_uninstall_cluster_state", return_value={
                "phase": "ready", "serviceUID": "service-demo", "authPin": {"image": "synthetic"}}), \
             mock.patch.object(JOINT, "run", return_value=values), \
             mock.patch.object(JOINT, "kubectl", side_effect=[json.dumps(owner), json.dumps(owner)]):
            JOINT._assert_uninstall_state_ready(config, "auth")
        wrong_owner = {"metadata": {"annotations": {"meta.helm.sh/release-name": "other"}}}
        with mock.patch.object(JOINT, "_uninstall_cluster_state", return_value={
                "phase": "ready", "serviceUID": "service-demo", "authPin": {"image": "synthetic"}}), \
             mock.patch.object(JOINT, "run", return_value=values), \
             mock.patch.object(JOINT, "kubectl", side_effect=[json.dumps(owner), json.dumps(wrong_owner)]):
            with self.assertRaisesRegex(ConfigError, "ownership or state changed"):
                JOINT._assert_uninstall_state_ready(config, "auth")

    def test_joint_uninstall_modeling_version_comes_from_installed_values(self):
        config = {"cluster": {"context": "synthetic"},
                  "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}
        release_version = "release-demo"
        metadata = {"name": "modeling", "namespace": "modeling-demo", "version": "0.2.0",
                    "appVersion": "0.2.0"}
        def command(args, **_kwargs):
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(metadata))
        with mock.patch.object(JOINT, "run_program", side_effect=command), \
             mock.patch.object(JOINT, "run", return_value=json.dumps({"global": {"releaseVersion": release_version}})):
            identity = JOINT._uninstall_release_identity(config, "modeling")
        self.assertEqual(identity, {"releaseVersion": release_version, "chartVersion": "0.2.0"})

    def test_joint_uninstall_entry_does_not_open_release_packages(self):
        path = self.base / "uninstall.yaml"
        path.write_text(yaml.safe_dump({"schemaVersion": "cce-joint/v1",
            "cluster": {"context": "synthetic"}, "stateDir": "./state",
            "consumer": {"releaseName": "consumer", "namespace": "consumer-demo"},
            "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}))
        original = sys.argv
        try:
            sys.argv = ["joint-cce.py", "uninstall", "--config", str(path)]
            with mock.patch.object(JOINT, "safe_extract", side_effect=AssertionError("package opened")), \
                 mock.patch.object(JOINT, "uninstall_releases",
                                   side_effect=lambda _config, _operation_id, _resume, result:
                                   result.update(status="succeeded", phase="uninstalled")), \
                 redirect_stdout(StringIO()) as output:
                self.assertEqual(JOINT.main(), 0)
        finally:
            sys.argv = original
        self.assertEqual(json.loads(output.getvalue())["phase"], "uninstalled")

    def test_joint_uninstall_rejects_a_manifest_containing_storage(self):
        config = {"cluster": {"context": "synthetic"},
                  "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}
        manifest = "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: modeling-data\n"
        with mock.patch.object(JOINT, "run", return_value=manifest):
            with self.assertRaisesRegex(ConfigError, "outside the uninstall allowlist"):
                JOINT._assert_uninstall_manifest_safe(config, "modeling")

    def test_joint_uninstall_cleans_owned_residual_workloads_only(self):
        config = {"cluster": {"context": "synthetic"},
                  "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}
        owned = {"metadata": {"name": "modeling-core", "namespace": "modeling-demo",
                               "labels": {"app.kubernetes.io/instance": "modeling"}}}
        external = {"metadata": {"name": "other", "namespace": "modeling-demo",
                                  "labels": {"app.kubernetes.io/instance": "other"}}}
        commands = []
        with mock.patch.object(JOINT, "run_program", side_effect=lambda command, **_: \
             SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"items": [owned, external]}))) as call:
            with mock.patch.object(JOINT, "run", side_effect=lambda *command: commands.append(command)):
                # The residual list is mocked as always present only to verify the delete selection;
                # the final recheck is exercised separately by the normal uninstall tests.
                with self.assertRaises(RuntimeError):
                    JOINT._cleanup_uninstall_residuals(config, "modeling")
        self.assertTrue(any("modeling-core" in command for command in commands))
        self.assertFalse(any("other" in command for command in commands))

    def test_joint_uninstall_removes_only_matching_installation_state(self):
        config = {"cluster": {"context": "synthetic"},
                  "consumer": {"releaseName": "consumer", "namespace": "consumer-demo"}}
        state = {"phase": "ready", "releaseName": "consumer"}
        commands = []
        def command(*parts):
            commands.append(parts)
            return ""
        with mock.patch.object(JOINT, "_uninstall_cluster_state", side_effect=[state, None]), \
             mock.patch.object(JOINT, "run", side_effect=command):
            JOINT._delete_uninstall_state(config, "consumer", "remove-demo")
        self.assertEqual(commands[0][0:5], ("kubectl", "--context", "synthetic", "-n", "consumer-demo"))
        self.assertEqual(commands[0][5:8], ("delete", "configmap", "consumer-cce-state"))
        with mock.patch.object(JOINT, "_uninstall_cluster_state", return_value={"phase": "upgrading"}), \
             mock.patch.object(JOINT, "run", side_effect=AssertionError("unexpected delete")):
            with self.assertRaisesRegex(ConfigError, "changed during uninstall"):
                JOINT._delete_uninstall_state(config, "consumer", "remove-demo")

    def test_joint_uninstall_can_remove_a_modeling_release_waiting_for_dependencies(self):
        config = {"cluster": {"context": "synthetic"},
                  "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}
        for stage in ("waiting-dependencies", "verifying-service"):
            with mock.patch.object(JOINT, "_uninstall_cluster_state",
                                   side_effect=[{"stage": stage}, {"stage": stage}, None]), \
                 mock.patch.object(JOINT, "run") as command:
                JOINT._assert_uninstall_state_ready(config, "modeling")
                JOINT._delete_uninstall_state(config, "modeling", "remove-demo")
                self.assertEqual(command.call_args.args[5:8],
                                 ("delete", "configmap", "modeling-cce-state"))

    def test_joint_main_reports_failed_child_and_preserves_original_error_after_lease_warning(self):
        class FakeLease:
            def __init__(self, *_):
                pass
            def acquire(self):
                pass
            def check(self):
                pass
            def release(self):
                raise RuntimeError("synthetic lease release failure")

        config = {"stateDir": self.base, "cluster": {"context": "synthetic"},
                  "compatibilityFile": self.base / "compatibility.yaml"}
        for side in JOINT.JOINT_SIDE_ORDER:
            config[side] = {"package": self.base / f"{side}.tar.gz", "releaseName": side,
                            "namespace": f"{side}-demo", "values": self.base / f"{side}-values.yaml",
                            "resolvedImages": self.base / f"{side}-images.yaml"}
        packages = {"consumer": {"releaseVersion": "c2", "chartVersion": "2.0.0", "platform": "linux-amd64"},
                    "modeling": {"releaseVersion": "m2", "chartVersion": "2.0.0", "platform": "linux-amd64"}}
        child = {"schemaVersion": "cce-operation/v1", "status": "failed", "phase": "recovery",
                 "errorCode": "RECOVERY_REQUIRED", "message": "inspect recovery state",
                 "retryable": True, "recovery": {"status": "degraded", "issues": ["synthetic issue"]}}
        def invoke(side, action, *_args, **_kwargs):
            if side == "consumer" and action == "preflight":
                raise JOINT.ChildOperationError(side, action, child)
            return {"schemaVersion": "cce-operation/v1", "status": "succeeded"}

        original = sys.argv
        try:
            sys.argv = ["joint-cce.py", "upgrade", "--config", str(self.base / "deployment.yaml"),
                        "--scope", "both"]
            with mock.patch.object(JOINT, "load_config", return_value=config), \
                 mock.patch.object(JOINT, "safe_extract", side_effect=lambda path, *_: self.base / path.name.removesuffix(".tar.gz")), \
                 mock.patch.object(JOINT, "validate_release", side_effect=lambda _root, side: packages[side]), \
                 mock.patch.object(JOINT, "validate_combination"), \
                 mock.patch.object(JOINT, "unique_target_transition", return_value="synthetic-transition"), \
                 mock.patch.object(JOINT, "validate_cross_side"), \
                 mock.patch.object(JOINT, "read_yaml", side_effect=lambda path: {"global": {"clusterFlavor": "standard", "access": {
                     "elb": {"id": "synthetic"}, "listenerPort": 443 if "consumer" in str(path) else 8443}}}), \
                 mock.patch.object(JOINT, "effective_images", return_value=self.base / "effective.yaml"), \
                 mock.patch.object(JOINT, "operation_fingerprint", return_value="synthetic"), \
                 mock.patch.object(JOINT, "checksum", return_value="synthetic"), \
                 mock.patch.object(JOINT, "installed_identity", return_value={"releaseVersion": "c1", "chartVersion": "1.0.0"}), \
                 mock.patch.object(JOINT, "verified_joint_transition"), \
                 mock.patch.object(JOINT, "Lease", FakeLease), \
                 mock.patch.object(JOINT, "invoke", side_effect=invoke), \
                 redirect_stdout(StringIO()) as output:
                self.assertEqual(JOINT.main(), 1)
            result = json.loads(output.getvalue())
        finally:
            sys.argv = original
        self.assertEqual(result["phase"], "consumer-preflight")
        self.assertEqual(result["failedSide"], "consumer")
        self.assertEqual(result["failedAction"], "preflight")
        self.assertEqual(result["errorCode"], "RECOVERY_REQUIRED")
        self.assertEqual(result["message"], child["message"])
        self.assertEqual(result["results"]["consumer"], child)
        self.assertFalse(result["retryable"])
        self.assertIn("could not release operation lease", result["warnings"])

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
        with tarfile.open(archive, "w:gz") as output:  # NOSONAR: synthetic test archive
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
            "ontoModeling": "registry.example.com/onto@sha256:" + "a" * 64,
            "knowledgeHub": "registry.example.com/hub@sha256:" + "a" * 64,
            "evaluation": "registry.example.com/evaluation@sha256:" + "a" * 64,
        }}}))
        config = {"consumer": {"resolvedImages": consumer_map}, "modeling": {"resolvedImages": modeling_map}}
        result = JOINT.effective_images(config, self.base, "consumer", "operation-demo")
        self.assertEqual(yaml.safe_load(result.read_text())["global"]["resolvedImages"]["jiuwenbox"], digest)
        self.assertNotIn("jiuwenbox", yaml.safe_load(modeling_map.read_text())["global"]["resolvedImages"])
        modeling_map.write_text(yaml.safe_dump({"global": {"resolvedImages": {"jiuwenbox": "registry.example.com/other@sha256:" + "d" * 64}}}))
        with self.assertRaises(ConfigError):
            JOINT.effective_images(config, self.base, "consumer", "operation-demo")

    def test_delegated_upgrade_requires_transition_before_cluster_access(self):
        args = ["install-cce.py", "upgrade", "--context", "demo", "--namespace", "modeling-demo",
                "--release", "modeling", "--lock-holder", "operation-demo"]
        old = sys.argv
        try:
            sys.argv = args
            with mock.patch.object(argparse.ArgumentParser, "exit", side_effect=ValueError("CLI rejected")) as exit_call:
                with self.assertRaisesRegex(ValueError, "CLI rejected"):
                    INSTALL.main()
        finally:
            sys.argv = old
        self.assertEqual(exit_call.call_args.args[0], 2)

    def test_standalone_upgrade_rejects_delegated_transition_record(self):
        backup_record = self.base / "backup-record.json"
        backup_record.write_text('{"status":"synthetic"}')
        old = sys.argv
        try:
            sys.argv = ["install-cce.py", "upgrade", "--context", "demo",
                        "--namespace", "modeling-demo", "--release", "modeling",
                        "--backup-record", str(backup_record),
                        "--transition-record", str(backup_record)]
            with mock.patch.object(argparse.ArgumentParser, "exit", side_effect=ValueError("CLI rejected")) as exit_call:
                with self.assertRaisesRegex(ValueError, "CLI rejected"):
                    INSTALL.main()
        finally:
            sys.argv = old
        self.assertEqual(exit_call.call_args.args[0], 2)

    def test_joint_upgrade_selects_only_an_unambiguous_target_transition(self):
        path, _, _ = self.upgrade_fixture()
        config = {"compatibilityFile": path}
        manifest = yaml.safe_load(path.read_text())
        target = next(item for item in manifest["combinations"] if item["id"] == "new")
        installed = {"consumer": {"releaseVersion": "c1", "chartVersion": "1.0.0"},
                     "modeling": {"releaseVersion": "m1", "chartVersion": "1.0.0"}}
        self.assertEqual(JOINT.unique_target_transition(config, target, installed), "upgrade-demo")
        duplicate = dict(manifest["transitions"][0], id="another-upgrade")
        manifest["transitions"].append(duplicate)
        path.write_text(yaml.safe_dump(manifest))
        with self.assertRaisesRegex(ConfigError, "current-to-target upgrade has no unique joint transition"):
            JOINT.unique_target_transition(config, target, installed)

    def test_joint_lease_uses_microsecond_rfc3339_for_create_and_release(self):
        args = {"cluster": {"context": "synthetic"},
                "consumer": {"releaseName": "consumer", "namespace": "consumer-demo"},
                "modeling": {"releaseName": "modeling", "namespace": "modeling-demo"}}
        lease = JOINT.Lease(args, "consumer", "operation-demo")
        document = json.loads(lease._document())
        for field in ("acquireTime", "renewTime"):
            self.assertRegex(document["spec"][field],
                             r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

        current = {"metadata": {"resourceVersion": "7"},
                   "spec": {"holderIdentity": "operation-demo",
                            "acquireTime": document["spec"]["acquireTime"]}}
        released = json.loads(lease._document(current))
        released["spec"].update(holderIdentity="", renewTime="1970-01-01T00:00:00.000000Z",
                                  leaseDurationSeconds=1)
        self.assertEqual(released["spec"].get("renewTime"), "1970-01-01T00:00:00.000000Z")

    def test_joint_lease_acquires_released_auth_migration_lease(self):
        config = {"cluster": {"context": "synthetic"},
                  "consumer": {"releaseName": "consumer", "namespace": "consumer-demo"}}
        lease = JOINT.Lease(config, "consumer", "joint-operation")
        released = {"metadata": {"resourceVersion": "7"},
                    "spec": {"holderIdentity": "", "renewTime": JOINT.lease_timestamp(
                        JOINT.datetime.now(JOINT.timezone.utc)), "leaseDurationSeconds": 30}}
        with mock.patch.object(lease, "_get", return_value=released), \
             mock.patch.object(JOINT, "kubectl") as replace, \
             mock.patch.object(JOINT.threading, "Thread") as thread:
            lease.acquire()
        self.assertEqual(replace.call_args.args[3:6], ("replace", "-f", "-"))
        self.assertEqual(json.loads(replace.call_args.kwargs["input_text"])["spec"]["holderIdentity"],
                         "joint-operation")
        thread.return_value.start.assert_called_once()

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
                with tarfile.open(destination / "modeling-0.1.0.tgz", "w:gz") as stream:  # NOSONAR: synthetic Chart fixture
                    stream.add(chart, arcname="modeling")
            return None

        old = sys.argv
        try:
            sys.argv = ["package-cce.py", "--version", "demo", "--arch", "amd64", "--output", str(output)]
            for image_id in ("ontoModeling", "connectorGateway", "knowledgeHub", "evaluation"):
                sys.argv.extend(("--image", f"{image_id}=registry.example.com/{image_id}@sha256:" + "a" * 64))
            from contextlib import redirect_stdout
            from io import StringIO
            with mock.patch.object(PACKAGE, "run", side_effect=fake_run), \
                 mock.patch.object(PACKAGE, "resolve_helm", return_value=("helm", "helm-tgz")), \
                 redirect_stdout(StringIO()):
                PACKAGE.main()
        finally:
            sys.argv = old
        with tarfile.open(output, "r:gz") as stream:  # NOSONAR: locally generated synthetic package
            names = stream.getnames()
            self.assertIn("modeling-cce-demo-amd64/deploy/chart/Chart.yaml", names)
            self.assertIn("modeling-cce-demo-amd64/deploy/upgrade.py", names)
            self.assertIn("modeling-cce-demo-amd64/images/knowledgeHub.tar", names)
            self.assertIn("modeling-cce-demo-amd64/images/connectorGateway.tar", names)
            self.assertIn("modeling-cce-demo-amd64/images/evaluation.tar", names)
            release = yaml.safe_load(stream.extractfile("modeling-cce-demo-amd64/release.yaml"))
            chart = yaml.safe_load(stream.extractfile("modeling-cce-demo-amd64/deploy/chart/Chart.yaml"))
        self.assertEqual(set(release["images"]), {"ontoModeling", "connectorGateway", "knowledgeHub", "evaluation"})
        self.assertEqual(chart["appVersion"], release["releaseVersion"])
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
                with tarfile.open(destination / "modeling-0.1.0.tgz", "w:gz") as stream:  # NOSONAR: synthetic Chart fixture
                    stream.add(chart, arcname="modeling")
            return None

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
        self.assertEqual(len([command for command in calls if command[:2] == ("docker", "save")]), 4)
        unpacked = self.base / "unpacked-local"
        unpacked.mkdir()
        release = validate_release(safe_extract(output, unpacked, "modeling"), "modeling")
        self.assertTrue(all(record["sourceManifestDigest"] is None for record in release["images"].values()))
        self.assertEqual({record["archiveRef"] for record in release["images"].values()}, {
            "modeling-offline/onto-modeling:demo", "modeling-offline/onto-modeling-connector-gateway:demo",
            "modeling-offline/knowledge-hub:demo", "modeling-offline/evaluation:demo",
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
        with tarfile.open(output, "r:gz") as stream:  # NOSONAR: locally generated synthetic package
            names = set(stream.getnames())
            self.assertIn("modeling-cce-demo-amd64/deploy/chart/Chart.yaml", names)
            for name in ("offline-package-build.md", "modeling-deployment.md", "joint-deployment.md"):
                self.assertIn(f"modeling-cce-demo-amd64/docs/{name}", names)
            release = yaml.safe_load(stream.extractfile("modeling-cce-demo-amd64/release.yaml"))
            chart = yaml.safe_load(stream.extractfile("modeling-cce-demo-amd64/deploy/chart/Chart.yaml"))
        self.assertEqual(release["chartDelivery"], "chart-source")
        self.assertEqual(chart["appVersion"], release["releaseVersion"])

    def test_missing_helm_download_failure_uses_chart_source(self):
        with mock.patch.object(PACKAGE.shutil, "which", return_value=None), \
             mock.patch.object(PACKAGE.platform, "machine", return_value="x86_64"), \
             mock.patch.object(PACKAGE.urllib.request, "urlopen", side_effect=OSError("offline")), \
             mock.patch.dict(os.environ, {"CCE_HELM_VERSION": "v3.19.0"}), \
             mock.patch("sys.stderr", new_callable=StringIO):
            self.assertEqual(PACKAGE.resolve_helm(self.base), (None, "chart-source"))

    def test_missing_helm_downloads_temporary_working_binary(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:  # NOSONAR: synthetic in-memory fixture
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
        self.assertEqual(run_test_command((helm, "version", "--short"), check=True,
                                          capture_output=True, text=True).stdout.strip(), "v3.19.0")

    def test_downloaded_helm_that_cannot_run_uses_chart_source(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:  # NOSONAR: synthetic in-memory fixture
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

    def test_oversized_helm_download_uses_chart_source(self):
        with mock.patch.object(PACKAGE.shutil, "which", return_value=None), \
             mock.patch.object(PACKAGE.platform, "machine", return_value="x86_64"), \
             mock.patch.object(PACKAGE.urllib.request, "urlopen", return_value=io.BytesIO(b"12345")), \
             mock.patch.object(PACKAGE, "MAX_HELM_DOWNLOAD_BYTES", 4), \
             mock.patch.dict(os.environ, {"CCE_HELM_VERSION": "v3.19.0"}):
            self.assertEqual(PACKAGE.resolve_helm(self.base), (None, "chart-source"))

    def test_external_commands_use_fixed_argv_without_shell(self):
        with mock.patch("common.subprocess.run", return_value=SimpleNamespace(returncode=0)) as process:
            from common import run_program
            run_program(("helm", "version", "--short"))
        self.assertFalse(process.call_args.kwargs["shell"])
        with self.assertRaisesRegex(ConfigError, "not allowed"):
            run_program(("sh", "-c", "true"))

    def test_build_script_loads_locally_or_pushes_explicit_registry(self):
        root = self.base / "build-root"
        script_dir = root / "modeling/deploy/cce"
        script_dir.mkdir(parents=True)
        shutil.copy2(ROOT / "build-images.sh", script_dir / "build-images.sh")
        (root / "modeling/onto-modeling/vendor/wheels").mkdir(parents=True)
        requirements = root / "modeling/onto-modeling/backend/requirements.txt"
        requirements.parent.mkdir(parents=True)
        requirements.write_text("synthetic-package==1.0\n")
        default_index = "https://repo.huaweicloud.com/repository/pypi/simple"
        index_hash = hashlib.sha256(default_index.encode()).hexdigest()
        (root / "modeling/onto-modeling/vendor/wheels/.prepared-amd64").write_text(
            f"{checksum(requirements)}:{index_hash}\n")
        drawio = root / "modeling/onto-modeling/vendor/drawio/webapp"
        drawio.mkdir(parents=True)
        (drawio / "index.html").write_text("synthetic fixture")
        binary = self.base / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS"\n')
        docker.chmod(0o755)
        calls = self.base / "docker-calls"
        environment = {**os.environ, "PATH": f"{binary}:{os.environ['PATH']}",
                       "ONTO_MODELING_PIP_INDEX_URL": default_index, "DOCKER_CALLS": str(calls)}
        run_test_command(["bash", str(script_dir / "build-images.sh"), "demo", "amd64"],
                       env=environment, check=True, capture_output=True, text=True)
        local_calls = calls.read_text().splitlines()
        self.assertEqual(sum("--load" in line for line in local_calls), 3)
        self.assertFalse(any("--push" in line or "imagetools" in line for line in local_calls))
        self.assertTrue(any("modeling-offline/onto-modeling:demo" in line for line in local_calls))
        self.assertTrue(any(f"--build-arg PIP_INDEX_URL={default_index}" in line
                            for line in local_calls))
        calls.unlink()
        run_test_command(["bash", str(script_dir / "build-images.sh"), "demo", "amd64", "registry.example.com/demo"],
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
        index_url = "https://pypi.example.com/simple"
        environment = {**os.environ, "PATH": f"{binary}:{os.environ['PATH']}",
                       "ONTO_MODELING_PIP_INDEX_URL": index_url,
                       "PIP_INDEX_URL": "https://fallback.example.com/simple",
                       "DEPENDENCY_CALLS": str(calls), "FIXTURE_ROOT": str(self.base / "fixture")}
        command = ["bash", str(script_dir / "build-images.sh"), "demo", "amd64"]
        first = run_test_command(command, env=environment, check=False, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_calls = calls.read_text().splitlines()
        self.assertEqual(sum(line.startswith("pip:") for line in first_calls), 1)
        self.assertTrue(any(line.startswith("pip:") and f"-i {index_url}" in line for line in first_calls))
        self.assertEqual(sum(line.startswith("npm:") for line in first_calls), 1)
        self.assertEqual(sum(line.startswith("docker:buildx build") for line in first_calls), 4)
        # 连接器网关是同一个 Dockerfile 的 connector-gateway 目标，单独打镜像
        self.assertTrue(any("--target connector-gateway" in line and "onto-modeling-connector-gateway:demo" in line
                            for line in first_calls))
        marker = root / "modeling/onto-modeling/vendor/wheels/.prepared-amd64"
        self.assertEqual(marker.read_text().strip(),
                         f"{checksum(requirements)}:{hashlib.sha256(index_url.encode()).hexdigest()}")
        self.assertTrue(any(f"--build-arg PIP_INDEX_URL={index_url}" in line for line in first_calls))
        calls.unlink()
        second = run_test_command(command, env=environment, check=False, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        second_calls = calls.read_text().splitlines()
        self.assertFalse(any(line.startswith("pip:") or line.startswith("npm:") for line in second_calls))
        self.assertEqual(sum(line.startswith("docker:buildx build") for line in second_calls), 4)
        requirements.write_text("synthetic-package==2.0\n")
        calls.unlink()
        third = run_test_command(command, env=environment, check=False, capture_output=True, text=True)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(sum(line.startswith("pip:") for line in calls.read_text().splitlines()), 1)
        calls.unlink()
        environment["ONTO_MODELING_PIP_INDEX_URL"] = "https://other.example.com/simple"
        fourth = run_test_command(command, env=environment, check=False, capture_output=True, text=True)
        self.assertEqual(fourth.returncode, 0, fourth.stderr)
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
        index_hash = hashlib.sha256(b"https://repo.huaweicloud.com/repository/pypi/simple").hexdigest()
        (wheels / ".prepared-amd64").write_text(f"{checksum(requirements)}:{index_hash}\n")
        binary = self.base / "bin"
        binary.mkdir()
        for name in ("bash", "dirname", "mkdir", "sha256sum", "cut", "cat"):
            (binary / name).symlink_to(shutil.which(name))
        docker = binary / "docker"
        docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOCKER_CALLS\"\n")
        docker.chmod(0o755)
        calls = self.base / "docker-calls"
        environment = {**os.environ, "PATH": str(binary),
                       "ONTO_MODELING_PIP_INDEX_URL": "https://repo.huaweicloud.com/repository/pypi/simple",
                       "DOCKER_CALLS": str(calls)}
        result = run_test_command(["bash", str(script_dir / "build-images.sh"), "demo", "amd64"],
                                env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host npm unavailable", result.stderr)
        self.assertFalse((root / "modeling/onto-modeling/vendor/drawio/webapp/index.html").exists())
        docker_calls = calls.read_text().splitlines()
        self.assertFalse(any(line.startswith("run") for line in docker_calls))
        self.assertEqual(sum(line.startswith("buildx build") for line in docker_calls), 3)
        calls.unlink()
        second = run_test_command(["bash", str(script_dir / "build-images.sh"), "demo", "amd64"],
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
        failed_pack = run_test_command(["bash", str(script_dir / "build-images.sh"), "demo", "amd64"],
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
        result = run_test_command(["bash", str(wrapper), "demo", "amd64"], cwd=self.base,
                                check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = calls.read_text().splitlines()
        self.assertEqual(lines[0], "build:demo amd64")
        self.assertIn("--local", lines[1])
        self.assertIn("modeling/dist/modeling-cce-demo-amd64.tar.gz", lines[1])
        custom_output = self.base / "release files" / "modeling demo.tar.gz"
        result = run_test_command(["bash", str(wrapper), "demo", "arm64", str(custom_output)],
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
        result = run_test_command(["bash", str(wrapper), "demo", "amd64"], check=False,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.read_text(), "build")

    def test_build_offline_can_remove_only_current_version_images_after_package(self):
        script_dir = self.base / "modeling/deploy/cce"
        script_dir.mkdir(parents=True)
        wrapper = script_dir / "build-offline.sh"
        shutil.copy2(ROOT / "build-offline.sh", wrapper)
        calls = self.base / "cleanup.log"
        (script_dir / "build-images.sh").write_text(
            f"#!/bin/sh\nprintf 'build:%s\\n' \"$*\" >> {calls}\n"
        )
        (script_dir / "package-cce.py").write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            f"pathlib.Path({str(calls)!r}).open('a').write('package:' + ' '.join(sys.argv[1:]) + '\\n')\n"
        )
        docker = self.base / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            f"printf 'docker:%s\\n' \"$*\" >> {calls}\n"
        )
        for path in (wrapper, script_dir / "build-images.sh", script_dir / "package-cce.py", docker):
            path.chmod(0o755)
        environment = {**os.environ, "PATH": f"{self.base}:{os.environ['PATH']}"}
        result = run_test_command(["bash", str(wrapper), "demo", "amd64", "--cleanup-images"],
                                cwd=self.base, env=environment, check=False,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = calls.read_text().splitlines()
        self.assertLess(lines.index("docker:image rm modeling-offline/onto-modeling:demo "
                                    "modeling-offline/onto-modeling-connector-gateway:demo "
                                    "modeling-offline/knowledge-hub:demo modeling-offline/evaluation:demo"),
                        len(lines))

    def test_hub_evaluation_chart_and_secret_contract(self):
        chart = ROOT / "chart"
        source = (chart / "templates/workloads.yaml").read_text()
        services = (chart / "templates/services.yaml").read_text()
        ingress = (chart / "templates/ingress.yaml").read_text()
        values = yaml.safe_load((chart / "values.yaml").read_text())
        images = yaml.safe_load((ROOT / "resolved-images.example.yaml").read_text())
        self.assertTrue({"knowledgeHub", "evaluation"}.issubset(images["global"]["resolvedImages"]))
        for component, path in (("hub", "/api/health"), ("evaluation", "/evaluation/api/health")):
            self.assertIn(f'name: {{{{ include "modeling.name" . }}}}-{component}', source)
            self.assertIn(path, source)
            self.assertIn(f'"{component}"', services)
        self.assertIn('eq .endpointId "evaluation"', ingress)
        self.assertEqual(values["hub"]["replicas"], 1)
        self.assertEqual(values["evaluation"]["replicas"], 1)
        self.assertEqual([item["endpointId"] for item in values["global"]["access"]["listeners"]][-1], "evaluation")
        schema = json.loads((chart / "values.schema.json").read_text())
        self.assertIn("sourceRef", schema["definitions"]["importableSecretRef"]["properties"])
        self.assertIn("internal", schema["properties"]["hub"]["properties"]["secrets"]["required"])
        self.assertIn("encryptionKey", schema["properties"]["evaluation"]["properties"]["secrets"]["required"])
        self.assertIn(".Values.hub.secrets.internal.modelingTokenKey", source)
        self.assertIn(".Values.hub.secrets.internal.evaluationTokenKey", source)
        self.assertIn(".Values.evaluation.secrets.encryptionKey.key", source)
        self.assertIn('name: ONTO_EVALUATION_SERVICE_TOKEN', source)
        self.assertIn('name: ONTO_WEB_EVAL_URL, value: "http://{{ include "modeling.name" . }}-evaluation:5180"', source)
        self.assertIn('name: EVAL_MODELING_SERVICE_TOKEN', source)
        self.assertEqual(source.count('.Values.evaluation.secrets.notificationToken.name'), 2)
        self.assertEqual(source.count('.Values.evaluation.secrets.notificationToken.key'), 2)
        self.assertIn("notificationToken", values["evaluation"]["secrets"])
        self.assertIn("notificationToken", schema["properties"]["evaluation"]["properties"]["secrets"]["required"])
        self.assertEqual(values["evaluation"]["feedback"], {"enabled": False, "notifyRuns": False})
        self.assertIn("flywheelToken", schema["properties"]["evaluation"]["properties"]["secrets"]["properties"])
        self.assertIn("EVAL_FLYWHEEL_SERVICE_TOKEN", source)
        self.assertIn("EVAL_FEEDBACK_ENABLED", source)
        self.assertIn("EVAL_NOTIFY_FEEDBACK_RUNS", source)
        self.assertNotIn('"{{ include "modeling.name" . }}-hub-internal"', source)

    def test_flywheel_values_render_and_validate(self):
        import jsonschema
        defaults = yaml.safe_load((ROOT / "chart/values.yaml").read_text())
        schema = json.loads((ROOT / "chart/values.schema.json").read_text())
        values = INSTALL.merge(defaults, yaml.safe_load((ROOT / "values.customer.example.yaml").read_text()))
        values["global"]["resolvedImages"] = {name: "registry.example.com/app@sha256:" + "a" * 64
            for name in ("ontoModeling", "connectorGateway", "knowledgeHub", "evaluation", "jiuwenbox")}
        values["evaluation"]["feedback"] = {"enabled": True, "notifyRuns": True}
        values["evaluation"]["secrets"]["flywheelToken"] = {"name": "feedback-credential", "key": "token"}
        jsonschema.validate(values, schema)
        helm = shutil.which("helm")
        self.assertIsNotNone(helm)
        rendered = run_test_command([str(Path(helm).resolve()), "template", "modeling-demo", str(ROOT / "chart"),
                                     "-f", str(ROOT / "values.customer.example.yaml"),
                                     "--set", "evaluation.feedback.enabled=true,evaluation.feedback.notifyRuns=true",
                                     "--set", "evaluation.secrets.flywheelToken.name=feedback-credential,evaluation.secrets.flywheelToken.key=token",
                                     "--set", "global.resolvedImages.ontoModeling=example/onto@sha256:" + "a" * 64,
                                     "--set", "global.resolvedImages.knowledgeHub=example/hub@sha256:" + "a" * 64,
                                     "--set", "global.resolvedImages.evaluation=example/eval@sha256:" + "a" * 64,
                                     "--set", "global.resolvedImages.jiuwenbox=example/box@sha256:" + "a" * 64],
                                    capture_output=True, text=True, check=True)
        deployments = [item for item in yaml.safe_load_all(rendered.stdout)
                       if item and item.get("kind") == "Deployment" and item["metadata"]["name"].endswith("-evaluation")]
        env = {item["name"]: item for item in deployments[0]["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["EVAL_FEEDBACK_ENABLED"]["value"], "true")
        self.assertEqual(env["EVAL_NOTIFY_FEEDBACK_RUNS"]["value"], "true")
        self.assertEqual(env["EVAL_FLYWHEEL_SERVICE_TOKEN"]["valueFrom"]["secretKeyRef"],
                         {"name": "feedback-credential", "key": "token"})

    def test_hub_evaluation_secret_creation_and_resume_reuses_existing(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        saved = {}
        created = []
        source_value = "c3ludGhldGljLXNlcnZpY2UtdG9rZW4="
        feedback_value = base64.b64encode(b"f" * 40).decode()
        source = {"type": "Opaque", "data": {"service-token": source_value}}
        merged = {
            "hub": {"secrets": {"internal": {"name": "hub-keys", "modelingTokenKey": "onto-key",
                                              "evaluationTokenKey": "eval-key"},
                                "consumerToken": {"name": "", "key": ""}}},
            "evaluation": {"secrets": {"encryptionKey": {"name": "eval-keys", "key": "fernet-key"},
                                       "notificationToken": {"name": "custom-notify", "key": "notify-key"},
                                       "consumerToken": {"name": "consumer-evaluation", "key": "service-token",
                                            "sourceRef": {"namespace": "consumer-demo", "name": "source", "key": "service-token"}},
                                       "flywheelToken": {"name": "consumer-feedback", "key": "feedback-token",
                                            "sourceRef": {"namespace": "consumer-demo", "name": "feedback-source", "key": "feedback-token"}},
                                       "judgeApiKey": {"name": "", "key": ""},
                                       "sqlConfig": {"name": "", "key": ""}}},
        }
        def get_secret(_args, name, namespace=None):
            if (namespace or args.namespace, name) == ("consumer-demo", "source"):
                return source
            if (namespace or args.namespace, name) == ("consumer-demo", "feedback-source"):
                return {"type": "Opaque", "data": {"feedback-token": feedback_value}}
            return saved.get((namespace or args.namespace, name))
        def create(_command, *, input, **_kwargs):
            document = json.loads(input)
            saved[(document["metadata"]["namespace"], document["metadata"]["name"])] = document
            created.append(document["metadata"]["name"])
            return SimpleNamespace(returncode=0, stderr="")
        with mock.patch.object(INSTALL, "_read_secret", side_effect=get_secret), \
             mock.patch.object(INSTALL, "run_program", side_effect=create):
            INSTALL.ensure_application_secrets(args, merged)
            first = dict(saved)
            INSTALL.ensure_application_secrets(args, merged)
        self.assertEqual(len(created), 5)
        self.assertNotIn((args.namespace, "integration"), saved)
        self.assertEqual(saved, first)
        self.assertIn("notify-key", saved[(args.namespace, "custom-notify")]["data"])
        self.assertIn((args.namespace, "hub-keys"), saved)
        self.assertEqual(set(saved[(args.namespace, "hub-keys")]["data"]), {"onto-key", "eval-key"})
        self.assertIn("fernet-key", saved[(args.namespace, "eval-keys")]["data"])
        self.assertEqual(saved[(args.namespace, "consumer-evaluation")]["data"]["service-token"], source_value)
        self.assertEqual(saved[(args.namespace, "consumer-feedback")]["data"]["feedback-token"], feedback_value)

    def test_hub_internal_tokens_must_be_distinct(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        merged = {"hub": {"secrets": {"internal": {"name": "custom-hub", "modelingTokenKey": "onto-key",
                                                    "evaluationTokenKey": "eval-key"}}}}
        def get_secret(_args, name, namespace=None):
            values = {"custom-hub": {"onto-key": "same", "eval-key": "same"}}
            data = values.get(name)
            if data is None:
                raise AssertionError(f"unexpected synthetic Secret: {name}")
            return {"data": data}
        with mock.patch.object(INSTALL, "_read_secret", side_effect=get_secret):
            with self.assertRaisesRegex(ConfigError, "must be distinct"):
                INSTALL._assert_distinct_internal_tokens(args, merged)

    def test_flywheel_token_must_differ_from_other_service_tokens(self):
        args = SimpleNamespace(namespace="modeling-demo")
        merged = {"hub": {"secrets": {"internal": {"name": "hub-internal", "modelingTokenKey": "onto",
                                                    "evaluationTokenKey": "evaluation"},
                                      "consumerToken": {"name": "hub-consumer", "key": "token"}}},
                  "evaluation": {"secrets": {"flywheelToken": {"name": "flywheel", "key": "token"},
                                             "consumerToken": {"name": "evaluation-consumer", "key": "token"},
                                             "notificationToken": {"name": "notification", "key": "token"}}}}
        other_token = base64.b64encode(b"d" * 40).decode()
        values = {}
        for name in ("hub-consumer", "evaluation-consumer", "notification"):
            values[name] = {"type": "Opaque", "data": {"token": other_token}}
        values["hub-internal"] = {"type": "Opaque", "data": {"onto": "other", "evaluation": "another"}}
        values["flywheel"] = {"type": "Opaque", "data": {"token": base64.b64encode(b"u" * 40).decode()}}
        with mock.patch.object(INSTALL, "_read_secret", side_effect=lambda _args, name: values[name]):
            INSTALL._assert_distinct_flywheel_token(args, merged)
            values["flywheel"]["data"]["token"] = base64.b64encode(b"short").decode()
            with self.assertRaisesRegex(ConfigError, "at least 32"):
                INSTALL._assert_distinct_flywheel_token(args, merged)
            values["flywheel"]["data"]["token"] = other_token
            with self.assertRaisesRegex(ConfigError, "must differ"):
                INSTALL._assert_distinct_flywheel_token(args, merged)

    def test_notification_secret_must_have_a_token_when_reused(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        with mock.patch.object(INSTALL, "_read_secret", return_value={"type": "Opaque", "data": {}}), \
             mock.patch.object(INSTALL, "run_program", side_effect=AssertionError("create attempted")):
            with self.assertRaisesRegex(ConfigError, "modeling-notify is incomplete"):
                INSTALL.ensure_application_secrets(args, {"evaluation": {"secrets": {"encryptionKey": {"name": "eval-keys"}}}})

    def test_precreated_internal_secrets_are_reused_without_create_permission(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        merged = {
            "hub": {"secrets": {"internal": {"name": "custom-hub", "modelingTokenKey": "onto-key",
                                              "evaluationTokenKey": "eval-key"},
                                "consumerToken": {"name": "", "key": ""}}},
            "evaluation": {"secrets": {"encryptionKey": {"name": "custom-evaluation", "key": "fernet-key"},
                                       "notificationToken": {"name": "precreated-notify", "key": "notification-key"},
                                       "consumerToken": {"name": "", "key": ""},
                                       "judgeApiKey": {"name": "", "key": ""},
                                       "sqlConfig": {"name": "", "key": ""}}},
        }
        secrets = {
            "custom-hub": {"onto-key": "first", "eval-key": "second"},
            "custom-evaluation": {"fernet-key": "third"},
            "precreated-notify": {"notification-key": "fifth"},
        }
        def get_secret(_args, name):
            data = secrets.get(name)
            if data is None:
                raise AssertionError(f"unexpected synthetic Secret: {name}")
            return {"type": "Opaque", "data": data}
        with mock.patch.object(INSTALL, "_read_secret", side_effect=get_secret), \
             mock.patch.object(INSTALL, "run_program", side_effect=AssertionError("create attempted")):
            INSTALL.ensure_application_secrets(args, merged)

    def test_internal_secret_create_failure_requires_precreation(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        merged = {
            "hub": {"secrets": {"internal": {"name": "custom-hub", "modelingTokenKey": "onto-key",
                                              "evaluationTokenKey": "eval-key"},
                                "consumerToken": {"name": "", "key": ""}}},
            "evaluation": {"secrets": {"encryptionKey": {"name": "custom-evaluation", "key": "fernet-key"},
                                       "consumerToken": {"name": "", "key": ""},
                                       "judgeApiKey": {"name": "", "key": ""},
                                       "sqlConfig": {"name": "", "key": ""}}},
        }
        with mock.patch.object(INSTALL, "_read_secret", return_value=None), \
             mock.patch.object(INSTALL, "run_program", return_value=SimpleNamespace(returncode=1, stderr="forbidden")):
            with self.assertRaisesRegex(ConfigError, "unable to create Secret modeling-notify"):
                INSTALL.ensure_application_secrets(args, merged)

    def test_existing_internal_secret_references_cannot_change_during_upgrade(self):
        args = SimpleNamespace(release="modeling")
        merged = {
            "hub": {"secrets": {"internal": {"name": "custom-hub", "modelingTokenKey": "onto-key",
                                              "evaluationTokenKey": "eval-key"}}},
            "evaluation": {"secrets": {"encryptionKey": {"name": "custom-evaluation", "key": "fernet-key"}}},
        }
        with mock.patch.object(INSTALL, "installed_values", return_value={"hub": {}, "evaluation": {}}):
            with self.assertRaisesRegex(ConfigError, "cannot change during upgrade"):
                INSTALL._assert_unchanged_internal_secrets(args, merged)
        old = {
            "hub": {"secrets": {"internal": merged["hub"]["secrets"]["internal"]}},
            "evaluation": {"secrets": {"encryptionKey": merged["evaluation"]["secrets"]["encryptionKey"]}},
        }
        with mock.patch.object(INSTALL, "installed_values", return_value=old):
            INSTALL._assert_unchanged_internal_secrets(args, merged)
        configured = {**merged, "evaluation": {"secrets": {
            **merged["evaluation"]["secrets"],
            "notificationToken": {"name": "precreated-notify", "key": "notification-key"}}}}
        with mock.patch.object(INSTALL, "installed_values", return_value=old):
            INSTALL._assert_unchanged_internal_secrets(args, configured)
        with mock.patch.object(INSTALL, "installed_values", return_value=configured):
            INSTALL._assert_unchanged_internal_secrets(args, configured)
            with self.assertRaisesRegex(ConfigError, "notification Secret name and key cannot change"):
                INSTALL._assert_unchanged_internal_secrets(args, merged)
        changed = {**merged, "evaluation": {"secrets": {"encryptionKey": {"name": "other", "key": "fernet-key"}}}}
        with mock.patch.object(INSTALL, "installed_values", return_value=old):
            with self.assertRaisesRegex(ConfigError, "cannot change during upgrade"):
                INSTALL._assert_unchanged_internal_secrets(args, changed)

    def test_evaluation_secret_source_mismatch_blocks_reuse(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling")
        merged = {
            "hub": {"secrets": {"internal": {"name": "modeling-hub-internal", "modelingTokenKey": "modeling-token",
                                              "evaluationTokenKey": "evaluation-token"},
                                "consumerToken": {"name": "", "key": ""}}},
            "evaluation": {"secrets": {"encryptionKey": {"name": "modeling-evaluation-internal", "key": "encryption-key"},
                                       "consumerToken": {"name": "consumer-evaluation", "key": "service-token",
                "sourceRef": {"namespace": "consumer-demo", "name": "source", "key": "service-token"}},
                "judgeApiKey": {"name": "", "key": ""}, "sqlConfig": {"name": "", "key": ""}}},
        }
        def get_secret(_args, name, namespace=None):
            data = {"modeling-hub-internal": {"modeling-token": "a", "evaluation-token": "b"},
                    "modeling-evaluation-internal": {"encryption-key": "c"},
                    "modeling-notify": {"service-token": "e"},
                    "consumer-evaluation": {"service-token": "old"},
                    "source": {"service-token": "new"}}[name]
            return {"type": "Opaque", "data": data}
        with mock.patch.object(INSTALL, "_read_secret", side_effect=get_secret):
            with self.assertRaisesRegex(ConfigError, "source and target Secret differ"):
                INSTALL.ensure_application_secrets(args, merged)

    def test_hub_evaluation_upgrade_uses_five_snapshots_and_evidence(self):
        args = SimpleNamespace(context="synthetic", namespace="modeling-demo", release="modeling",
                               operation_id="upgrade-demo", state_dir=self.base, timeout_seconds=10,
                               min_free_percent=15, backup_record=None)
        old = {"global": {"targetArch": "amd64", "auth": {"issuer": "synthetic"}},
               "modeling": {"workloads": {name: {"persistence": {"existingClaim": name + "-pvc"}}
                                          for name in UPGRADE.LEGACY_WORKLOADS}},
               "hub": {"persistence": {"existingClaim": "hub-pvc"}},
               "evaluation": {"persistence": {"existingClaim": "evaluation-pvc"}}}
        before = {name: {"claimName": name + "-pvc", "claimUid": name + "-uid",
                         "volumeName": name + "-pv"} for name in UPGRADE.LEGACY_EXPANDED_WORKLOADS}
        evidence = dict.fromkeys(UPGRADE.LEGACY_EXPANDED_WORKLOADS, 1)
        with mock.patch.object(INSTALL, "installed_release", return_value={"releaseVersion": "m1"}), \
             mock.patch.object(INSTALL, "installed_values", return_value=old), \
             mock.patch.object(INSTALL, "bindings", return_value=before), \
             mock.patch.object(INSTALL, "_check_capacity"), \
             mock.patch.object(INSTALL, "_optional_json", return_value={"items": [{"metadata": {"name": "synthetic-class"}}]}), \
             mock.patch.object(INSTALL, "_count_persisted_data", return_value=evidence), \
             mock.patch.object(INSTALL, "_stop_for_backup"), \
             mock.patch.object(INSTALL, "_identity_record", return_value={"modeling-evaluation-internal": "uid-1"}), \
             mock.patch.object(INSTALL, "_ensure_volume_snapshot", side_effect=lambda _args, name, _claim, *_rest: {"snapshotName": name + "-snapshot", "snapshotUid": name + "-snapshot-uid"}):
            record = INSTALL.prepare_auto_backup(args, self.base, {"releaseVersion": "m2"}, old)
        saved = json.loads(record.read_text())
        self.assertEqual(set(saved["claims"]), set(UPGRADE.LEGACY_EXPANDED_WORKLOADS))
        self.assertEqual(saved["dataEvidence"], evidence)
        self.assertEqual(saved["secretUids"], {"modeling-evaluation-internal": "uid-1"})

    def test_installed_workload_selection_covers_first_and_later_upgrade(self):
        legacy = {"modeling": {"workloads": {name: {"persistence": {"existingClaim": name + "-pvc"}}
                                            for name in UPGRADE.LEGACY_WORKLOADS}}}
        self.assertEqual(UPGRADE.installed_workloads(legacy), UPGRADE.LEGACY_WORKLOADS)
        expanded = {**legacy, "hub": {"persistence": {"existingClaim": "hub-pvc"}},
                    "evaluation": {"persistence": {"existingClaim": "evaluation-pvc"}}}
        self.assertEqual(UPGRADE.installed_workloads(expanded), UPGRADE.LEGACY_EXPANDED_WORKLOADS)
        current = {"modeling": {"workloads": {"onto": legacy["modeling"]["workloads"]["onto"]}},
                   "hub": expanded["hub"], "evaluation": expanded["evaluation"]}
        self.assertEqual(UPGRADE.installed_workloads(current), UPGRADE.WORKLOADS)
        with self.assertRaises(ConfigError):
            UPGRADE.installed_workloads({**legacy, "hub": expanded["hub"]})

    def test_legacy_upgrade_detects_retirement_without_extra_flag(self):
        args = SimpleNamespace(backup_record=self.base / "backup.json", transition_id=None,
                               transition_record=None, lock_holder="operation-demo",
                               standalone_upgrade=True, release="modeling")
        args.backup_record.write_text("{}")
        target = {"global": {"targetArch": "amd64", "auth": {"issuer": "synthetic"}},
                  "modeling": {"workloads": {"onto": {"persistence": {"existingClaim": "onto-data"}}}},
                  "hub": {"persistence": {"existingClaim": "hub-data"}},
                  "evaluation": {"persistence": {"existingClaim": "evaluation-data"}}}
        old = {"global": target["global"],
               "modeling": {"workloads": {name: {"persistence": {"existingClaim": name + "-data"}}
                                          for name in UPGRADE.LEGACY_WORKLOADS}}}
        source_bindings = {name: {"claimName": name + "-data"}
                           for name in UPGRADE.LEGACY_WORKLOADS}
        with mock.patch.object(INSTALL, "check_delegated_lease"), \
             mock.patch.object(INSTALL, "installed_release", return_value={
                 "releaseVersion": "old", "chartVersion": "0.1.0"}), \
             mock.patch.object(INSTALL, "installed_values", return_value=old), \
             mock.patch.object(INSTALL, "bindings", return_value=source_bindings), \
             mock.patch.object(INSTALL, "verified_snapshots"):
            result = INSTALL.upgrade_precheck(args, self.base, {"releaseVersion": "new"}, target)
        self.assertEqual(result, source_bindings)
        self.assertEqual(args.backup_workloads, UPGRADE.LEGACY_WORKLOADS)

    def test_retired_claim_cannot_be_reused_for_hub_or_evaluation(self):
        target = {"modeling": {"workloads": {"onto": {"persistence": {"existingClaim": "onto-data"}}}},
                  "hub": {"persistence": {"existingClaim": "core-data"}},
                  "evaluation": {"persistence": {"existingClaim": "evaluation-data"}}}
        source = {"core": {"claimName": "core-data"}, "agent": {"claimName": "agent-data"},
                  "onto": {"claimName": "onto-data"}}
        with self.assertRaisesRegex(ConfigError, "hub target PVC belongs to a retired"):
            UPGRADE.assert_no_retired_claim_reuse(target, source)

    def test_retired_snapshot_groups_are_never_pruned(self):
        args = SimpleNamespace(namespace="modeling-demo", release="modeling", context="synthetic",
                               snapshot_retention=1)
        snapshots = []
        for group, workloads, created in (
                ("legacy", UPGRADE.LEGACY_EXPANDED_WORKLOADS, "2026-01-01T00:00:00Z"),
                ("newer", UPGRADE.WORKLOADS, "2026-01-03T00:00:00Z"),
                ("older", UPGRADE.WORKLOADS, "2026-01-02T00:00:00Z")):
            for workload in workloads:
                snapshots.append({"metadata": {"name": f"{group}-{workload}",
                    "creationTimestamp": created, "labels": {
                        "modeling.cce/backup-group": group, "modeling.cce/workload": workload}}})
        with mock.patch.object(INSTALL, "_optional_json", return_value={"items": snapshots}), \
             mock.patch.object(INSTALL, "run_program", return_value=SimpleNamespace(returncode=0)) as delete:
            self.assertEqual(INSTALL._cleanup_old_successful_snapshots(args), [])
        names = [call.args[0][call.args[0].index("volumesnapshot") + 1]
                 for call in delete.call_args_list]
        self.assertEqual(set(names), {f"older-{name}" for name in UPGRADE.WORKLOADS})

    def test_retired_workloads_must_be_absent_after_upgrade(self):
        args = SimpleNamespace(namespace="modeling-demo", release="modeling",
                               retired_ingress_names=("modeling-core-entry",))
        with mock.patch.object(INSTALL, "_optional_json", return_value=None), \
             mock.patch.object(INSTALL, "kube", return_value={"items": []}):
            INSTALL.verify_retired_workloads(args)
        with mock.patch.object(INSTALL, "_optional_json", side_effect=[{}, None, None, None]):
            with self.assertRaisesRegex(ConfigError, "retired core deployment still exists"):
                INSTALL.verify_retired_workloads(args)
        with mock.patch.object(INSTALL, "_optional_json", side_effect=[None, None, None, None, {}]), \
             mock.patch.object(INSTALL, "kube", return_value={"items": []}):
            with self.assertRaisesRegex(ConfigError, "retired Core Ingress"):
                INSTALL.verify_retired_workloads(args)

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
        self.assertEqual(len(result["endpoints"]), 3)
        self.assertEqual(Path(result["artifacts"][0]).read_text(), "apiVersion: v1\nkind: ConfigMap\n")


if __name__ == "__main__":
    unittest.main()
