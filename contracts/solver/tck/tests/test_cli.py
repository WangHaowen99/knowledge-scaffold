from __future__ import annotations

import json

from contracts.solver.tck import __main__ as cli
from contracts.solver.tck.runner import TckReport


def test_cli_emits_only_a_generic_report(monkeypatch, capsys):
    class SyntheticRunner:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://127.0.0.1:8080"

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def run(self):
            return TckReport(
                passed=True,
                checks=("health",),
                skipped=("fault_429",),
                capability_gaps=("fault_scenario_driver_missing",),
            )

    monkeypatch.setattr(cli, "ConnectorTckRunner", SyntheticRunner)

    assert cli.main(["--base-url", "http://127.0.0.1:8080"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "passed",
        "checks": ["health"],
        "skipped": ["fault_429"],
        "capability_gaps": ["fault_scenario_driver_missing"],
    }


def test_cli_admission_fails_when_checks_are_skipped(monkeypatch, capsys):
    class SyntheticRunner:
        def __init__(self, **_kwargs):
            pass

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def run(self):
            return TckReport(
                passed=True,
                checks=("health",),
                skipped=("fault_429",),
                capability_gaps=("fault_scenario_driver_missing",),
            )

    monkeypatch.setattr(cli, "ConnectorTckRunner", SyntheticRunner)

    assert cli.main(["--base-url", "http://127.0.0.1:8080", "--admission"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "tck_admission_failed",
        "checks": ["health"],
        "skipped": ["fault_429"],
        "capability_gaps": ["fault_scenario_driver_missing"],
    }


def test_cli_admission_fails_when_report_is_not_passed(monkeypatch, capsys):
    class SyntheticRunner:
        def __init__(self, **_kwargs):
            pass

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def run(self):
            return TckReport(passed=False, checks=("health",))

    monkeypatch.setattr(cli, "ConnectorTckRunner", SyntheticRunner)

    assert cli.main(["--base-url", "http://127.0.0.1:8080", "--admission"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "tck_admission_failed",
        "checks": ["health"],
    }


def test_cli_admission_passes_only_for_a_complete_report(monkeypatch, capsys):
    class SyntheticRunner:
        def __init__(self, **_kwargs):
            pass

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def run(self):
            return TckReport(passed=True, checks=("health",))

    monkeypatch.setattr(cli, "ConnectorTckRunner", SyntheticRunner)

    assert cli.main(["--base-url", "http://127.0.0.1:8080", "--admission"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "passed",
        "checks": ["health"],
    }


def test_cli_admission_wires_external_fault_driver_and_public_verifier(monkeypatch, capsys):
    captured = {}

    class SyntheticRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def run(self):
            return TckReport(passed=True, checks=("health",))

    monkeypatch.setattr(cli, "ConnectorTckRunner", SyntheticRunner)
    assert (
        cli.main(
            [
                "--base-url",
                "http://127.0.0.1:8080",
                "--admission",
                "--fault-control-url",
                "http://127.0.0.1:9090/control",
                "--fault-control-token",
                "synthetic-control-token",
            ]
        )
        == 0
    )
    assert captured["fault_driver"].control_url.endswith("/control")
    assert captured["result_verifier"].__name__ == "verify_solver_result"


def test_cli_admission_rejects_only_one_fault_control_credential(capsys):
    assert (
        cli.main(
            [
                "--base-url",
                "http://127.0.0.1:8080",
                "--admission",
                "--fault-control-url",
                "http://127.0.0.1:9090/control",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "tck_admission_configuration_invalid",
    }
