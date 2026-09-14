# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from internal_access import internal
import json
import os
import stat
import subprocess
import sys
import textwrap
from operator import getitem
from pathlib import Path
from types import SimpleNamespace

import pytest
from backend_synthetic_credentials import synthetic_secret


class _CatalogSnapshot:
    def __init__(self, *, generation, workspaces):
        self.generation = generation
        self.ordered_ids = tuple(workspace["id"] for workspace in workspaces)
        self._items = {workspace["id"]: dict(workspace) for workspace in workspaces}

    def copy_item(self, workspace_id):
        return dict(self._items[workspace_id])


def test_inventory_reads_one_snapshot_and_one_runtime_config_per_workspace(monkeypatch):
    from config import model_profile_references as references

    snapshot = _CatalogSnapshot(
        generation=7,
        workspaces=[
            {"id": "beta", "name": "Beta"},
            {"id": "alpha", "name": "Alpha"},
        ],
    )
    catalog_calls = []
    runtime_calls = []
    generation_calls = []
    runtime_by_workspace = {
        "alpha": {
            "llm": {
                "default_main_profile_ref": {"scope": "workspace", "id": "private"},
                "default_task_profile_ref": "shared",
            },
            "api_key": synthetic_secret("model_profile_references-credential-01"),
        },
        "beta": {
            "llm": {
                "default_main_profile_ref": "global:shared",
                "default_task_profile_ref": None,
            }
        },
    }

    def load_catalog():
        catalog_calls.append(True)
        return snapshot

    def load_runtime(workspace_id):
        runtime_calls.append(workspace_id)
        return runtime_by_workspace[workspace_id]

    def load_generation(workspace_id):
        generation_calls.append(workspace_id)
        return {"alpha": 2, "beta": 4}[workspace_id]

    monkeypatch.setattr(references, "get_workspace_catalog_snapshot", load_catalog)
    monkeypatch.setattr(references, "read_workspace_runtime_config", load_runtime)
    monkeypatch.setattr(references, "get_workspace_config_generation", load_generation)

    result = references.get_model_profile_references()

    assert catalog_calls == [True]
    assert runtime_calls == ["alpha", "beta"]
    assert generation_calls == ["alpha", "beta"]
    assert result == {
        "complete": True,
        "entries": [
            {
                "workspace_id": "alpha",
                "workspace_name": "Alpha",
                "config_generation": 2,
                "references": [
                    {
                        "field": "default_main_profile_ref",
                        "scope": "workspace",
                        "profile_id": "private",
                    },
                    {
                        "field": "default_task_profile_ref",
                        "scope": "global",
                        "profile_id": "shared",
                    },
                ],
            },
            {
                "workspace_id": "beta",
                "workspace_name": "Beta",
                "config_generation": 4,
                "references": [
                    {
                        "field": "default_main_profile_ref",
                        "scope": "global",
                        "profile_id": "shared",
                    }
                ],
            },
        ],
        "generation": result["generation"],
    }
    assert isinstance(result["generation"], str)
    assert result["generation"].startswith("sha256:")
    assert synthetic_secret("model_profile_references-credential-01") not in repr(result)


def test_inventory_fails_closed_and_preserves_discovered_entries(monkeypatch):
    from config import model_profile_references as references

    snapshot = _CatalogSnapshot(
        generation=9,
        workspaces=[
            {"id": "alpha", "name": "Alpha"},
            {"id": "broken", "name": "Broken"},
        ],
    )

    def load_runtime(workspace_id):
        if workspace_id == "broken":
            raise ValueError("invalid token containing customer-secret")
        return {
            "llm": {
                "default_main_profile_ref": "global:shared",
                "default_task_profile_ref": None,
            }
        }

    monkeypatch.setattr(references, "get_workspace_catalog_snapshot", lambda: snapshot)
    monkeypatch.setattr(references, "read_workspace_runtime_config", load_runtime)
    monkeypatch.setattr(references, "get_workspace_config_generation", lambda _workspace_id: 3)

    result = references.get_model_profile_references()

    assert result["complete"] is False
    assert result["entries"][0]["workspace_id"] == "alpha"
    assert result["entries"][0]["references"][0]["profile_id"] == "shared"
    assert result["entries"][1] == {
        "workspace_id": "broken",
        "workspace_name": "Broken",
        "config_generation": 3,
        "references": [],
        "diagnostic": {
            "code": "runtime_config_invalid",
            "message": "Runtime model references could not be verified.",
        },
    }
    assert "customer-secret" not in repr(result)


def test_inventory_preserves_valid_ref_when_sibling_ref_is_invalid(monkeypatch):
    from config import model_profile_references as references

    snapshot = _CatalogSnapshot(
        generation=10,
        workspaces=[{"id": "alpha", "name": "Alpha"}],
    )
    monkeypatch.setattr(references, "get_workspace_catalog_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        references,
        "read_workspace_runtime_config",
        lambda _workspace_id: {
            "llm": {
                "default_main_profile_ref": "global:shared",
                "default_task_profile_ref": {"scope": "invalid", "id": "secret-id"},
            }
        },
    )
    monkeypatch.setattr(references, "get_workspace_config_generation", lambda _workspace_id: 1)

    result = references.get_model_profile_references()

    assert result["complete"] is False
    assert result["entries"][0]["references"] == [
        {
            "field": "default_main_profile_ref",
            "scope": "global",
            "profile_id": "shared",
        }
    ]
    assert result["entries"][0]["diagnostic"]["code"] == "runtime_config_invalid"
    assert "secret-id" not in repr(result["entries"][0]["diagnostic"])


def test_inventory_filters_by_canonical_profile_ref_without_changing_generation(monkeypatch):
    from config import model_profile_references as references

    snapshot = _CatalogSnapshot(
        generation=11,
        workspaces=[
            {"id": "alpha", "name": "Alpha"},
            {"id": "beta", "name": "Beta"},
        ],
    )
    runtime_by_workspace = {
        "alpha": {
            "llm": {
                "default_main_profile_ref": "target",
                "default_task_profile_ref": "global:other",
            }
        },
        "beta": {
            "llm": {
                "default_main_profile_ref": "workspace:target",
                "default_task_profile_ref": None,
            }
        },
    }
    monkeypatch.setattr(references, "get_workspace_catalog_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        references,
        "read_workspace_runtime_config",
        lambda workspace_id: getitem(runtime_by_workspace, workspace_id),
    )
    monkeypatch.setattr(references, "get_workspace_config_generation", lambda _workspace_id: 0)

    full = references.get_model_profile_references()
    filtered = references.get_model_profile_references("global:target")

    assert filtered["complete"] is True
    assert filtered["generation"] == full["generation"]
    assert filtered["entries"] == [
        {
            "workspace_id": "alpha",
            "workspace_name": "Alpha",
            "config_generation": 0,
            "references": [
                {
                    "field": "default_main_profile_ref",
                    "scope": "global",
                    "profile_id": "target",
                }
            ],
        }
    ]


def test_runtime_config_reader_distinguishes_optional_config_from_missing_workspace(
    tmp_path,
    monkeypatch,
):
    from config import model_profile_references as references

    workspace_dir = tmp_path / "alpha"
    workspace_dir.mkdir()
    monkeypatch.setattr(
        references,
        "get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(workspace_dir=workspace_dir),
    )

    assert references.read_workspace_runtime_config("alpha") == {}

    (workspace_dir / "runtime_config.json").write_text(
        json.dumps({"llm": {"default_profile_ref": "legacy-main"}}),
        encoding="utf-8",
    )
    loaded = references.read_workspace_runtime_config("alpha")
    assert loaded["llm"]["default_profile_ref"] == "legacy-main"

    (workspace_dir / "runtime_config.json").unlink()
    workspace_dir.rmdir()
    with pytest.raises(FileNotFoundError):
        references.read_workspace_runtime_config("alpha")


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": True},
        {"lang": {"enabled": "yes"}},
        {"acp_suppress_script_patterns": 7},
        {"llm": {"unexpected": "value"}},
        {"llm": {"default_main_profile_ref": ["global:target"]}},
        {"llm": {"default_task_profile_ref": True}},
        {"llm": {"default_main_profile_ref": 7}},
        {"llm": {"default_main_profile_ref": {"scope": ["global"], "id": "target"}}},
        {"llm": {"default_main_profile_ref": {"scope": "global", "id": 7}}},
        {"llm": {"default_main_profile_ref": {"scope": "global", "id": True}}},
        {
            "llm": {
                "default_main_profile_ref": {
                    "scope": "global",
                    "id": {"private": synthetic_secret("model_profile_references-credential-01")},
                }
            }
        },
        {
            "llm": {
                "default_main_profile_ref": {
                    "scope": "global",
                    "id": "target",
                    "private": synthetic_secret("model_profile_references-credential-01"),
                }
            }
        },
    ],
    ids=[
        "unknown-top-level",
        "invalid-lang",
        "invalid-acp-patterns",
        "unknown-llm-field",
        "list-reference",
        "boolean-reference",
        "numeric-reference",
        "list-scope",
        "numeric-id",
        "boolean-id",
        "mapping-id",
        "extra-reference-field",
    ],
)
def test_runtime_config_reader_rejects_malformed_document(
    tmp_path,
    monkeypatch,
    payload,
):
    from config import model_profile_references as references

    workspace_dir = tmp_path / "alpha"
    workspace_dir.mkdir()
    (workspace_dir / "runtime_config.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        references,
        "get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(workspace_dir=workspace_dir),
    )

    with pytest.raises(ValueError):
        references.read_workspace_runtime_config("alpha")


def test_runtime_config_reader_rejects_dangling_symlink(
    tmp_path,
    monkeypatch,
):
    from config import model_profile_references as references

    workspace_dir = tmp_path / "alpha"
    workspace_dir.mkdir()
    config_path = workspace_dir / "runtime_config.json"
    try:
        config_path.symlink_to(workspace_dir / "missing-runtime-config.json")
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setattr(
        references,
        "get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(workspace_dir=workspace_dir),
    )

    assert config_path.is_symlink()
    assert not config_path.exists()
    with pytest.raises(ValueError, match="symlink"):
        references.read_workspace_runtime_config("alpha")


def test_runtime_config_reader_checks_link_metadata_before_treating_file_as_missing(
    tmp_path,
    monkeypatch,
):
    from config import model_profile_references as references

    workspace_dir = tmp_path / "alpha"
    workspace_dir.mkdir()
    config_path = workspace_dir / "runtime_config.json"
    original_lstat = Path.lstat

    def fake_lstat(path):
        if path == config_path:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(
        references,
        "get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(workspace_dir=workspace_dir),
    )

    with pytest.raises(ValueError, match="symlink"):
        references.read_workspace_runtime_config("alpha")


def test_runtime_config_reader_rejects_file_replaced_between_metadata_and_open(
    tmp_path,
    monkeypatch,
):
    from config import model_profile_references as references

    workspace_dir = tmp_path / "alpha"
    workspace_dir.mkdir()
    config_path = workspace_dir / "runtime_config.json"
    replacement_path = workspace_dir / "replacement.json"
    config_path.write_text(json.dumps({"llm": {}}), encoding="utf-8")
    replacement_path.write_text(
        json.dumps({"llm": {"default_main_profile_ref": "global:target"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        references,
        "get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(workspace_dir=workspace_dir),
    )
    original_secure_open = getattr(
        references,
        "_open_runtime_config_descriptor",
        None,
    )
    swapped = False

    def swapping_secure_open(path):
        nonlocal swapped
        if Path(path) == config_path and not swapped:
            config_path.unlink()
            replacement_path.replace(config_path)
            swapped = True
        if original_secure_open is None:
            return os.open(
                path,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
        return original_secure_open(path)

    monkeypatch.setattr(
        references,
        "_open_runtime_config_descriptor",
        swapping_secure_open,
        raising=False,
    )

    with pytest.raises(ValueError, match="changed while being read"):
        references.read_workspace_runtime_config("alpha")
    assert swapped is True


def test_runtime_config_reader_requests_nonblocking_nofollow_open(
    tmp_path,
    monkeypatch,
):
    from config import model_profile_references as references

    workspace_dir = tmp_path / "alpha"
    workspace_dir.mkdir()
    config_path = workspace_dir / "runtime_config.json"
    config_path.write_text(json.dumps({"llm": {}}), encoding="utf-8")
    monkeypatch.setattr(
        references,
        "get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(workspace_dir=workspace_dir),
    )
    synthetic_nonblock = 1 << 28
    synthetic_nofollow = 1 << 29
    monkeypatch.setattr(os, "O_NONBLOCK", synthetic_nonblock, raising=False)
    monkeypatch.setattr(os, "O_NOFOLLOW", synthetic_nofollow, raising=False)
    original_open = os.open
    captured_flags = []

    def capturing_open(path, flags, *args, **kwargs):
        captured_flags.append(flags)
        portable_flags = flags & ~synthetic_nonblock & ~synthetic_nofollow
        return original_open(path, portable_flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", capturing_open)

    assert references.read_workspace_runtime_config("alpha") == {"llm": {}}
    assert len(captured_flags) == 1
    assert captured_flags[0] & synthetic_nonblock
    assert captured_flags[0] & synthetic_nofollow


def test_secure_open_uses_windows_no_follow_fallback_without_posix_flags(
    tmp_path,
    monkeypatch,
):
    from config import model_profile_references as references

    config_path = tmp_path / "runtime_config.json"
    calls = []
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(references, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(
        references,
        "_open_windows_runtime_config_descriptor",
        lambda path: calls.append(path) or 41,
        raising=False,
    )

    def fail_blocking_open(*_args, **_kwargs):
        raise AssertionError("blocking os.open fallback must not be used")

    monkeypatch.setattr(os, "open", fail_blocking_open)

    descriptor = internal(references).open_runtime_config_descriptor(config_path)

    assert descriptor == 41
    assert calls == [config_path]


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "mkfifo"),
    reason="FIFO replacement test requires POSIX mkfifo",
)
def test_runtime_config_fifo_replacement_is_rejected_without_blocking(tmp_path):
    workspace_dir = tmp_path / "alpha"
    workspace_dir.mkdir()
    script = textwrap.dedent(
        """
        import json
        import os
        import sys
        from pathlib import Path
        from types import SimpleNamespace

        from config import model_profile_references as references

        workspace_dir = Path(sys.argv[1])
        config_path = workspace_dir / "runtime_config.json"
        config_path.write_text(json.dumps({"llm": {}}), encoding="utf-8")
        references.get_workspace_paths = lambda _workspace_id: SimpleNamespace(
            workspace_dir=workspace_dir
        )
        original_open = references.os.open
        swapped = False

        def swapping_open(path, flags, *args, **kwargs):
            global swapped
            if Path(path) == config_path and not swapped:
                config_path.unlink()
                os.mkfifo(config_path)
                swapped = True
            return original_open(path, flags, *args, **kwargs)

        references.os.open = swapping_open
        try:
            references.read_workspace_runtime_config("alpha")
        except Exception:
            raise SystemExit(0)
        raise SystemExit(2)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script, str(workspace_dir)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert completed.returncode == 0


def test_inventory_fails_closed_when_runtime_document_is_malformed(
    tmp_path,
    monkeypatch,
):
    from config import model_profile_references as references

    workspace_dir = tmp_path / "alpha"
    workspace_dir.mkdir()
    (workspace_dir / "runtime_config.json").write_text(
        json.dumps({"llm": {"default_main_profile_ref": ["global:target"]}}),
        encoding="utf-8",
    )
    snapshot = _CatalogSnapshot(
        generation=1,
        workspaces=[{"id": "alpha", "name": "Alpha"}],
    )
    monkeypatch.setattr(references, "get_workspace_catalog_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        references,
        "get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(workspace_dir=workspace_dir),
    )
    monkeypatch.setattr(
        references,
        "get_workspace_config_generation",
        lambda _workspace_id: 0,
    )

    result = references.get_model_profile_references("global:target")

    assert result["complete"] is False
    assert result["entries"] == [
        {
            "workspace_id": "alpha",
            "workspace_name": "Alpha",
            "config_generation": 0,
            "references": [],
            "diagnostic": {
                "code": "runtime_config_invalid",
                "message": "Runtime model references could not be verified.",
            },
        }
    ]


def test_inventory_does_not_echo_invalid_nested_reference_content(
    tmp_path,
    monkeypatch,
):
    from config import model_profile_references as references

    workspace_dir = tmp_path / "alpha"
    workspace_dir.mkdir()
    (workspace_dir / "runtime_config.json").write_text(
        json.dumps(
            {
                "llm": {
                    "default_main_profile_ref": {
                        "scope": "global",
                        "id": {"private": synthetic_secret("model_profile_references-credential-01")},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    snapshot = _CatalogSnapshot(
        generation=1,
        workspaces=[{"id": "alpha", "name": "Alpha"}],
    )
    monkeypatch.setattr(references, "get_workspace_catalog_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        references,
        "get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(workspace_dir=workspace_dir),
    )
    monkeypatch.setattr(
        references,
        "get_workspace_config_generation",
        lambda _workspace_id: 0,
    )

    result = references.get_model_profile_references()

    assert result["complete"] is False
    assert result["entries"][0]["references"] == []
    assert synthetic_secret("model_profile_references-credential-01") not in repr(result)


@pytest.mark.parametrize(
    "profile_id",
    ["   ", "x" * 81, "global:", "workspace:bad:id"],
)
def test_inventory_supports_legacy_main_ref_and_rejects_invalid_filter(
    monkeypatch,
    profile_id,
):
    from config import model_profile_references as references

    snapshot = _CatalogSnapshot(
        generation=1,
        workspaces=[{"id": "alpha", "name": "Alpha"}],
    )
    monkeypatch.setattr(references, "get_workspace_catalog_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        references,
        "read_workspace_runtime_config",
        lambda _workspace_id: {"llm": {"default_profile_ref": "legacy-main"}},
    )
    monkeypatch.setattr(references, "get_workspace_config_generation", lambda _workspace_id: 0)

    result = references.get_model_profile_references()

    assert result["entries"][0]["references"] == [
        {
            "field": "default_main_profile_ref",
            "scope": "global",
            "profile_id": "legacy-main",
        }
    ]
    with pytest.raises(references.InvalidModelProfileFilterError, match="profile_id"):
        references.get_model_profile_references(profile_id)


def test_inventory_generation_tracks_catalog_and_runtime_config_generations(monkeypatch):
    from config import model_profile_references as references

    state = {"catalog_generation": 4, "config_generation": 8}

    def load_catalog():
        return _CatalogSnapshot(
            generation=state["catalog_generation"],
            workspaces=[{"id": "alpha", "name": "Alpha"}],
        )

    monkeypatch.setattr(references, "get_workspace_catalog_snapshot", load_catalog)
    monkeypatch.setattr(references, "read_workspace_runtime_config", lambda _workspace_id: {})
    monkeypatch.setattr(
        references,
        "get_workspace_config_generation",
        lambda _workspace_id: state["config_generation"],
    )

    baseline = references.get_model_profile_references()["generation"]
    assert references.get_model_profile_references()["generation"] == baseline

    state["config_generation"] += 1
    config_changed = references.get_model_profile_references()["generation"]
    assert config_changed != baseline

    state["catalog_generation"] += 1
    assert references.get_model_profile_references()["generation"] != config_changed
