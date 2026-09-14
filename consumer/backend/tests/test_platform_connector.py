import threading
from datetime import UTC, datetime

from backend_synthetic_credentials import synthetic_secret
from integration.observability import RuntimeEvent, RuntimeEventType, RuntimeStatus


def test_platform_event_mapper_projects_run_terminal_event_without_rich_content():
    from integration.platform.config import PlatformConfig
    from integration.platform.events import to_platform_event

    event = RuntimeEvent(
        type=RuntimeEventType.RUN_FINISHED,
        run_id="run-1",
        session_id="session-1",
        turn_id=3,
        timestamp_ms=1_781_418_000_000,
        payload={
            "status": RuntimeStatus.SUCCESS,
            "duration_ms": 1234,
            "workspace_id": "workspace-a",
            "user_id": "user-a",
            "final_answer": "this must not be forwarded",
        },
    )

    envelope = to_platform_event(
        event,
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
        ),
    )

    assert envelope == {
        "event_id": "instance-prod-01:run-1:run.finished:1781418000000",
        "event_type": "run.finished",
        "schema_version": "v1",
        "occurred_at": "2026-06-14T06:20:00Z",
        "runtime_id": "instance-prod-01",
        "organization_id": None,
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "session_id": "session-1",
        "run_id": "run-1",
        "trace_id": event.trace_id,
        "payload": {
            "status": "success",
            "duration_ms": 1234,
            "turn_count": 3,
            "tool_call_count": 0,
            "model_call_count": 0,
        },
    }


def test_platform_event_mapper_projects_tool_finished_event():
    from integration.platform.config import PlatformConfig
    from integration.platform.events import to_platform_event

    event = RuntimeEvent(
        type=RuntimeEventType.TOOL_FINISHED,
        run_id="run-1",
        session_id="session-1",
        span_key="tool:main:1:business_data_search:call-1",
        timestamp_ms=1_781_500_401_000,
        payload={
            "tool_name": "business_data_search",
            "status": RuntimeStatus.SUCCESS,
            "elapsed_ms": 55,
        },
    )

    envelope = to_platform_event(
        event,
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
        ),
        run_context={"workspace_id": "workspace-a", "user_id": "user-a"},
    )

    assert envelope["event_type"] == "tool.finished"
    assert envelope["workspace_id"] == "workspace-a"
    assert envelope["user_id"] == "user-a"
    assert envelope["payload"] == {
        "tool_name": "business_data_search",
        "tool_type": None,
        "status": "success",
        "duration_ms": 55,
        "error_code": None,
        "error_message": None,
    }


def test_platform_status_contract_normalizes_runtime_success_aliases():
    from integration.platform.contracts import normalize_platform_status

    assert normalize_platform_status(RuntimeStatus.SUCCESS) == "success"
    assert normalize_platform_status("completed") == "success"
    assert normalize_platform_status("succeeded") == "success"
    assert normalize_platform_status(None) == "success"


def test_platform_telemetry_adapter_normalizes_completed_tool_status():
    from integration.platform.config import PlatformConfig
    from integration.platform.telemetry import to_platform_telemetry_event

    event = RuntimeEvent(
        type=RuntimeEventType.TOOL_FINISHED,
        run_id="run-1",
        session_id="session-1",
        span_key="tool:main:1:ontology_context:call-1",
        timestamp_ms=1_781_500_401_000,
        payload={
            "tool_name": "ontology_context",
            "status": "completed",
            "elapsed_ms": 42,
        },
    )

    envelope = to_platform_telemetry_event(
        event,
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
        ),
    )

    assert envelope is not None
    assert envelope["event_type"] == "tool.finished"
    assert envelope["payload"]["status"] == "success"


def test_platform_event_mapper_projects_model_usage_event():
    from integration.platform.config import PlatformConfig
    from integration.platform.events import to_platform_event

    event = RuntimeEvent(
        type=RuntimeEventType.LLM_FINISHED,
        run_id="run-1",
        session_id="session-1",
        span_key="llm:main:1",
        timestamp_ms=1_781_500_402_000,
        payload={
            "status": RuntimeStatus.SUCCESS,
            "model": "gpt-test",
            "model_provider": "openai_compatible",
            "request_label": "main",
            "duration_ms": 200,
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            "prompt_text": "do not forward prompt",
            "completion_text": "do not forward answer",
        },
    )

    envelope = to_platform_event(
        event,
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
        ),
        run_context={"workspace_id": "workspace-a", "user_id": "user-a"},
    )

    assert envelope["event_type"] == "model.usage"
    assert envelope["payload"] == {
        "provider": "openai_compatible",
        "model_name": "gpt-test",
        "profile_name": "main",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "duration_ms": 200,
        "status": "success",
        "error_code": None,
        "error_message": None,
    }


def test_platform_event_mapper_ignores_unsupported_event_type():
    from integration.platform.config import PlatformConfig
    from integration.platform.events import to_platform_event

    envelope = to_platform_event(
        RuntimeEvent(
            type=RuntimeEventType.AGENT_ITERATION_PREPARED,
            run_id="run-1",
            timestamp_ms=1_781_500_402_000,
            payload={},
        ),
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
        ),
    )

    assert envelope is None


def test_platform_event_mapper_builds_ontology_snapshot_event(tmp_path):
    from integration.platform.config import PlatformConfig
    from integration.platform.events import build_ontology_snapshot_event

    ontology_root = tmp_path / "ontologies"
    ontology_dir = ontology_root / "sales"
    version_dir = ontology_dir / "versions" / "v2"
    version_dir.mkdir(parents=True)
    (version_dir / "index.md").write_text("# Sales Ontology\n", encoding="utf-8")
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: sales",
                "name: Sales Ontology",
                "description: Sales domain ontology",
                "version: v2",
                "active_version: v2",
                "versions:",
                "  - v1",
                "  - v2",
                "language: zh",
                "enabled: true",
                "scope:",
                "  - sales",
            ]
        ),
        encoding="utf-8",
    )

    envelope = build_ontology_snapshot_event(
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
        ),
        workspace_id="workspace-a",
        ontology_root=ontology_root,
        occurred_at=datetime(2026, 6, 14, 6, 20, tzinfo=UTC),
    )

    assert envelope == {
        "event_id": "instance-prod-01:asset.snapshot:ontology:workspace-a:1781418000000",
        "event_type": "asset.snapshot",
        "schema_version": "v1",
        "occurred_at": "2026-06-14T06:20:00Z",
        "runtime_id": "instance-prod-01",
        "organization_id": None,
        "workspace_id": "workspace-a",
        "user_id": None,
        "session_id": None,
        "run_id": None,
        "trace_id": None,
        "payload": {
            "asset_type": "ontology",
            "snapshot_mode": "full",
            "workspace": {
                "id": "workspace-a",
                "name": "workspace-a",
                "status": "active",
            },
            "assets": [
                {
                    "id": "sales",
                    "name": "Sales Ontology",
                    "version": "v2",
                    "active_version": "v2",
                    "versions": ["v1", "v2"],
                    "status": "active",
                    "metadata": {
                        "description": "Sales domain ontology",
                        "language": "zh",
                        "scope": ["sales"],
                    },
                }
            ],
        },
    }


def test_platform_exporter_batches_events_with_environment_auth_headers():
    from integration.platform.config import PlatformConfig
    from integration.platform.exporter import PlatformExporter

    calls = []

    def transport(url, payload, headers, timeout):
        calls.append((url, payload, headers, timeout))
        return {"accepted": len(payload["events"])}

    exporter = PlatformExporter(
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net/",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
            batch_size=2,
            timeout_seconds=3.5,
        ),
        transport=transport,
    )

    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_STARTED,
            run_id="run-1",
            session_id="session-1",
            timestamp_ms=1_781_500_399_000,
            payload={
                "workspace_id": "workspace-a",
                "user_id": "user-a",
                "external_trace_id": "a" * 32,
            },
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.TOOL_FINISHED,
            run_id="run-1",
            session_id="session-1",
            span_key="tool:main:1:tool-a:call-1",
            timestamp_ms=1_781_500_401_000,
            payload={"tool_name": "tool-a", "status": "success"},
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_FINISHED,
            run_id="run-1",
            session_id="session-1",
            timestamp_ms=1_781_500_402_000,
            payload={"status": "success"},
        )
    )

    assert len(calls) == 1
    url, payload, headers, timeout = calls[0]
    assert url == "https://platform.example.net/api/v1/events/ingest"
    assert headers == {
        "Content-Type": "application/json",
        "X-Environment-Id": "prod",
        "X-Environment-Api-Key": synthetic_secret("platform_connector-credential-02"),
    }
    assert timeout == 3.5
    assert [event["event_type"] for event in payload["events"]] == [
        "tool.finished",
        "run.finished",
    ]
    assert payload["events"][0]["workspace_id"] == "workspace-a"
    assert all(event["trace_id"] == "a" * 32 for event in payload["events"])


def test_platform_exporter_sends_ontology_snapshot_with_environment_auth_headers(tmp_path):
    from integration.platform.config import PlatformConfig
    from integration.platform.exporter import PlatformExporter

    ontology_root = tmp_path / "ontologies"
    ontology_dir = ontology_root / "sales"
    version_dir = ontology_dir / "versions" / "v1"
    version_dir.mkdir(parents=True)
    (version_dir / "index.md").write_text("# Sales Ontology\n", encoding="utf-8")
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: sales",
                "name: Sales Ontology",
                "description: Sales domain ontology",
                "version: v1",
                "active_version: v1",
                "language: zh",
            ]
        ),
        encoding="utf-8",
    )
    calls = []

    def transport(url, payload, headers, timeout):
        calls.append((url, payload, headers, timeout))
        return {"accepted": len(payload["events"])}

    exporter = PlatformExporter(
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net/",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
            timeout_seconds=3.5,
        ),
        transport=transport,
    )

    exporter.export_ontology_snapshot(
        workspace_id="workspace-a",
        ontology_root=ontology_root,
        occurred_at=datetime(2026, 6, 14, 6, 20, tzinfo=UTC),
    )

    assert len(calls) == 1
    url, payload, headers, timeout = calls[0]
    assert url == "https://platform.example.net/api/v1/events/ingest"
    assert headers == {
        "Content-Type": "application/json",
        "X-Environment-Id": "prod",
        "X-Environment-Api-Key": synthetic_secret("platform_connector-credential-02"),
    }
    assert timeout == 3.5
    assert payload["events"][0]["event_type"] == "asset.snapshot"
    assert payload["events"][0]["workspace_id"] == "workspace-a"


def test_platform_exporter_sends_ontology_snapshot_for_each_workspace(monkeypatch, tmp_path):
    from integration.platform.config import PlatformConfig
    from integration.platform.exporter import PlatformExporter
    from workspace.workspaces import create_workspace

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Alpha Workspace", workspace_id="alpha", set_as_default=True)
    create_workspace("Beta Workspace", workspace_id="beta")
    calls = []

    def transport(url, payload, headers, timeout):
        del url, headers, timeout
        calls.append(payload)
        return {"accepted": len(payload["events"])}

    exporter = PlatformExporter(
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net/",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
        ),
        transport=transport,
    )

    exporter.export_ontology_snapshots(
        occurred_at=datetime(2026, 6, 14, 6, 20, tzinfo=UTC),
    )

    events = [payload["events"][0] for payload in calls]
    assert [event["workspace_id"] for event in events] == ["alpha", "beta"]
    assert [event["payload"]["workspace"]["name"] for event in events] == [
        "Alpha Workspace",
        "Beta Workspace",
    ]
    assert all(event["event_type"] == "asset.snapshot" for event in events)
    assert all(event["payload"]["assets"] == [] for event in events)


def test_platform_exporter_background_heartbeat_sends_immediately_and_repeats():
    from integration.platform.config import PlatformConfig
    from integration.platform.exporter import PlatformExporter

    calls = []
    repeated = threading.Event()

    def transport(url, payload, headers, timeout):
        calls.append((url, payload, headers, timeout))
        if len(calls) >= 2:
            repeated.set()
        return {"accepted": len(payload["events"])}

    exporter = PlatformExporter(
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
            heartbeat_interval_seconds=0.01,
        ),
        transport=transport,
        start_heartbeat=True,
    )

    try:
        assert repeated.wait(timeout=1)
    finally:
        exporter.close()

    assert len(calls) >= 2
    assert calls[0][1]["events"][0]["event_type"] == "runtime.heartbeat"
    assert calls[0][1]["events"][0]["runtime_id"] == "instance-prod-01"
    assert calls[0][1]["events"][0]["payload"]["status"] == "online"
    assert calls[0][1]["events"][0]["payload"]["environment"] == "prod"
    assert calls[1][1]["events"][0]["event_type"] == "runtime.heartbeat"


def test_platform_exporter_background_heartbeat_retries_after_failure():
    from integration.platform.config import PlatformConfig
    from integration.platform.exporter import PlatformExporter

    calls = []
    retried = threading.Event()

    def transport(url, payload, headers, timeout):
        del headers, timeout
        calls.append((url, payload))
        if len(calls) == 1:
            raise RuntimeError("platform unavailable")
        retried.set()
        return {"accepted": 1}

    exporter = PlatformExporter(
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
            heartbeat_interval_seconds=0.01,
        ),
        transport=transport,
        start_heartbeat=True,
    )

    try:
        assert retried.wait(timeout=1)
    finally:
        exporter.close()

    assert [payload["events"][0]["event_type"] for _url, payload in calls[:2]] == [
        "runtime.heartbeat",
        "runtime.heartbeat",
    ]


def test_platform_observer_starts_when_connector_is_enabled(monkeypatch):
    import integration.platform.manager as manager_module

    calls = []

    class EnabledPlatformConfig:
        enabled = True
        available = True

    class FakePlatformExporter:
        available = True

        def __init__(self, config, *, start_heartbeat=False):
            self.config = config
            calls.append(("exporter", start_heartbeat))

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, event):
            calls.append(("event", event.run_id))

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def flush(self):
            calls.append("flush")

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            calls.append("close")

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export_ontology_snapshots(self):
            calls.append("snapshots")

    monkeypatch.setattr(
        manager_module.PlatformConfig,
        "from_env",
        classmethod(lambda cls: EnabledPlatformConfig()),
    )
    monkeypatch.setattr(manager_module, "PlatformExporter", FakePlatformExporter)
    manager_module.shutdown_platform_observer(timeout=1)

    observer = manager_module.start_platform_observer_if_enabled()
    try:
        assert observer is not None
        assert calls[:2] == [("exporter", True), "snapshots"]
    finally:
        manager_module.shutdown_platform_observer(timeout=1)

    assert "close" in calls


def test_platform_exporter_retries_buffer_after_batch_failure():
    from integration.platform.config import PlatformConfig
    from integration.platform.exporter import PlatformExporter

    calls = []
    fail_next_batch = True

    def transport(url, payload, headers, timeout):
        nonlocal fail_next_batch
        del url, headers, timeout
        calls.append(payload)
        if fail_next_batch:
            fail_next_batch = False
            raise RuntimeError("temporary failure")
        return {"accepted": len(payload["events"])}

    exporter = PlatformExporter(
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
            batch_size=1,
        ),
        transport=transport,
    )

    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_FINISHED,
            run_id="run-1",
            timestamp_ms=1_781_418_000_000,
            payload={"status": "success"},
        )
    )
    exporter.flush()

    assert [payload["events"][0]["event_type"] for payload in calls] == [
        "run.finished",
        "run.finished",
    ]


def test_platform_config_generates_and_reuses_local_instance_id(monkeypatch, tmp_path):
    from integration.platform.config import PlatformConfig

    instance_id_file = tmp_path / "platform" / "instance_id"
    monkeypatch.setenv("ONTO_PLATFORM_CONFIG_PATH", str(tmp_path / "platform_config.json"))
    monkeypatch.setenv("ONTO_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("ONTO_PLATFORM_BASE_URL", "https://platform.example.net")
    monkeypatch.setenv("ONTO_PLATFORM_ENVIRONMENT_ID", "prod")
    monkeypatch.setenv("ONTO_PLATFORM_API_KEY", synthetic_secret("platform_connector-credential-02"))
    monkeypatch.setenv("ONTO_PLATFORM_INSTANCE_ID_FILE", str(instance_id_file))

    first = PlatformConfig.from_env()
    second = PlatformConfig.from_env()

    assert first.available is True
    assert first.environment_id == "prod"
    assert first.instance_id.startswith("inst_")
    assert second.instance_id == first.instance_id
    assert instance_id_file.read_text(encoding="utf-8") == first.instance_id


def test_platform_config_reads_saved_file_before_environment_and_masks_secret(monkeypatch, tmp_path):
    from integration.platform.config import (
        PlatformConfig,
        get_public_platform_config,
        save_platform_config,
    )

    config_path = tmp_path / "config" / "platform_config.json"
    monkeypatch.setenv("ONTO_PLATFORM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("ONTO_PLATFORM_ENABLED", "false")
    monkeypatch.setenv("ONTO_PLATFORM_BASE_URL", "https://env-platform.example.net")
    monkeypatch.setenv("ONTO_PLATFORM_ENVIRONMENT_ID", "env-prod")
    monkeypatch.setenv("ONTO_PLATFORM_API_KEY", "env-secret")
    monkeypatch.setenv("ONTO_PLATFORM_INSTANCE_ID", "env-instance")

    public_config = save_platform_config(
        {
            "enabled": True,
            "base_url": "https://saved-platform.example.net",
            "environment_id": "saved-prod",
            "instance_id": "saved-instance",
            "api_key": synthetic_secret("platform_connector-credential-01"),
            "batch_size": 7,
            "timeout_seconds": 2.5,
            "heartbeat_interval_seconds": 15,
        }
    )
    resolved = PlatformConfig.from_env()
    fetched_public_config = get_public_platform_config()

    assert resolved.enabled is True
    assert resolved.base_url == "https://saved-platform.example.net"
    assert resolved.environment_id == "saved-prod"
    assert resolved.instance_id == "saved-instance"
    assert resolved.api_key == synthetic_secret("platform_connector-credential-01")
    assert resolved.batch_size == 7
    assert resolved.timeout_seconds == 2.5
    assert resolved.heartbeat_interval_seconds == 15
    assert public_config["has_api_key"] is True
    assert fetched_public_config["has_api_key"] is True
    assert "api_key" not in public_config
    assert "api_key" not in fetched_public_config
    assert synthetic_secret("platform_connector-credential-01") not in config_path.read_text(encoding="utf-8")


def test_platform_policy_maps_snapshot_to_agent_ops_config_fields():
    from integration.platform.policy import map_policy_snapshot_to_agent_config

    mapped = map_policy_snapshot_to_agent_config(
        {
            "snapshot_key": "policy-runtime-prod-01-v1",
            "version": 1,
            "checksum": "sha256:test",
            "limits": {
                "run_tps": 50,
                "workspace_max_count": 5,
                "ontology_max_count_per_workspace": 20,
                "ontology_object_max_count": 200,
                "ontology_operation_max_count": 100,
                "entity_attribute_max_count": 100,
                "data_connection_configs_max_per_workspace": 8,
                "model_profile_max_count": 10,
                "model_output_token_limit": 2048,
                "session_max_per_user_workspace": 200,
                "operation_max_chars": 15000,
                "agent_log_retention_days": 30,
                "agent_log_compress_old": True,
            },
        }
    )

    assert mapped == {
        "snapshot_key": "policy-runtime-prod-01-v1",
        "version": 1,
        "checksum": "sha256:test",
        "limits": {
            "run_tps": 50,
            "workspace_max_count": 5,
            "ontology_max_count_per_workspace": 20,
            "ontology_object_max_count": 200,
            "ontology_operation_max_count": 100,
            "entity_attribute_max_count": 100,
            "data_connection_configs_max_per_workspace": 8,
            "model_profile_max_count": 10,
            "model_output_token_limit": 2048,
            "session_max_per_user_workspace": 200,
            "operation_max_chars": 15000,
            "agent_log_retention_days": 30,
            "agent_log_compress_old": True,
        },
    }


def test_platform_policy_client_fetches_snapshot_with_environment_auth_headers():
    from integration.platform.config import PlatformConfig
    from integration.platform.policy import fetch_policy_snapshot

    calls = []

    def transport(url, headers, timeout):
        calls.append((url, headers, timeout))
        return {
            "snapshot_key": "policy-runtime-prod-01-v1",
            "version": 1,
            "checksum": "sha256:test",
            "limits": {},
        }

    snapshot = fetch_policy_snapshot(
        PlatformConfig(
            enabled=True,
            base_url="https://platform.example.net/",
            environment_id="prod",
            instance_id="instance-prod-01",
            api_key=synthetic_secret("platform_connector-credential-02"),
            timeout_seconds=2.0,
        ),
        transport=transport,
    )

    assert snapshot["snapshot_key"] == "policy-runtime-prod-01-v1"
    assert calls == [
        (
            "https://platform.example.net/api/v1/runtime/policy-snapshot?runtime_id=instance-prod-01",
            {
                "Accept": "application/json",
                "X-Environment-Id": "prod",
                "X-Environment-Api-Key": synthetic_secret("platform_connector-credential-02"),
            },
            2.0,
        )
    ]
