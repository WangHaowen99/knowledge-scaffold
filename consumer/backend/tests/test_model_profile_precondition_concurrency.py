from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import Event

import pytest

from config.model_profile_references import get_model_profile_references
from config.system_config import (
    ModelProfilePreconditionError,
    get_public_system_config_snapshot,
    update_system_config_section,
)
from workspace.workspaces import (
    create_workspace,
    get_workspace_runtime_config,
    update_workspace_runtime_config,
)


def _profile(profile_id):
    return {
        "id": profile_id,
        "name": "Model {}".format(profile_id),
        "provider": "openai_compatible",
        "base_url": "https://{}.example/v1".format(profile_id),
        "model": "{}-model".format(profile_id),
        "timeout": 600,
        "max_tokens": 4096,
        "temperature": 0.7,
        "enabled": True,
    }


def _setup_state():
    update_system_config_section(
        "llm_profiles",
        {
            "profiles": [_profile("old"), _profile("stable")],
            "default_main_profile_ref": {"scope": "global", "id": "stable"},
            "default_task_profile_ref": None,
        },
    )
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)


def _delete_candidate_and_precondition():
    snapshot = get_public_system_config_snapshot()
    inventory = get_model_profile_references()
    return (
        {
            "profiles": [_profile("stable")],
            "default_main_profile_ref": {"scope": "global", "id": "stable"},
            "default_task_profile_ref": None,
        },
        {
            "config_generation": snapshot["generation"],
            "config_fingerprint": snapshot["llm_profiles_fingerprint"],
            "reference_generation": inventory["generation"],
            "removed_profile_ids": ["old"],
            "renames": [],
        },
    )


def test_workspace_reference_write_waits_for_destructive_commit_then_revalidates(
    monkeypatch,
):
    import config.system_config as system_config_module

    _setup_state()
    candidate, precondition = _delete_candidate_and_precondition()
    entered_commit = Event()
    release_commit = Event()
    original_write = system_config_module._write_config_document_unlocked

    def blocking_write(*args, **kwargs):
        entered_commit.set()
        assert release_commit.wait(timeout=5)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(
        system_config_module,
        "_write_config_document_unlocked",
        blocking_write,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        delete_future = executor.submit(
            update_system_config_section,
            "llm_profiles",
            candidate,
            precondition=precondition,
        )
        assert entered_commit.wait(timeout=5)
        workspace_future = executor.submit(
            update_workspace_runtime_config,
            "alpha",
            {"llm": {"default_main_profile_ref": "global:old"}},
        )
        with pytest.raises(FutureTimeoutError):
            workspace_future.result(timeout=0.2)
        release_commit.set()
        delete_future.result(timeout=5)
        with pytest.raises(ValueError, match="global model profile.*old.*不存在"):
            workspace_future.result(timeout=5)

    assert get_workspace_runtime_config("alpha")["llm"]["default_main_profile_ref"] is None


def test_destructive_commit_waits_for_workspace_reference_write_without_deadlock(
    monkeypatch,
):
    import workspace.workspaces as workspaces_module

    _setup_state()
    candidate, precondition = _delete_candidate_and_precondition()
    reference_validated = Event()
    release_workspace_write = Event()
    original_validate = workspaces_module._validate_global_runtime_profile_refs

    def blocking_reference_validation(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        reference_validated.set()
        assert release_workspace_write.wait(timeout=5)
        return result

    monkeypatch.setattr(
        workspaces_module,
        "_validate_global_runtime_profile_refs",
        blocking_reference_validation,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        workspace_future = executor.submit(
            update_workspace_runtime_config,
            "alpha",
            {"llm": {"default_main_profile_ref": "global:old"}},
        )
        assert reference_validated.wait(timeout=5)
        delete_future = executor.submit(
            update_system_config_section,
            "llm_profiles",
            candidate,
            precondition=precondition,
        )
        with pytest.raises(FutureTimeoutError):
            delete_future.result(timeout=0.2)
        release_workspace_write.set()
        workspace_future.result(timeout=5)
        with pytest.raises(ModelProfilePreconditionError) as error:
            delete_future.result(timeout=5)

    assert error.value.code == "MODEL_PROFILE_REFERENCES_CHANGED"
    assert get_workspace_runtime_config("alpha")["llm"]["default_main_profile_ref"] == {
        "scope": "global",
        "id": "old",
    }
