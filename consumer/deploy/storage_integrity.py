#!/usr/bin/env python3
"""Create and verify canonical Consumer storage archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


ARCHIVE_NAME = "storage-data.tar.gz"
MANIFEST_NAME = "storage-manifest.json"
CHECKSUMS_NAME = "storage-checksums.sha256"
DATASET_NAMES = ("runtime", "auth")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_relative(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    _validate_relative(relative)
    return relative


def _validate_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"invalid canonical relative path: {value!r}")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError(f"invalid canonical relative path: {value!r}")
    canonical = parsed.as_posix()
    if canonical != value:
        raise ValueError(f"non-canonical relative path: {value!r}")
    return canonical


def _scan_dataset(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"storage dataset root is not a directory: {root}")

    directories: list[str] = []
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = _canonical_relative(path, root)
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"storage dataset contains unsupported symbolic link: {relative}")
        if stat.S_ISDIR(mode):
            directories.append(relative)
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"storage dataset contains unsupported special file: {relative}")
        size = path.stat().st_size
        total_bytes += size
        files.append(
            {
                "path": relative,
                "size": size,
                "sha256": _sha256(path),
                "mode": format(stat.S_IMODE(mode), "04o"),
            }
        )
    return {
        "directories": directories,
        "files": files,
        "file_count": len(files),
        "bytes": total_bytes,
    }


def _tree_digest(datasets: dict[str, Any]) -> str:
    encoded = json.dumps(
        datasets,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_manifest(runtime: Path, auth: Path, storage_mode: str) -> dict[str, Any]:
    if storage_mode not in {"bind", "volume"}:
        raise ValueError("storage mode must be bind or volume")
    datasets = {
        "runtime": _scan_dataset(runtime),
        "auth": _scan_dataset(auth),
    }
    manifest = {
        "schema_version": 1,
        "storage_mode": storage_mode,
        "datasets": datasets,
        "totals": {
            "file_count": sum(item["file_count"] for item in datasets.values()),
            "bytes": sum(item["bytes"] for item in datasets.values()),
        },
        "tree_sha256": _tree_digest(datasets),
    }
    _validate_manifest(manifest)
    return manifest


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write(path, encoded)


def _tar_info_for_directory(path: Path, archive_name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(archive_name.rstrip("/") + "/")
    info.type = tarfile.DIRTYPE
    info.mode = stat.S_IMODE(path.stat().st_mode)
    info.mtime = int(path.stat().st_mtime)
    return info


def _create_tar(archive: Path, roots: dict[str, Path]) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent
    )
    os.close(descriptor)
    temp_path = Path(raw_temp)
    try:
        with tarfile.open(temp_path, "w:gz") as handle:
            for dataset_name in DATASET_NAMES:
                root = roots[dataset_name].resolve()
                handle.addfile(_tar_info_for_directory(root, dataset_name))
                for path in sorted(
                    root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
                ):
                    relative = _canonical_relative(path, root)
                    arcname = f"{dataset_name}/{relative}"
                    mode = path.lstat().st_mode
                    if stat.S_ISLNK(mode) or not (
                        stat.S_ISDIR(mode) or stat.S_ISREG(mode)
                    ):
                        raise ValueError(
                            f"storage dataset contains unsupported entry: {relative}"
                        )
                    handle.add(path, arcname=arcname, recursive=False)
        with temp_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, archive)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_storage_checksums(backup_dir: Path) -> None:
    lines = [
        f"{_sha256(backup_dir / ARCHIVE_NAME)}  {ARCHIVE_NAME}\n",
        f"{_sha256(backup_dir / MANIFEST_NAME)}  {MANIFEST_NAME}\n",
    ]
    _atomic_write(backup_dir / CHECKSUMS_NAME, "".join(lines).encode("ascii"))


def create_archive(
    runtime: Path,
    auth: Path,
    archive: Path,
    manifest_path: Path,
    storage_mode: str,
) -> dict[str, Any]:
    if archive.name != ARCHIVE_NAME or manifest_path.name != MANIFEST_NAME:
        raise ValueError(
            f"archive and manifest must be named {ARCHIVE_NAME} and {MANIFEST_NAME}"
        )
    if archive.parent.resolve() != manifest_path.parent.resolve():
        raise ValueError("archive and manifest must share a backup directory")
    manifest = create_manifest(runtime, auth, storage_mode)
    _create_tar(archive, {"runtime": runtime, "auth": auth})
    _write_json(manifest_path, manifest)
    _write_storage_checksums(archive.parent)
    verify_backup(archive.parent)
    return manifest


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("storage manifest must be a JSON object")
    _validate_manifest(payload)
    return payload


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported storage manifest schema")
    if manifest.get("storage_mode") not in {"bind", "volume"}:
        raise ValueError("storage manifest has invalid storage mode")
    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict) or set(datasets) != set(DATASET_NAMES):
        raise ValueError("storage manifest must contain runtime and auth datasets")

    total_files = 0
    total_bytes = 0
    for dataset_name in DATASET_NAMES:
        dataset = datasets[dataset_name]
        if not isinstance(dataset, dict):
            raise ValueError(f"invalid {dataset_name} dataset manifest")
        directories = dataset.get("directories")
        files = dataset.get("files")
        if not isinstance(directories, list) or not isinstance(files, list):
            raise ValueError(f"invalid {dataset_name} manifest entries")
        normalized_directories = [_validate_relative(value) for value in directories]
        if normalized_directories != sorted(set(normalized_directories)):
            raise ValueError(f"non-canonical {dataset_name} directory ordering")
        normalized_files: list[str] = []
        calculated_bytes = 0
        for item in files:
            if not isinstance(item, dict):
                raise ValueError(f"invalid {dataset_name} file manifest entry")
            relative = _validate_relative(item.get("path"))
            normalized_files.append(relative)
            size = item.get("size")
            digest = item.get("sha256")
            mode = item.get("mode")
            if not isinstance(size, int) or size < 0:
                raise ValueError(f"invalid size for {dataset_name}/{relative}")
            if not isinstance(digest, str) or len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(f"invalid SHA256 for {dataset_name}/{relative}")
            if not isinstance(mode, str) or len(mode) != 4 or any(
                character not in "01234567" for character in mode
            ):
                raise ValueError(f"invalid mode for {dataset_name}/{relative}")
            calculated_bytes += size
        if normalized_files != sorted(set(normalized_files)):
            raise ValueError(f"non-canonical {dataset_name} file ordering")
        if dataset.get("file_count") != len(files) or dataset.get("bytes") != calculated_bytes:
            raise ValueError(f"invalid totals for {dataset_name} dataset")
        total_files += len(files)
        total_bytes += calculated_bytes

    if manifest.get("totals") != {"file_count": total_files, "bytes": total_bytes}:
        raise ValueError("invalid storage manifest totals")
    expected_tree_digest = _tree_digest(datasets)
    if manifest.get("tree_sha256") != expected_tree_digest:
        raise ValueError("storage manifest tree SHA256 mismatch")


def _verify_checksums(backup_dir: Path) -> None:
    checksums_path = backup_dir / CHECKSUMS_NAME
    if not checksums_path.is_file():
        raise ValueError(f"missing {CHECKSUMS_NAME}")
    expected_names = {ARCHIVE_NAME, MANIFEST_NAME}
    observed: dict[str, str] = {}
    for raw_line in checksums_path.read_text(encoding="ascii").splitlines():
        parts = raw_line.split()
        if len(parts) != 2:
            raise ValueError("invalid storage checksum manifest")
        digest, name = parts
        if name in observed or name not in expected_names:
            raise ValueError("invalid storage checksum entry")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("invalid storage checksum digest")
        observed[name] = digest
    if set(observed) != expected_names:
        raise ValueError("storage checksum manifest is incomplete")
    for name, expected in observed.items():
        path = backup_dir / name
        if not path.is_file() or _sha256(path) != expected:
            raise ValueError(f"storage backup checksum mismatch: {name}")


def _safe_member_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    raw_name = member.name.rstrip("/")
    if not raw_name or "\\" in raw_name:
        raise ValueError(f"unsafe archive member: {member.name!r}")
    path = PurePosixPath(raw_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe archive member: {member.name!r}")
    if path.parts[0] not in DATASET_NAMES or not (member.isdir() or member.isfile()):
        raise ValueError(f"unsafe archive member: {member.name!r}")
    return path.parts


def _extract_tar(archive: Path, runtime: Path, auth: Path) -> None:
    roots = {"runtime": runtime, "auth": auth}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as handle:
        members = handle.getmembers()
        for member in members:
            _safe_member_parts(member)
        for member in members:
            parts = _safe_member_parts(member)
            root = roots[parts[0]].resolve()
            relative_parts = parts[1:]
            target = root.joinpath(*relative_parts)
            target.resolve().relative_to(root)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                if relative_parts:
                    target.chmod(member.mode & 0o777)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = handle.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read archive member: {member.name}")
            descriptor, raw_temp = tempfile.mkstemp(
                prefix=f".{target.name}.restore-", dir=target.parent
            )
            temp_path = Path(raw_temp)
            try:
                with source, os.fdopen(descriptor, "wb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
                temp_path.chmod(member.mode & 0o777)
                os.replace(temp_path, target)
            finally:
                temp_path.unlink(missing_ok=True)


def _assert_archive_matches_manifest(archive: Path, manifest: dict[str, Any]) -> None:
    observed: dict[str, dict[str, Any]] = {
        name: {"directories": [], "files": [], "file_count": 0, "bytes": 0}
        for name in DATASET_NAMES
    }
    seen_roots: set[str] = set()
    seen_entries: set[tuple[str, str]] = set()
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle:
            parts = _safe_member_parts(member)
            dataset_name = parts[0]
            if len(parts) == 1:
                if not member.isdir() or dataset_name in seen_roots:
                    raise ValueError(f"unsafe archive member: {member.name!r}")
                seen_roots.add(dataset_name)
                continue
            relative = PurePosixPath(*parts[1:]).as_posix()
            _validate_relative(relative)
            entry_key = (dataset_name, relative)
            if entry_key in seen_entries:
                raise ValueError(f"duplicate archive member: {member.name!r}")
            seen_entries.add(entry_key)
            dataset = observed[dataset_name]
            if member.isdir():
                dataset["directories"].append(relative)
                continue
            source = handle.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read archive member: {member.name}")
            digest = hashlib.sha256()
            byte_count = 0
            with source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    byte_count += len(chunk)
            if byte_count != member.size:
                raise ValueError(f"archive member size mismatch: {member.name}")
            dataset["files"].append(
                {
                    "path": relative,
                    "size": byte_count,
                    "sha256": digest.hexdigest(),
                    "mode": format(member.mode & 0o777, "04o"),
                }
            )
            dataset["file_count"] += 1
            dataset["bytes"] += byte_count
    if seen_roots != set(DATASET_NAMES):
        raise ValueError("storage archive is missing a dataset root")
    for dataset in observed.values():
        dataset["directories"].sort()
        dataset["files"].sort(key=lambda item: item["path"])
    if observed != manifest["datasets"]:
        raise ValueError("storage archive does not match canonical manifest")


def _assert_tree_matches(runtime: Path, auth: Path, manifest: dict[str, Any]) -> None:
    observed = {
        "runtime": _scan_dataset(runtime),
        "auth": _scan_dataset(auth),
    }
    if observed != manifest["datasets"]:
        raise ValueError("storage tree does not match canonical manifest")
    if _tree_digest(observed) != manifest["tree_sha256"]:
        raise ValueError("storage tree SHA256 mismatch")


def _validate_staging_entries(runtime: Path, auth: Path, manifest: dict[str, Any]) -> None:
    for dataset_name, root in (("runtime", runtime), ("auth", auth)):
        if root.exists() and not root.is_dir():
            raise ValueError(f"restore target is not a directory: {root}")
        if not root.exists():
            continue
        dataset = manifest["datasets"][dataset_name]
        expected_directories = set(dataset["directories"])
        expected_files = {item["path"] for item in dataset["files"]}
        for path in root.rglob("*"):
            relative = _canonical_relative(path, root)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"restore staging contains unsupported entry: {relative}")
            if stat.S_ISDIR(mode) and relative not in expected_directories:
                raise ValueError(f"restore staging contains unexpected directory: {relative}")
            if stat.S_ISREG(mode) and relative not in expected_files:
                raise ValueError(f"restore staging contains unexpected file: {relative}")


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    backup_dir = backup_dir.resolve()
    _verify_checksums(backup_dir)
    manifest = _load_manifest(backup_dir / MANIFEST_NAME)
    _assert_archive_matches_manifest(backup_dir / ARCHIVE_NAME, manifest)
    return manifest


def extract_archive(backup_dir: Path, runtime: Path, auth: Path) -> dict[str, Any]:
    manifest = verify_backup(backup_dir)
    if runtime.is_dir() and auth.is_dir():
        try:
            _assert_tree_matches(runtime, auth, manifest)
            return manifest
        except ValueError:
            pass
    _validate_staging_entries(runtime, auth, manifest)
    _extract_tar(backup_dir / ARCHIVE_NAME, runtime, auth)
    _assert_tree_matches(runtime, auth, manifest)
    return manifest


def _copy_preflight_file(source_root: Path, output_root: Path, relative: Path) -> None:
    source = source_root / relative
    if not source.exists():
        return
    source_root_resolved = source_root.resolve()
    source_resolved = source.resolve()
    source_resolved.relative_to(source_root_resolved)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"preflight source is not a regular file: {relative.as_posix()}")
    target = output_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def export_preflight_view(runtime: Path, auth: Path, output: Path) -> None:
    runtime = runtime.resolve()
    auth = auth.resolve()
    if not runtime.is_dir() or not auth.is_dir():
        raise ValueError("preflight source roots must be directories")
    if output.exists() and any(output.iterdir()):
        raise ValueError("preflight output must be an empty staging directory")
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    runtime_output = output / ".onto-runtime"
    auth_output = output / ".onto-auth"
    runtime_output.mkdir(mode=0o700, exist_ok=True)
    auth_output.mkdir(mode=0o700, exist_ok=True)
    _copy_preflight_file(
        runtime,
        runtime_output,
        Path("config") / "system_config.json",
    )
    workspaces = runtime / "workspaces"
    if workspaces.is_dir() and not workspaces.is_symlink():
        for workspace in sorted(workspaces.iterdir(), key=lambda path: path.name):
            if workspace.is_symlink() or not workspace.is_dir():
                continue
            _copy_preflight_file(
                runtime,
                runtime_output,
                Path("workspaces") / workspace.name / "ontology-data-sources.json",
            )
    _copy_preflight_file(auth, auth_output, Path("users.json"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-archive")
    create.add_argument("--runtime", required=True, type=Path)
    create.add_argument("--auth", required=True, type=Path)
    create.add_argument("--archive", required=True, type=Path)
    create.add_argument("--manifest", required=True, type=Path)
    create.add_argument("--storage-mode", required=True, choices=("bind", "volume"))

    verify = commands.add_parser("verify-backup")
    verify.add_argument("--backup-dir", required=True, type=Path)

    extract = commands.add_parser("extract-archive")
    extract.add_argument("--backup-dir", required=True, type=Path)
    extract.add_argument("--runtime", required=True, type=Path)
    extract.add_argument("--auth", required=True, type=Path)

    manifest = commands.add_parser("create-manifest")
    manifest.add_argument("--runtime", required=True, type=Path)
    manifest.add_argument("--auth", required=True, type=Path)
    manifest.add_argument("--output", required=True, type=Path)
    manifest.add_argument("--storage-mode", required=True, choices=("bind", "volume"))

    verify_tree = commands.add_parser("verify-tree")
    verify_tree.add_argument("--runtime", required=True, type=Path)
    verify_tree.add_argument("--auth", required=True, type=Path)
    verify_tree.add_argument("--manifest", required=True, type=Path)

    preflight_view = commands.add_parser("export-preflight-view")
    preflight_view.add_argument("--runtime", required=True, type=Path)
    preflight_view.add_argument("--auth", required=True, type=Path)
    preflight_view.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "create-archive":
            result = create_archive(
                args.runtime,
                args.auth,
                args.archive,
                args.manifest,
                args.storage_mode,
            )
        elif args.command == "verify-backup":
            result = verify_backup(args.backup_dir)
        elif args.command == "extract-archive":
            result = extract_archive(args.backup_dir, args.runtime, args.auth)
        elif args.command == "create-manifest":
            result = create_manifest(args.runtime, args.auth, args.storage_mode)
            _write_json(args.output, result)
        elif args.command == "verify-tree":
            result = _load_manifest(args.manifest)
            _assert_tree_matches(args.runtime, args.auth, result)
        else:
            export_preflight_view(args.runtime, args.auth, args.output)
            print("ok")
            return 0
        print(result["tree_sha256"])
        return 0
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        print(f"[storage-integrity][error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
