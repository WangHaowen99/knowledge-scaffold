"""Synthetic CCE package and cross-release contract checks."""

import importlib.util
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

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import ConfigError, checksum, safe_extract, validate_release  # noqa: E402


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


JOINT = load_script("joint-cce.py")
INSTALL = load_script("install-cce.py")
PACKAGE = load_script("package-cce.py")


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
                       platform="linux/amd64", components=list(records), images=records,
                       imageDependencies={"jiuwenbox": {"sourceSide": "consumer", "sourceImageId": "jiuwenbox"}})
        (root / "release.yaml").write_text(yaml.safe_dump(release))
        (root / "checksums.sha256").write_text("".join(
            f"{checksum(path)}  {path.relative_to(root)}\n" for path in sorted(root.rglob("*")) if path.is_file() and path.name != "checksums.sha256"))
        return root

    def test_package_validates_and_detects_tampering(self):
        root = self.package()
        self.assertEqual(validate_release(root, "modeling")["platform"], "linux/amd64")
        (root / "images/modeling.tar").write_bytes(b"changed")
        with self.assertRaises(ConfigError):
            validate_release(root, "modeling")

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
            from contextlib import redirect_stdout
            from io import StringIO
            output = StringIO()
            with redirect_stdout(output):
                code = INSTALL.main()
        finally:
            sys.argv = old
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["errorCode"], "MIGRATION_FAILED")

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
            with mock.patch.object(PACKAGE, "run", side_effect=fake_run), redirect_stdout(StringIO()):
                PACKAGE.main()
        finally:
            sys.argv = old
        with tarfile.open(output, "r:gz") as stream:
            names = stream.getnames()
            self.assertIn("modeling-cce-demo-amd64/deploy/chart/Chart.yaml", names)
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
            with mock.patch.object(PACKAGE, "run", side_effect=fake_run), redirect_stdout(StringIO()):
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
            "modeling-offline/modeling:demo-amd64", "modeling-offline/modeling-agent:demo-amd64",
            "modeling-offline/onto-modeling:demo-amd64",
        })

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
        self.assertTrue(any("modeling-offline/onto-modeling:demo-amd64" in line for line in local_calls))
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
            "modelingReleaseVersion": "demo", "modelingChartVersion": "0.1.0", "platform": "linux/amd64",
            "authApiVersion": "v1", "consumerApiVersion": "v1",
            "operationSchemaVersion": "cce-operation/v1", "endpointSchemaVersion": "cce-endpoints/v1",
        }]}))
        output_dir = self.base / "state"
        old = sys.argv
        try:
            sys.argv = ["install-cce.py", "plan", "--context", "synthetic", "--namespace", "modeling-demo",
                "--release", "modeling", "--package-root", str(root), "--values", str(values),
                "--resolved-images", str(image_map), "--compatibility", str(compatibility),
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
