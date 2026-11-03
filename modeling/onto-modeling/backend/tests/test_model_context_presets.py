"""模型上下文推荐表：名称规范化、精确匹配、表数据自洽、自定义表热加载与页面保存、接口权限。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import model_context_presets, model_settings
from app.services.model_context_presets import (
    CUSTOM_FILE_NAME,
    PREVIOUS_FILE_NAME,
    PresetConflictError,
    PresetFileError,
)

ADMIN = SimpleNamespace(state=SimpleNamespace(user={"role": "admin", "sub": "admin"}))
MODELER = SimpleNamespace(state=SimpleNamespace(user={"role": "modeler", "sub": "modeler"}))


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """每个用例用独立的数据目录，并清掉上一个用例加载的自定义表。"""
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(model_context_presets, "_snapshot", None)
    monkeypatch.setattr(model_context_presets, "_error", None)


def _entry(preset_id, window=200000, output=32000, **extra):
    return {"id": preset_id, "max_context_tokens": window, "max_output_tokens": output, **extra}


def _stored(preset_id, window=200000, output=32000, **extra):
    """_entry 经校验后写入文件的标准格式。"""
    return {
        "id": preset_id.strip(),
        "vendor": "",
        "aliases": [],
        "max_context_tokens": window,
        "max_output_tokens": output,
        "note": "",
        "sources": [],
        **extra,
    }


def _write_custom(directory: Path, document: dict) -> Path:
    path = directory / CUSTOM_FILE_NAME
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _builtin_presets() -> list[dict]:
    return model_context_presets._builtin()["presets"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  DeepSeek-V4.1-Flash ", "deepseek-v4-1-flash"),
        ("deepseek/deepseek-v4-pro", "deepseek-v4-pro"),
        ("glm_5.1", "glm-5-1"),
        ("deepseek-v4.1-flash-991231", "deepseek-v4-1-flash"),  # 网关 6 位日期后缀
        ("glm-5.2-20991231", "glm-5-2"),  # 8 位日期后缀
        ("qwen3.8-max-0902", "qwen3-8-max-0902"),  # 4 位快照号不是日期后缀
        ("qwen-plus-2025-04-28", "qwen-plus-2025-04-28"),  # 官方带横线日期快照不剥
        ("", ""),
    ],
)
def test_normalize_model_name(raw, expected):
    assert model_context_presets.normalize_model_name(raw) == expected


@pytest.mark.parametrize(
    ("model", "preset_id"),
    [
        ("deepseek-v4-pro", "deepseek-v4-pro"),
        ("deepseek-v4.1-flash", "deepseek-flash"),
        ("deepseek-v4-1-flash-991231", "deepseek-flash"),
        ("deepseek-v4-flash", "deepseek-flash"),
        ("minimax-m2.7", "MiniMax-M2.7"),
        ("MiniMax-M3", "MiniMax-M3"),
        ("qwen3.8-max-2026-09-02", "qwen3.8-max"),
        ("kimi-k3[1m]", "kimi-k3"),
        ("zhipu/GLM-5.3", "glm-5.3"),
    ],
)
def test_match_hits_official_ids_and_aliases(model, preset_id):
    assert model_context_presets.match(model)["id"] == preset_id


@pytest.mark.parametrize(
    "model",
    [
        "",
        "   ",
        # 用户要求暂不收录
        "deepseek-v4.1-pro",
        "deepseek-v3",
        "deepseek-v3.2",
        # 官方没有这个 id，只有 kimi-k2.7-code
        "kimi-k2.7",
        # 只做精确匹配，不按前缀猜
        "glm-5.3-air",
        "qwen3.8",
        # 旧快照参数与当前版不同，未列为别名
        "qwen-plus-2025-04-28",
    ],
)
def test_match_misses(model):
    assert model_context_presets.match(model) is None


def test_table_entries_are_consistent_and_saveable(monkeypatch):
    for env_name, _, _ in model_settings.CONTEXT_FIELDS.values():
        monkeypatch.delenv(env_name, raising=False)
    table = model_context_presets.presets()
    assert table["verified_at"]

    seen = {}
    for preset in table["presets"]:
        for name in [preset["id"], *preset["aliases"]]:
            key = model_context_presets.normalize_model_name(name)
            assert key not in seen, f"{name} 与 {seen.get(key)} 规范化后重名"
            seen[key] = preset["id"]
        assert preset["sources"] and all(url.startswith("https://") for url in preset["sources"])
        assert preset["max_output_tokens"] < preset["max_context_tokens"]
        # 预填值必须能原样通过配置页保存校验
        model_settings.save_context(
            {
                "max_context_tokens": preset["max_context_tokens"],
                "max_output_tokens": preset["max_output_tokens"],
            }
        )


def test_preset_routes_require_admin(monkeypatch):
    from app.api.errors import ApiError
    from app.routers import settings as settings_router
    from app.services import auth

    monkeypatch.setattr(auth, "ENABLED", True)
    with pytest.raises(ApiError) as exc:
        settings_router.get_context_presets(MODELER)
    assert (exc.value.status, exc.value.code) == (403, "forbidden")
    with pytest.raises(ApiError):
        settings_router.match_context_preset(MODELER, model="glm-5")

    assert settings_router.get_context_presets(ADMIN)["rows"]
    hit = settings_router.match_context_preset(ADMIN, model="glm-5")
    assert hit["preset"]["window"] == 200000
    assert settings_router.match_context_preset(ADMIN, model="unknown-model")["preset"] is None


def test_custom_table_overrides_builtin_and_appends(tmp_path):
    """同 id 整条替换内置条目（保持内置顺序），新 id 追加；与内置值不同时带上内置值。"""
    changed, same = _builtin_presets()[0], _builtin_presets()[1]
    _write_custom(
        tmp_path,
        {
            "verified_at": "2026-10-15",
            "presets": [
                _entry("example-model-pro", aliases=["example-pro"], sources=["https://example.com/docs/models"]),
                _entry(changed["id"], 128000, 16000),
                _entry(same["id"], same["max_context_tokens"], same["max_output_tokens"]),
            ],
        },
    )

    table = model_context_presets.presets()
    assert len(table["presets"]) == table["builtin_count"] + 1
    overridden = table["presets"][0]
    assert overridden["id"] == changed["id"]
    assert overridden["origin"] == "custom"
    assert overridden["overrides_builtin"] is True
    assert overridden["max_context_tokens"] == 128000
    assert overridden["builtin_values"] == {
        "max_context_tokens": changed["max_context_tokens"],
        "max_output_tokens": changed["max_output_tokens"],
    }
    assert table["presets"][1]["overrides_builtin"] is True
    assert "builtin_values" not in table["presets"][1]
    appended = table["presets"][-1]
    assert appended["id"] == "example-model-pro"
    assert appended["origin"] == "custom"
    assert "overrides_builtin" not in appended

    custom = table["custom"]
    assert custom["exists"] is True
    assert custom["count"] == 3
    assert custom["verified_at"] == "2026-10-15"
    assert custom["loaded_at"]
    assert custom["error"] is None
    assert custom["version"]
    assert [entry["id"] for entry in custom["entries"]] == ["example-model-pro", changed["id"], same["id"]]

    assert model_context_presets.match("provider/Example_Pro")["id"] == "example-model-pro"
    assert model_context_presets.match(changed["id"])["max_context_tokens"] == 128000


def test_custom_table_hot_reload_and_invalid_file_keeps_last_good(tmp_path):
    path = _write_custom(tmp_path, {"presets": [_entry("example-model-pro")]})
    assert model_context_presets.match("example-model-pro")["max_output_tokens"] == 32000

    # 改过即生效，无需重启
    _write_custom(tmp_path, {"presets": [_entry("example-model-pro", output=8000)]})
    assert model_context_presets.match("example-model-pro")["max_output_tokens"] == 8000
    loaded = model_context_presets.presets()["custom"]

    # 有误时整份不生效，沿用上一次合法的结果，错误随推荐表返回
    path.write_text("{bad json", encoding="utf-8")
    assert model_context_presets.match("example-model-pro")["max_output_tokens"] == 8000
    custom = model_context_presets.presets()["custom"]
    assert custom["error"].startswith("不是合法的 JSON")
    assert custom["loaded_at"] == loaded["loaded_at"]
    assert custom["version"] == loaded["version"]

    # 与内置条目规范化后重名同样整份不生效
    builtin_id = _builtin_presets()[0]["id"]
    _write_custom(tmp_path, {"presets": [_entry("example-model-pro", aliases=[builtin_id])]})
    assert "重名" in model_context_presets.presets()["custom"]["error"]

    _write_custom(tmp_path, {"presets": [_entry("example-model-max")]})
    custom = model_context_presets.presets()["custom"]
    assert custom["error"] is None
    assert model_context_presets.match("example-model-pro") is None
    assert model_context_presets.match("example-model-max")["origin"] == "custom"


def test_disable_custom_renames_to_backup(tmp_path):
    path = _write_custom(tmp_path, {"presets": [_entry("example-model-pro")]})
    assert model_context_presets.match("example-model-pro")

    result = model_context_presets.disable_custom()
    backup = Path(result["disabled_backup"])
    assert not path.exists()
    assert backup.is_file()
    assert backup.parent == tmp_path
    assert backup.name.startswith(f"{CUSTOM_FILE_NAME}.bak-")
    assert result["custom"]["backup_path"] == str(backup)
    assert result["custom"]["exists"] is False
    assert result["custom"]["count"] == 0
    assert model_context_presets.match("example-model-pro") is None

    # 文件已不在时直接返回
    assert model_context_presets.disable_custom()["disabled_backup"] is None


def test_save_custom_creates_file_then_keeps_previous_and_mode(tmp_path):
    path = tmp_path / CUSTOM_FILE_NAME
    assert model_context_presets.presets()["custom"]["version"] == ""

    saved = model_context_presets.save_custom(
        [_entry(" example-model-pro ", sources=["https://example.com/docs/models"])], "", "2026-10-15", "admin"
    )
    assert saved["previous_path"] is None
    assert path.stat().st_mode & 0o777 == 0o644
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "verified_at": "2026-10-15",
        "presets": [_stored("example-model-pro", sources=["https://example.com/docs/models"])],
    }
    assert saved["custom"]["count"] == 1
    assert saved["custom"]["version"]
    assert model_context_presets.match("example-model-pro")["origin"] == "custom"

    # 再次保存：上一版存为 .prev，沿用原文件权限；核对日期不传时沿用原值。
    # 热加载按修改时间与大小判断文件变了没有，用例里连续改写时让大小不同，免得落在同一时钟刻内
    path.chmod(0o600)
    first = path.read_bytes()
    saved = model_context_presets.save_custom(
        [_entry("example-model-pro", output=8000)], saved["custom"]["version"]
    )
    previous = tmp_path / PREVIOUS_FILE_NAME
    assert saved["previous_path"] == str(previous)
    assert previous.read_bytes() == first
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["verified_at"] == "2026-10-15"
    assert model_context_presets.match("example-model-pro")["max_output_tokens"] == 8000
    assert not _temporary_files(tmp_path)


def _temporary_files(directory: Path) -> list[Path]:
    return [path for path in directory.iterdir() if path.suffix == ".tmp"]


def test_save_custom_write_failure_keeps_file_and_removes_temporary(tmp_path, monkeypatch):
    path = _write_custom(tmp_path, {"presets": [_entry("example-model-pro")]})
    content = path.read_bytes()
    version = model_context_presets.presets()["custom"]["version"]

    def fail_replace(source, target):
        raise OSError("磁盘已满")

    monkeypatch.setattr(model_context_presets.os, "replace", fail_replace)
    with pytest.raises(OSError, match="磁盘已满"):
        model_context_presets.save_custom([_entry("example-model-max")], version)
    assert path.read_bytes() == content
    assert not _temporary_files(tmp_path)


def test_save_custom_rejects_stale_version_and_invalid_file(tmp_path):
    path = _write_custom(tmp_path, {"presets": [_entry("example-model-pro")]})
    version = model_context_presets.presets()["custom"]["version"]

    # 读取后运维又改了文件
    _write_custom(tmp_path, {"presets": [_entry("example-model-pro", output=8000)]})
    content = path.read_bytes()
    with pytest.raises(PresetConflictError, match="已被修改"):
        model_context_presets.save_custom([_entry("example-model-max")], version)
    # 页面读取时还没有文件，保存前已有人建了
    with pytest.raises(PresetConflictError, match="已被修改"):
        model_context_presets.save_custom([_entry("example-model-max")], "")
    assert path.read_bytes() == content
    assert not (tmp_path / PREVIOUS_FILE_NAME).exists()

    path.write_text("{bad json", encoding="utf-8")
    bad_version = model_context_presets._version(path.read_bytes())
    with pytest.raises(PresetConflictError, match="请先改正或停用"):
        model_context_presets.save_custom([_entry("example-model-max")], bad_version)
    assert path.read_text(encoding="utf-8") == "{bad json"


@pytest.mark.parametrize(
    ("entries", "verified_at", "message"),
    [
        ([_entry("example-model-pro", 128000, 128000)], None, "必须小于"),
        ([_entry("example-model-pro", True)], None, "必须是整数"),
        ([_entry("example-model-pro", 999)], None, "须在"),
        ([_entry("example-model-pro"), _entry("Example_Model.Pro")], None, "重复"),
        ([_entry("example-model-pro", aliases=["glm-5"])], None, "重名"),
        ([_entry("example-model-pro", sources=["example.com/docs"])], None, "sources"),
        ([_entry("example-model-pro", sources=["见 https://example.com/docs"])], None, "https://"),
        ([_entry("  ")], None, "id 必须是非空字符串"),
        ([_entry("example-model-pro")], "2026/10/15", "核对日期"),
    ],
)
def test_save_custom_rejects_invalid_entries(tmp_path, entries, verified_at, message):
    with pytest.raises(PresetFileError, match=message):
        model_context_presets.save_custom(entries, "", verified_at)
    assert not (tmp_path / CUSTOM_FILE_NAME).exists()


def test_save_custom_keeps_other_top_level_fields(tmp_path):
    path = _write_custom(tmp_path, {"presets": [], "_readme": ["说明"], "verified_at": "2026-10-01"})
    version = model_context_presets.presets()["custom"]["version"]

    # 核对日期传空串表示去掉；其它顶层字段原样保留且排在前面
    saved = model_context_presets.save_custom([_entry("example-model-pro")], version, "")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert list(document) == ["_readme", "presets"]
    assert document["_readme"] == ["说明"]
    assert saved["custom"]["verified_at"] == ""

    model_context_presets.save_custom([], saved["custom"]["version"], "2026-10-15")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert list(document) == ["_readme", "verified_at", "presets"]
    assert document["presets"] == []


def test_custom_preset_routes(tmp_path, monkeypatch):
    from app.api.errors import ApiError
    from app.routers import settings as settings_router
    from app.services import auth

    monkeypatch.setattr(auth, "ENABLED", True)
    row = settings_router.PresetRow(model="example-model-pro", window=200000, max_output=32000)
    body = settings_router.CustomPresetsBody(version="", presets=[row])
    with pytest.raises(ApiError) as exc:
        settings_router.put_custom_context_presets(body, MODELER)
    assert exc.value.status == 403
    with pytest.raises(ApiError):
        settings_router.disable_custom_context_presets(MODELER)
    assert not (tmp_path / CUSTOM_FILE_NAME).exists()

    saved = settings_router.put_custom_context_presets(body, ADMIN)
    assert saved["custom"]["added"] == 1 and saved["custom"]["status"] == "ok"
    # 带着旧版本号再存：文件已被改过
    with pytest.raises(ApiError) as exc:
        settings_router.put_custom_context_presets(body, ADMIN)
    assert (exc.value.status, exc.value.code) == (409, "revision_conflict")
    invalid = settings_router.CustomPresetsBody(
        version=saved["custom"]["version"],
        presets=[settings_router.PresetRow(model="example-model-pro", window=128000, max_output=128000)],
    )
    with pytest.raises(ApiError) as exc:
        settings_router.put_custom_context_presets(invalid, ADMIN)
    assert (exc.value.status, exc.value.code) == (400, "invalid_argument")

    disabled = settings_router.disable_custom_context_presets(ADMIN)
    assert disabled["custom"]["status"] == "disabled" and disabled["custom"]["backup_available"]
    assert not (tmp_path / CUSTOM_FILE_NAME).exists()


def test_custom_preset_rows_keep_aliases_and_sources(tmp_path, monkeypatch):
    """页面不编辑别名与来源链接：保存时沿用已存的同名条目，不会被整份替换冲掉。"""
    from app.routers import settings as settings_router

    monkeypatch.setattr(model_context_presets, "_snapshot", None)
    model_context_presets.save_custom(
        [_entry("example-model-pro", aliases=["example-pro"], sources=["https://example.com/model"])], ""
    )
    version = model_context_presets.presets()["custom"]["version"]
    body = settings_router.CustomPresetsBody(
        version=version,
        presets=[settings_router.PresetRow(model="example-model-pro", window=256000, max_output=32000)],
    )
    saved = settings_router.put_custom_context_presets(body, ADMIN)
    row = next(row for row in saved["rows"] if row["model"] == "example-model-pro")
    assert row["window"] == 256000 and row["aliases"] == ["example-pro"]
    assert row["sources"] == ["https://example.com/model"] and row["origin"] == "custom"
