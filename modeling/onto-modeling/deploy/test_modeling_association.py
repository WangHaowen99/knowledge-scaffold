"""Synthetic deployment tests; no Docker daemon or real credentials required."""
import os
from pathlib import Path
import shutil
import secrets
import subprocess
import tempfile
import unittest
from urllib.parse import urlsplit


class AssociationDeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="association-deploy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        binary = self.root / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text('''#!/bin/bash
case "$*" in
  *".Config.Env"*) printf 'MODELING_SERVICE_TOKEN=%s\\n' "$SYNTHETIC_TOKEN";;
  *".NetworkSettings.Ports"*) echo 8100;;
  "container inspect "*) test "$SYNTHETIC_PRESENT" = 1;;
  "info "*) echo "$SYNTHETIC_OS";;
  "network inspect "*) echo "$SYNTHETIC_GATEWAY";;
  "compose "*|"ps "*) exit 0;;
  *) exit 1;;
esac
''')
        docker.chmod(0o700)
        self.env = {
            **os.environ,
            "PATH": str(binary) + ":" + os.environ["PATH"],
            "SYNTHETIC_TOKEN": secrets.token_urlsafe(32),
            "SYNTHETIC_GATEWAY": "gateway.example.com",
            "SYNTHETIC_OS": "Docker Desktop",
            "SYNTHETIC_PRESENT": "1",
        }
        self.env_file = self.root / ".env"
        self.env_file.write_text("UNRELATED=keep\nONTO_WEB_AUTH_URL=https://auth.example.com\n")

    def run_script(self, source, *args):
        # Execute only repository-owned scripts copied into our private test
        # directory. Arguments are passed literally; no shell command is built.
        script = (self.root / source.name).resolve()
        if source.resolve() != script:
            shutil.copy2(source, script)
        script.chmod(0o700)
        return subprocess.run(  # NOSONAR(S4721): fixed repository test script and literal argv
            [str(script), *args], env=self.env, capture_output=True, text=True,
            shell=False, check=False, timeout=30,
        )

    def assert_local_association_url(self, contents, hostname):
        values = dict(line.split("=", 1) for line in contents.splitlines() if "=" in line)
        url = urlsplit(values["ONTO_WEB_MODELING_ASSOCIATION_URL"])
        # Docker's local HTTP endpoint is intentionally preserved. This test
        # only inspects generated configuration and never opens a connection.
        self.assertEqual(url.scheme, "http")
        self.assertEqual(url.hostname, hostname)
        self.assertEqual(url.port, 8100)
        self.assertEqual(url.path, "")
        self.assertEqual(url.query, "")
        self.assertEqual(url.fragment, "")
        self.assertIsNone(url.username)
        self.assertIsNone(url.password)

    def run_config(self, *args, success=True):
        script = Path(__file__).with_name("configure-modeling-association.sh")
        result = self.run_script(script, str(self.root), *args)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        if self.env["SYNTHETIC_TOKEN"]:
            self.assertNotIn(self.env["SYNTHETIC_TOKEN"], result.stdout + result.stderr)
        return self.env_file.read_text()

    def test_first_deploy_and_repeat_preserve_settings(self):
        first = self.run_config()
        self.assert_local_association_url(first, "host.docker.internal")
        self.assertEqual(first, self.run_config())
        self.assertIn("UNRELATED=keep", first)
        self.assertIn("ONTO_WEB_AUTH_URL=https://auth.example.com", first)
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)

    def test_upgrade_preserves_complete_remote_pair_without_local_docker(self):
        self.env_file.write_text("ONTO_WEB_MODELING_ASSOCIATION_URL='https://service.example.com:5100'\n"
                                 "ONTO_WEB_MODELING_ASSOCIATION_TOKEN=synthetic-remote-token\n")
        before = self.env_file.read_text()
        self.env["SYNTHETIC_PRESENT"] = "0"
        self.assertEqual(before, self.run_config("https://stack.example.com:5100"))

    def test_partial_configuration_is_rejected_without_mutation(self):
        for key in ("URL", "TOKEN"):
            self.env_file.write_text(f"ONTO_WEB_MODELING_ASSOCIATION_{key}=synthetic-value\n")
            before = self.env_file.read_text()
            self.assertEqual(before, self.run_config(success=False))

    def test_linux_gateway_and_stack_address(self):
        self.env["SYNTHETIC_OS"] = "Docker Engine"
        self.assert_local_association_url(self.run_config(), self.env["SYNTHETIC_GATEWAY"])
        self.env_file.write_text("")
        self.assertIn("https://stack.example.com:5100", self.run_config("https://stack.example.com:5100"))

    def test_legacy_address_and_missing_service(self):
        self.env_file.write_text('ONTO_WEB_MODELING_URL="https://modeling.example.com:5100"\n')
        self.assertNotIn("ONTO_WEB_MODELING_ASSOCIATION_TOKEN=", self.run_config())
        before = self.env_file.read_text()
        self.env["SYNTHETIC_PRESENT"] = "0"
        self.assertEqual(before, self.run_config())

    def test_invalid_token_does_not_overwrite_configuration(self):
        before = self.env_file.read_text()
        self.env["SYNTHETIC_TOKEN"] = ""
        self.assertEqual(before, self.run_config(success=False))

    def test_release_deploy_with_and_without_association_helper(self):
        source = Path(__file__).parent
        shutil.copy2(source / "deploy.sh", self.root / "deploy.sh")
        (self.root / "preflight.sh").write_text("#!/bin/bash\nexit 0\n")
        (self.root / "images.env").write_text("")
        for with_helper in (False, True):
            self.env_file.write_text("UNRELATED=keep\n")
            if with_helper:
                shutil.copy2(source / "configure-modeling-association.sh", self.root)
            result = self.run_script(self.root / "deploy.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual("ONTO_WEB_MODELING_ASSOCIATION_TOKEN=" in self.env_file.read_text(),
                             with_helper)
            self.assertNotIn(self.env["SYNTHETIC_TOKEN"], result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
