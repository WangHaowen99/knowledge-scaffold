"""Dependency checks for an offline Modeling install on a shared Docker network."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).with_name("dependency_probe.py")
spec = importlib.util.spec_from_file_location("modeling_dependency_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def invoke(path, *extra):
    arguments = ["dependency_probe.py", "--image", "onto:test", "--network", "auth-net",
                 "--auth-url", "http://auth-server:5056", "--issuer", "https://issuer",
                 "--audience", "audience", "--consumer-url", "http://backend:5055",
                 "--state-file", str(path), *extra]
    with mock.patch.object(sys, "argv", arguments):
        return probe.main()


def test_missing_dependencies_wait_and_then_verify(tmp_path):
    path = tmp_path / ".dependencies.json"
    with mock.patch.object(probe, "probe", side_effect=[["auth", "consumer"], [], []]), \
         mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        assert invoke(path) == 0
        assert json.loads(path.read_text())["phase"] == "waiting-dependencies"
        assert invoke(path, "--verify") == 0
        assert json.loads(path.read_text())["phase"] == "ready"
        assert invoke(path, "--verify") == 0
        assert run.call_count == 6


def test_pending_verify_preserves_waiting_and_wrong_contract_fails(tmp_path):
    path = tmp_path / ".dependencies.json"
    with mock.patch.object(probe, "probe", return_value=["auth"]):
        assert invoke(path) == 0
        assert invoke(path, "--verify") == 1
        assert invoke(path, "--require-ready") == 2
    assert json.loads(path.read_text())["phase"] == "waiting-dependencies"
    content = json.loads(path.read_text())
    content["authIssuer"] = "https://different-issuer"
    path.write_text(json.dumps(content))
    with mock.patch.object(probe, "probe") as network_probe:
        assert invoke(path, "--verify") == 2
        network_probe.assert_not_called()


def test_healthy_dependencies_alone_do_not_claim_modeling_ready(tmp_path):
    path = tmp_path / ".dependencies.json"
    with mock.patch.object(probe, "probe", return_value=[]), \
         mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=1)):
        assert invoke(path) == 0
        assert json.loads(path.read_text())["phase"] == "verifying-service"
        assert invoke(path, "--verify") == 2
    assert json.loads(path.read_text())["phase"] == "verifying-service"


def test_existing_upgrade_check_does_not_replace_dependency_state(tmp_path):
    path = tmp_path / ".dependencies.json"
    with mock.patch.object(probe, "probe", return_value=[]):
        assert invoke(path) == 0
        original = path.read_bytes()
        assert invoke(path, "--check-only", "--require-ready") == 0
    assert path.read_bytes() == original


def test_waiting_install_rejects_image_change(tmp_path):
    path = tmp_path / ".dependencies.json"
    with mock.patch.object(probe, "probe", return_value=["auth"]):
        assert invoke(path) == 0
        original = path.read_bytes()
        with mock.patch.object(sys, "argv", ["dependency_probe.py", "--image", "onto:new",
                                            "--network", "auth-net", "--auth-url", "http://auth-server:5056",
                                            "--issuer", "https://issuer", "--audience", "audience",
                                            "--consumer-url", "http://backend:5055", "--state-file", str(path)]):
            assert probe.main() == 2
    assert path.read_bytes() == original


def test_probe_uses_existing_image_without_pulling():
    with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='["auth"]')) as run:
        assert probe.probe("onto:test", "auth-net", "http://auth", "issuer", "http://consumer") == ["auth"]
    assert run.call_args.args[0][:6] == ["docker", "run", "--rm", "--pull", "never", "--network"]
