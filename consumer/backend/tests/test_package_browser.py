"""Security and behavior tests for ontology package browsing."""

from __future__ import annotations

import os

import pytest
from ontology.ontology_catalog import build_asset_catalog, find_asset_file
from ontology.package_browser import (
    MAX_VIEW_BYTES,
    PackageBrowserError,
    build_package_tree,
    list_package_files,
    read_package_text,
    resolve_package_file,
)


def _flatten_paths(entries):
    paths = []
    for entry in entries:
        paths.append(entry["path"])
        paths.extend(_flatten_paths(entry.get("children") or []))
    return paths


def test_package_tree_excludes_private_data_hidden_paths_cache_and_symlinks(tmp_path):
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / "流程.md").write_text("流程\n", encoding="utf-8")
    for name in ("origin_data", "origin-data", "datasets", "dataset", "__pycache__", ".hidden"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "secret.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".secret.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / "linked-file.md").symlink_to(tmp_path / "operations" / "流程.md")
    (tmp_path / "linked-dir").symlink_to(tmp_path / "operations", target_is_directory=True)

    paths = _flatten_paths(build_package_tree(tmp_path))

    assert paths == ["operations", "operations/流程.md"]


@pytest.mark.parametrize(
    "raw_path",
    (
        "",
        "../secret.txt",
        "..\\secret.txt",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "operations/./流程.md",
        ".hidden/file.md",
        "dataset/file.md",
    ),
)
def test_package_reader_rejects_unsafe_paths(tmp_path, raw_path):
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / "流程.md").write_text("流程\n", encoding="utf-8")

    with pytest.raises(PackageBrowserError) as exc_info:
        read_package_text(tmp_path, raw_path)

    assert exc_info.value.code == "invalid_path"


def test_package_reader_rejects_file_and_directory_symlinks(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "inside.md").write_text("inside\n", encoding="utf-8")
    (tmp_path / "file-link.md").symlink_to(real_dir / "inside.md")
    (tmp_path / "dir-link").symlink_to(real_dir, target_is_directory=True)

    for raw_path in ("file-link.md", "dir-link/inside.md"):
        with pytest.raises(PackageBrowserError) as exc_info:
            read_package_text(tmp_path, raw_path)
        assert exc_info.value.code == "invalid_path"


def test_package_reader_rejects_unsupported_binary_non_utf8_and_large_files(tmp_path):
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    (tmp_path / "nul.txt").write_bytes(b"text\x00binary")
    (tmp_path / "invalid.txt").write_bytes(b"\xff\xfe")
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_VIEW_BYTES + 1))

    expected_codes = {
        "image.png": "not_viewable",
        "nul.txt": "not_viewable",
        "invalid.txt": "invalid_encoding",
        "large.txt": "too_large",
    }
    for raw_path, expected_code in expected_codes.items():
        with pytest.raises(PackageBrowserError) as exc_info:
            read_package_text(tmp_path, raw_path)
        assert exc_info.value.code == expected_code


def test_package_reader_returns_utf8_text_metadata(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "规则.yaml").write_text("enabled: true\n", encoding="utf-8")

    payload = read_package_text(tmp_path, "rules/规则.yaml")

    assert payload == {
        "path": "rules/规则.yaml",
        "name": "规则.yaml",
        "size": len("enabled: true\n".encode("utf-8")),
        "content": "enabled: true\n",
    }


def test_editor_listing_uses_same_filters_and_recursion_policy(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "visible.md").write_text("visible\n", encoding="utf-8")
    (tmp_path / "nested" / "ignored.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "dataset").mkdir()
    (tmp_path / "dataset" / "secret.md").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".hidden.md").write_text("hidden\n", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=value\n", encoding="utf-8")
    if hasattr(os, "symlink"):
        (tmp_path / "linked.md").symlink_to(tmp_path / "nested" / "visible.md")

    flat = list_package_files(tmp_path, recursive=False, allowed_extensions={".md"})
    recursive = list_package_files(tmp_path, recursive=True, allowed_extensions={".md"})
    assets = list_package_files(
        tmp_path,
        recursive=True,
        include_hidden_files=True,
    )

    assert flat == []
    assert [item["name"] for item in recursive] == ["nested/visible.md"]
    assert [item["name"] for item in assets] == [
        ".env",
        ".hidden.md",
        "nested/ignored.json",
        "nested/visible.md",
    ]


def test_asset_resolution_can_allow_hidden_file_without_allowing_hidden_directory(tmp_path):
    (tmp_path / ".env").write_text("KEY=value\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.txt").write_text("secret\n", encoding="utf-8")

    assert (
        resolve_package_file(
            tmp_path,
            ".env",
            allow_hidden_file=True,
        )
        == tmp_path / ".env"
    )
    with pytest.raises(PackageBrowserError):
        resolve_package_file(
            tmp_path,
            ".hidden/secret.txt",
            allow_hidden_file=True,
        )


@pytest.mark.parametrize("entry_point", ("tree", "list", "resolve", "read"))
def test_package_browser_rejects_symlinked_ancestor_below_trusted_root(
    tmp_path,
    entry_point,
):
    trusted_root = tmp_path / "ontology-store"
    trusted_root.mkdir()
    real_versions = tmp_path / "real-versions"
    version_dir = real_versions / "v1"
    version_dir.mkdir(parents=True)
    (version_dir / "root.md").write_text("root\n", encoding="utf-8")
    (trusted_root / "versions").symlink_to(real_versions, target_is_directory=True)
    browsed_root = trusted_root / "versions" / "v1"

    with pytest.raises(PackageBrowserError) as exc_info:
        if entry_point == "tree":
            build_package_tree(browsed_root, trusted_root=trusted_root)
        elif entry_point == "list":
            list_package_files(
                browsed_root,
                recursive=True,
                trusted_root=trusted_root,
            )
        elif entry_point == "resolve":
            resolve_package_file(
                browsed_root,
                "root.md",
                trusted_root=trusted_root,
            )
        else:
            read_package_text(
                browsed_root,
                "root.md",
                trusted_root=trusted_root,
            )

    assert exc_info.value.code == "invalid_path"


def test_package_browser_treats_trusted_root_itself_as_the_boundary(tmp_path):
    actual_root = tmp_path / "actual-store"
    version_dir = actual_root / "v1"
    version_dir.mkdir(parents=True)
    (version_dir / "root.md").write_text("trusted\n", encoding="utf-8")
    trusted_root = tmp_path / "trusted-store"
    trusted_root.symlink_to(actual_root, target_is_directory=True)

    payload = read_package_text(
        trusted_root / "v1",
        "root.md",
        trusted_root=trusted_root,
    )

    assert payload["content"] == "trusted\n"


@pytest.mark.parametrize("target_kind", ("internal", "external"))
def test_asset_catalog_and_resolver_reject_symlinked_assets_directory(
    tmp_path,
    target_kind,
):
    ontology_dir = tmp_path / "ontology" / "versions" / "v1"
    ontology_dir.mkdir(parents=True)
    if target_kind == "internal":
        real_assets = ontology_dir / "real-assets"
    else:
        real_assets = tmp_path / "external-assets"
    real_assets.mkdir()
    (real_assets / "config.json").write_text("{}\n", encoding="utf-8")
    (ontology_dir / "assets").symlink_to(real_assets, target_is_directory=True)

    assert build_asset_catalog(ontology_dir) == []
    path, error = find_asset_file(ontology_dir, "config.json")
    assert path is None
    assert error == "Error: Invalid asset key."


@pytest.mark.parametrize("target_kind", ("internal", "external"))
def test_asset_catalog_and_resolver_reject_file_and_directory_symlinks(
    tmp_path,
    target_kind,
):
    ontology_dir = tmp_path / "ontology" / "versions" / "v1"
    assets_dir = ontology_dir / "assets"
    assets_dir.mkdir(parents=True)
    if target_kind == "internal":
        target_dir = assets_dir / "real"
    else:
        target_dir = tmp_path / "external"
    target_dir.mkdir()
    (target_dir / "config.json").write_text("{}\n", encoding="utf-8")
    (assets_dir / "config-link.json").symlink_to(target_dir / "config.json")
    (assets_dir / "directory-link").symlink_to(target_dir, target_is_directory=True)

    assert "config-link.json" not in build_asset_catalog(ontology_dir)
    for key in ("config-link.json", "directory-link/config.json"):
        path, error = find_asset_file(ontology_dir, key)
        assert path is None
        assert error == "Error: Invalid asset key."


def test_asset_catalog_and_resolver_allow_final_dotenv_but_reject_hidden_directory(
    tmp_path,
):
    ontology_dir = tmp_path / "ontology" / "versions" / "v1"
    assets_dir = ontology_dir / "assets"
    assets_dir.mkdir(parents=True)
    dotenv = assets_dir / ".env"
    dotenv.write_text("KEY=value\n", encoding="utf-8")
    hidden_dir = assets_dir / ".hidden"
    hidden_dir.mkdir()
    (hidden_dir / "secret.txt").write_text("secret\n", encoding="utf-8")

    assert build_asset_catalog(ontology_dir) == [".env"]
    path, error = find_asset_file(ontology_dir, ".env")
    assert path == dotenv
    assert error is None

    hidden_path, hidden_error = find_asset_file(
        ontology_dir,
        ".hidden/secret.txt",
    )
    assert hidden_path is None
    assert hidden_error == "Error: Invalid asset key."
