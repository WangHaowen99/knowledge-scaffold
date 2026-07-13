import pytest

from workspace.workspaces import (
    create_workspace,
    get_workspace_runtime_config,
    update_workspace_runtime_config,
)


def _create_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)


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


def test_runtime_config_read_ignores_unknown_saved_keys(tmp_path, monkeypatch):
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

    assert get_workspace_runtime_config("demo")["lang"] == {"enabled": True, "default": "en"}


def test_create_workspace_rejects_new_workspace_after_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))

    for index in range(5):
        create_workspace(
            "Workspace {}".format(index),
            workspace_id="ws{}".format(index),
            set_as_default=index == 0,
        )

    with pytest.raises(ValueError, match="工作空间数量已达上限"):
        create_workspace("Workspace 5", workspace_id="ws5")
