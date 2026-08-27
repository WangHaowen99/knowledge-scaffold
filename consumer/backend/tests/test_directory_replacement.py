"""Crash recovery tests for portable ontology directory publication."""

from __future__ import annotations

import pytest

from ontology import directory_replacement
from ontology.directory_replacement import (
    DirectoryReplacementRecoveryError,
    recover_directory_replacements,
    recover_workspace_ontology_replacements,
)


def test_sync_directory_opens_with_explicit_restrictive_mode(tmp_path, monkeypatch):
    opened = []

    def fake_open(path, flags, mode):
        opened.append((path, flags, mode))
        return 7

    monkeypatch.setattr(directory_replacement.os, "open", fake_open)
    monkeypatch.setattr(directory_replacement.os, "fsync", lambda _descriptor: None)
    monkeypatch.setattr(directory_replacement.os, "close", lambda _descriptor: None)

    directory_replacement._sync_directory(tmp_path)

    assert opened == [
        (
            tmp_path,
            directory_replacement.os.O_RDONLY
            | getattr(directory_replacement.os, "O_DIRECTORY", 0),
            0o600,
        )
    ]


def _interrupted_backup(parent, target_name):
    backup_root = parent / ".{}.replace-backup-transaction_demo".format(
        target_name
    )
    previous = backup_root / "previous"
    previous.mkdir(parents=True)
    (backup_root / ".replace-transaction-v1").write_text("1\n", encoding="utf-8")
    return backup_root, previous


def test_recovery_restores_previous_tree_when_target_is_missing(tmp_path):
    backup_root, previous = _interrupted_backup(
        tmp_path,
        "knowledge_package_demo",
    )
    (previous / "state.txt").write_text("previous", encoding="utf-8")

    recovered = recover_directory_replacements(tmp_path)

    target = tmp_path / "knowledge_package_demo"
    assert recovered == 1
    assert (target / "state.txt").read_text(encoding="utf-8") == "previous"
    assert not backup_root.exists()


def test_recovery_keeps_published_tree_and_discards_rollback(tmp_path):
    target = tmp_path / "knowledge_package_demo"
    target.mkdir()
    (target / "state.txt").write_text("published", encoding="utf-8")
    backup_root, previous = _interrupted_backup(
        tmp_path,
        "knowledge_package_demo",
    )
    (previous / "state.txt").write_text("previous", encoding="utf-8")

    recovered = recover_directory_replacements(tmp_path)

    assert recovered == 1
    assert (target / "state.txt").read_text(encoding="utf-8") == "published"
    assert not backup_root.exists()


def test_recovery_fails_closed_when_both_target_and_rollback_are_missing(tmp_path):
    backup_root = tmp_path / ".knowledge_package_demo.replace-backup-transaction_demo"
    backup_root.mkdir()
    (backup_root / ".replace-transaction-v1").write_text("1\n", encoding="utf-8")

    with pytest.raises(
        DirectoryReplacementRecoveryError,
        match="rollback tree is unavailable",
    ):
        recover_directory_replacements(tmp_path)


def test_workspace_startup_recovery_scans_each_ontology_tree(tmp_path):
    ontologies_root = tmp_path / "workspace_demo" / "ontologies"
    ontologies_root.mkdir(parents=True)
    _backup_root, previous = _interrupted_backup(
        ontologies_root,
        "knowledge_package_demo",
    )
    (previous / "state.txt").write_text("previous", encoding="utf-8")

    recovered = recover_workspace_ontology_replacements(tmp_path)

    assert recovered == 1
    assert (
        ontologies_root / "knowledge_package_demo" / "state.txt"
    ).read_text(encoding="utf-8") == "previous"
