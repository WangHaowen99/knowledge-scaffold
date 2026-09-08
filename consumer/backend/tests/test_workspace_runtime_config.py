import ast
import inspect
import textwrap
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import workspace.workspaces as workspaces_module
from workspace.workspaces import (
    create_workspace,
    get_workspace_runtime_config,
    update_workspace_runtime_config,
)


def _create_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)


def _boolean_term_count(node):
    if isinstance(node, ast.BoolOp):
        return sum(_boolean_term_count(value) for value in node.values)
    return 1


def test_owner_update_quota_guard_stays_within_boolean_term_budget():
    source = textwrap.dedent(
        inspect.getsource(workspaces_module._enforce_workspace_owner_limit_for_update)
    )
    tree = ast.parse(source)

    assert all(
        _boolean_term_count(node.test) < 4
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
    )


def test_runtime_root_uses_explicit_project_root(tmp_path, monkeypatch):
    expected_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(expected_root))

    assert workspaces_module.get_runtime_root() == expected_root.resolve()


def test_runtime_config_rejects_unknown_top_level_key(tmp_path, monkeypatch):
    _create_workspace(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="Unknown runtime config key"):
        update_workspace_runtime_config("demo", {"unknown": True})


def test_runtime_config_rejects_invalid_lang_shape(tmp_path, monkeypatch):
    _create_workspace(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="lang.enabled must be a boolean"):
        update_workspace_runtime_config("demo", {"lang": {"enabled": "yes"}})


def test_runtime_config_merges_valid_partial_update(tmp_path, monkeypatch):
    _create_workspace(tmp_path, monkeypatch)

    updated = update_workspace_runtime_config(
        "demo",
        {
            "lang": {"enabled": True},
            "acp_suppress_script_patterns": ["故障历史", "维修记录"],
        },
    )

    assert updated["lang"] == {"enabled": True, "default": "zh"}
    assert updated["acp_suppress_script_patterns"] == ["故障历史", "维修记录"]
    assert get_workspace_runtime_config("demo") == updated


def test_runtime_config_read_ignores_unknown_saved_keys_without_rewriting(
    tmp_path,
    monkeypatch,
):
    _create_workspace(tmp_path, monkeypatch)
    update_workspace_runtime_config("demo", {"lang": {"enabled": True}})

    runtime_path = tmp_path / ".onto-runtime" / "workspaces" / "demo" / "runtime_config.json"
    runtime_path.write_text(
        """
{
  "lang": {
    "enabled": true,
    "default": "en"
  },
  "future_key": {
    "enabled": true
  }
}
""".strip(),
        encoding="utf-8",
    )

    original = runtime_path.read_bytes()

    loaded = get_workspace_runtime_config("demo")

    assert loaded["lang"] == {"enabled": True, "default": "en"}
    assert "future_key" not in loaded
    assert runtime_path.read_bytes() == original


def test_create_workspace_rejects_new_workspace_after_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setenv("WORKSPACE_MAX_COUNT", "5")

    for index in range(5):
        create_workspace(
            "Workspace {}".format(index),
            workspace_id="ws{}".format(index),
            owner="alice",
            auto_set_default=False,
        )

    with pytest.raises(ValueError, match="工作空间数量已达上限"):
        create_workspace(
            "Workspace 5",
            workspace_id="ws5",
            owner="alice",
            auto_set_default=False,
        )


def test_create_workspace_limit_is_scoped_to_each_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setenv("WORKSPACE_MAX_COUNT", "2")

    for index in range(2):
        create_workspace(
            "Alice Workspace {}".format(index),
            workspace_id="alice-ws{}".format(index),
            owner="alice",
            auto_set_default=False,
        )

    for index in range(2):
        create_workspace(
            "Bob Workspace {}".format(index),
            workspace_id="bob-ws{}".format(index),
            owner="bob",
            auto_set_default=False,
        )

    with pytest.raises(ValueError, match="工作空间数量已达上限"):
        create_workspace(
            "Alice Workspace 2",
            workspace_id="alice-ws2",
            owner="alice",
            auto_set_default=False,
        )


def test_ownerless_workspace_does_not_consume_an_owner_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setenv("WORKSPACE_MAX_COUNT", "1")

    create_workspace(
        "Alice Workspace",
        workspace_id="alice-ws",
        owner="alice",
        auto_set_default=False,
    )

    create_workspace(
        "Shared Workspace",
        workspace_id="shared-ws",
        owner=None,
        auto_set_default=False,
    )

    create_workspace(
        "Another Shared Workspace",
        workspace_id="shared-ws-2",
        owner=None,
        auto_set_default=False,
    )


def test_same_owner_update_does_not_consume_an_additional_quota_slot(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setenv("WORKSPACE_MAX_COUNT", "1")
    create_workspace(
        "Alice Workspace",
        workspace_id="alice-ws",
        owner="alice",
        auto_set_default=False,
    )
    create_workspace(
        "Legacy Over-limit Workspace",
        workspace_id="alice-legacy-over-limit",
        owner="alice",
        auto_set_default=False,
        enforce_workspace_limit=False,
    )

    updated = workspaces_module.update_workspace(
        "alice-ws",
        name="Renamed Workspace",
        owner="alice",
    )

    assert updated["owner"] == "alice"
    assert updated["name"] == "Renamed Workspace"


def test_concurrent_workspace_creation_never_exceeds_owner_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setenv("WORKSPACE_MAX_COUNT", "2")
    attempts = 8
    barrier = Barrier(attempts)

    def create(index):
        barrier.wait(timeout=5)
        try:
            create_workspace(
                "Alice Workspace {}".format(index),
                workspace_id="alice-concurrent-{}".format(index),
                owner="alice",
                auto_set_default=False,
            )
            return "created"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=attempts) as executor:
        results = list(executor.map(create, range(attempts)))

    assert results.count("created") == 2
    assert sum("工作空间数量已达上限" in result for result in results) == attempts - 2
    assert (
        sum(item.get("owner") == "alice" for item in workspaces_module.list_workspaces())
        == 2
    )
