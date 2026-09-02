import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


DEPLOY_ROOT = Path(__file__).resolve().parents[1]
INTEGRITY_TOOL = DEPLOY_ROOT / "storage_integrity.py"


def _test_bash_executable() -> str:
    if os.name == "nt":
        git_path = shutil.which("git")
        if git_path:
            git_bash = Path(git_path).parent.parent / "bin" / "bash.exe"
            if git_bash.is_file():
                return str(git_bash)
    return "bash"


TEST_BASH = _test_bash_executable()


def _run_integrity(
    *args: object,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env.update(env or {})
    return subprocess.run(
        [sys.executable, str(INTEGRITY_TOOL), *(str(value) for value in args)],
        text=True,
        capture_output=True,
        check=False,
        env=process_env,
    )


def _write_synthetic_tree(root: Path) -> tuple[Path, Path]:
    runtime = root / "runtime"
    auth = root / "auth"
    (runtime / "nested").mkdir(parents=True)
    (auth / "keys").mkdir(parents=True)
    (runtime / "empty.txt").write_bytes(b"")
    (runtime / "nested" / "session.json").write_text(
        '{"synthetic": true}\n', encoding="utf-8"
    )
    (auth / "keys" / "signing.key").write_bytes(b"synthetic-key-material\n")
    return runtime, auth


def _install_fake_docker(fake_bin: Path, volume_root: Path, log_path: Path) -> None:
    implementation = fake_bin / "fake_docker.py"
    implementation.write_text(
        r'''import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
log_path = Path(os.environ["FAKE_DOCKER_LOG"])
log_path.parent.mkdir(parents=True, exist_ok=True)
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")

volume_root = Path(os.environ["FAKE_DOCKER_VOLUME_ROOT"])
volume_root.mkdir(parents=True, exist_ok=True)

if args[:2] == ["compose", "version"] or args[:1] == ["info"]:
    raise SystemExit(0)
if args[:1] == ["compose"]:
    raise SystemExit(0)
if args[:2] == ["container", "inspect"]:
    raise SystemExit(1)
if args[:2] == ["image", "inspect"]:
    raise SystemExit(0)
if args[:1] == ["inspect"]:
    print(os.environ.get("FAKE_DOCKER_HEALTH", "healthy"))
    raise SystemExit(0)
if args[:2] == ["volume", "create"]:
    name = args[-1]
    (volume_root / name).mkdir(parents=True, exist_ok=True)
    print(name)
    raise SystemExit(0)
if args[:2] == ["volume", "inspect"]:
    raise SystemExit(0 if (volume_root / args[-1]).is_dir() else 1)
if args[:2] == ["volume", "rm"]:
    path = volume_root / args[-1]
    if path.exists():
        shutil.rmtree(path)
    raise SystemExit(0)
if args[:1] != ["run"]:
    raise SystemExit(0)

mounts = {}
index = 1
while index < len(args):
    argument = args[index]
    if argument in {"--rm", "--read-only"} or argument.startswith("--network="):
        index += 1
        continue
    if argument in {"-v", "--volume", "--user", "--security-opt", "--cap-drop", "--tmpfs", "--env-file", "-e"}:
        value = args[index + 1]
        if argument in {"-v", "--volume"}:
            source, target, mode = value.rsplit(":", 2)
            if "/" not in source and "\\" not in source and ":" not in source:
                source_path = volume_root / source
            elif os.name == "nt" and source.startswith("/tmp/"):
                source_path = Path(os.environ.get("TEMP", os.environ.get("TMP", "C:/tmp"))) / source[len("/tmp/"):]
            else:
                source_path = Path(source)
            mounts[target] = source_path
        index += 2
        continue
    break

if index >= len(args):
    raise SystemExit(64)
image = args[index]
command = args[index + 1 :]
if not command:
    raise SystemExit(64)
if os.environ.get("FAKE_DOCKER_FAIL_COMMAND") and os.environ["FAKE_DOCKER_FAIL_COMMAND"] in " ".join(command):
    raise SystemExit(75)

def translate(value):
    for target in sorted(mounts, key=len, reverse=True):
        if value == target or value.startswith(target + "/"):
            suffix = value[len(target):].lstrip("/")
            return str(mounts[target] / suffix) if suffix else str(mounts[target])
    return value

translated = [translate(value) for value in command]
if translated[0] in {"python", "python3"}:
    translated[0] = sys.executable
helper_names = {
    "migrate_auth_workspace.py",
    "migrate_persistent_storage.py",
    "migrate_system_config.py",
}
if any(Path(value).name in helper_names for value in translated):
    report_index = translated.index("--report") + 1
    report = Path(translated[report_index])
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    raise SystemExit(0)
if "migrations.runner" in translated:
    report_index = translated.index("--report") + 1
    report = Path(translated[report_index])
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    raise SystemExit(0)
result = subprocess.run(translated, env=os.environ.copy(), check=False)
raise SystemExit(result.returncode)
''',
        encoding="utf-8",
    )
    wrapper = fake_bin / "docker"
    wrapper.write_text(
        '#!/bin/bash\nexport MSYS2_ARG_CONV_EXCL="*"\nexec "$FAKE_DOCKER_PYTHON" "$FAKE_DOCKER_IMPL" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


def _prepare_bundle(tmp_path: Path, storage_mode: str) -> tuple[Path, dict[str, str], Path]:
    bundle = tmp_path / "consumer"
    deploy = bundle / "deploy"
    backend = bundle / "backend"
    frontend = bundle / "frontend" / "logs" / "nginx"
    fake_bin = tmp_path / "fake-bin"
    volume_root = tmp_path / "volumes"
    deploy.mkdir(parents=True)
    backend.mkdir()
    frontend.mkdir(parents=True)
    fake_bin.mkdir()
    volume_root.mkdir()
    for name in (
        "backup.sh",
        "common.sh",
        "docker-compose.release.yml",
        "docker-compose.storage-volume.yml",
        "storage_integrity.py",
    ):
        shutil.copy2(DEPLOY_ROOT / name, deploy / name)
    for name in (
        "restore.sh",
        "migrate_storage.sh",
        "migrate.sh",
        "migrate_auth_workspace.py",
        "migrate_persistent_storage.py",
        "migrate_system_config.py",
    ):
        if (DEPLOY_ROOT / name).exists():
            shutil.copy2(DEPLOY_ROOT / name, deploy / name)
    (backend / ".env").write_text("USER_MAX_COUNT=10\n", encoding="utf-8")
    (deploy / "manifest.json").write_text(
        json.dumps(
            {
                "app_version": "v2",
                "backend_image": "onto-agent-backend:v2",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    state = deploy / "state"
    state.mkdir()
    (state / "current.env").write_text(
        "CURRENT_VERSION=v1\n"
        "PREVIOUS_VERSION=\n"
        "LAST_BACKUP_DIR=\n"
        "CURRENT_BACKEND_IMAGE=\n"
        "CURRENT_FRONTEND_IMAGE=\n"
        "CURRENT_AUTH_IMAGE=\n"
        "CURRENT_JIUWENBOX_IMAGE=\n"
        f"CURRENT_STORAGE_MODE={storage_mode}\n",
        encoding="utf-8",
    )
    (state / "storage-mode.env").write_text(
        f"STORAGE_MODE_STATUS=installed\nSTORAGE_MODE={storage_mode}\n",
        encoding="utf-8",
    )
    fake_flock = fake_bin / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_flock.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        '#!/bin/bash\nexec "$FAKE_DOCKER_PYTHON" "$@"\n', encoding="utf-8"
    )
    fake_python.chmod(0o755)
    state_io = fake_bin / "state-io"
    state_io.write_text(
        "#!/bin/sh\n"
        "operation=\"$1\"\nshift\n"
        "case \"$operation\" in\n"
        "  fsync-file) test -f \"$1\" ;;\n"
        "  replace) mv \"$1\" \"$2\" ;;\n"
        "  fsync-dir) test -d \"$1\" ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    state_io.chmod(0o755)
    log_path = tmp_path / "docker-log.jsonl"
    _install_fake_docker(fake_bin, volume_root, log_path)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_DOCKER_LOG"] = str(log_path)
    env["FAKE_DOCKER_VOLUME_ROOT"] = str(volume_root)
    env["FAKE_DOCKER_IMPL"] = (fake_bin / "fake_docker.py").as_posix()
    env["FAKE_DOCKER_PYTHON"] = Path(sys.executable).as_posix()
    env["DEPLOY_STATE_IO_HELPER"] = state_io.as_posix()
    env.pop("STORAGE_MODE", None)
    return bundle, env, log_path


def _run_script(bundle: Path, env: dict[str, str], script: str, *args: str):
    return subprocess.run(
        [TEST_BASH, str(bundle / "deploy" / script), *args],
        cwd=bundle,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _docker_calls(log_path: Path) -> list[list[str]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def _create_backup(tmp_path: Path, source_root: Path, storage_mode: str = "bind") -> Path:
    runtime, auth = _write_synthetic_tree(source_root)
    backup = tmp_path / "synthetic-backup"
    backup.mkdir()
    result = _run_integrity(
        "create-archive",
        "--runtime",
        runtime,
        "--auth",
        auth,
        "--archive",
        backup / "storage-data.tar.gz",
        "--manifest",
        backup / "storage-manifest.json",
        "--storage-mode",
        storage_mode,
    )
    assert result.returncode == 0, result.stderr
    return backup


def _tree_digest(backup: Path) -> str:
    return json.loads(
        (backup / "storage-manifest.json").read_text(encoding="utf-8")
    )["tree_sha256"]


def _write_storage_operation(
    bundle: Path,
    *,
    status: str,
    operation: str,
    target_mode: str,
    target_runtime_volume: str,
    target_auth_volume: str,
    source_mode: str,
    source_runtime_volume: str,
    source_auth_volume: str,
    digest: str,
    backup: Path,
    staged_runtime_name: str = "",
    staged_auth_name: str = "",
    rollback_runtime_name: str = "",
    rollback_auth_name: str = "",
) -> None:
    (bundle / "deploy" / "state" / "storage-operation.env").write_text(
        f"STORAGE_OPERATION_STATUS={status}\n"
        f"STORAGE_OPERATION={operation}\n"
        f"TARGET_STORAGE_MODE={target_mode}\n"
        f"TARGET_RUNTIME_VOLUME={target_runtime_volume}\n"
        f"TARGET_AUTH_VOLUME={target_auth_volume}\n"
        f"SOURCE_STORAGE_MODE={source_mode}\n"
        f"SOURCE_RUNTIME_VOLUME={source_runtime_volume}\n"
        f"SOURCE_AUTH_VOLUME={source_auth_volume}\n"
        f"STORAGE_TREE_SHA256={digest}\n"
        f"BACKUP_DIR_HEX={backup.resolve().as_posix().encode('utf-8').hex()}\n"
        f"STAGED_RUNTIME_NAME={staged_runtime_name}\n"
        f"STAGED_AUTH_NAME={staged_auth_name}\n"
        f"ROLLBACK_RUNTIME_NAME={rollback_runtime_name}\n"
        f"ROLLBACK_AUTH_NAME={rollback_auth_name}\n",
        encoding="utf-8",
    )


def _set_volume_pointer(
    bundle: Path, runtime_volume: str, auth_volume: str, *, current: bool = True
) -> None:
    marker = bundle / "deploy" / "state" / "storage-mode.env"
    marker.write_text(
        "STORAGE_MODE_STATUS=installed\n"
        "STORAGE_MODE=volume\n"
        f"RUNTIME_VOLUME_NAME={runtime_volume}\n"
        f"AUTH_VOLUME_NAME={auth_volume}\n",
        encoding="utf-8",
    )
    if current:
        state = bundle / "deploy" / "state" / "current.env"
        state.write_text(
            state.read_text(encoding="utf-8")
            + f"CURRENT_RUNTIME_VOLUME={runtime_volume}\n"
            + f"CURRENT_AUTH_VOLUME={auth_volume}\n",
            encoding="utf-8",
        )


def _assert_tree_matches_backup(runtime: Path, auth: Path, backup: Path) -> None:
    result = _run_integrity(
        "verify-tree",
        "--runtime",
        runtime,
        "--auth",
        auth,
        "--manifest",
        backup / "storage-manifest.json",
    )
    assert result.returncode == 0, result.stderr


def test_storage_safety_entrypoints_exist():
    expected = {
        "storage_integrity.py",
        "restore.sh",
        "migrate_storage.sh",
    }

    assert expected <= {path.name for path in DEPLOY_ROOT.iterdir()}


def test_integrity_archive_has_canonical_manifest_and_verifies(tmp_path):
    runtime, auth = _write_synthetic_tree(tmp_path / "source")
    backup = tmp_path / "backup"
    backup.mkdir()
    archive = backup / "storage-data.tar.gz"
    manifest_path = backup / "storage-manifest.json"

    result = _run_integrity(
        "create-archive",
        "--runtime",
        runtime,
        "--auth",
        auth,
        "--archive",
        archive,
        "--manifest",
        manifest_path,
        "--storage-mode",
        "bind",
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["storage_mode"] == "bind"
    assert manifest["totals"] == {
        "file_count": 3,
        "bytes": sum(path.stat().st_size for path in (runtime / "empty.txt", runtime / "nested" / "session.json", auth / "keys" / "signing.key")),
    }
    runtime_files = manifest["datasets"]["runtime"]["files"]
    assert [item["path"] for item in runtime_files] == [
        "empty.txt",
        "nested/session.json",
    ]
    assert runtime_files[0]["size"] == 0
    assert runtime_files[0]["sha256"] == hashlib.sha256(b"").hexdigest()
    assert manifest["datasets"]["runtime"]["directories"] == ["nested"]
    assert "\\" not in json.dumps(manifest)
    assert len(manifest["tree_sha256"]) == 64

    verify = _run_integrity("verify-backup", "--backup-dir", backup)
    assert verify.returncode == 0, verify.stderr


@pytest.mark.parametrize(
    ("environment_name", "environment_value"),
    (
        ("CONSUMER_STORAGE_ARCHIVE_MAX_TOTAL_BYTES", "1"),
        ("CONSUMER_STORAGE_ARCHIVE_MAX_MEMBERS", "1"),
    ),
)
def test_integrity_rejects_archives_over_resource_limits(
    tmp_path,
    environment_name,
    environment_value,
):
    runtime, auth = _write_synthetic_tree(tmp_path / "source")
    backup = tmp_path / "backup"
    backup.mkdir()
    created = _run_integrity(
        "create-archive",
        "--runtime",
        runtime,
        "--auth",
        auth,
        "--archive",
        backup / "storage-data.tar.gz",
        "--manifest",
        backup / "storage-manifest.json",
        "--storage-mode",
        "bind",
    )
    assert created.returncode == 0, created.stderr

    verified = _run_integrity(
        "verify-backup",
        "--backup-dir",
        backup,
        env={environment_name: environment_value},
    )

    assert verified.returncode == 2
    assert "archive resource limit exceeded" in verified.stderr.lower()


def test_integrity_rejects_corrupt_archive(tmp_path):
    runtime, auth = _write_synthetic_tree(tmp_path / "source")
    backup = tmp_path / "backup"
    backup.mkdir()
    archive = backup / "storage-data.tar.gz"
    manifest = backup / "storage-manifest.json"
    created = _run_integrity(
        "create-archive",
        "--runtime",
        runtime,
        "--auth",
        auth,
        "--archive",
        archive,
        "--manifest",
        manifest,
        "--storage-mode",
        "bind",
    )
    assert created.returncode == 0, created.stderr
    payload = bytearray(archive.read_bytes())
    payload[len(payload) // 2] ^= 0xFF
    archive.write_bytes(payload)

    verify = _run_integrity("verify-backup", "--backup-dir", backup)

    assert verify.returncode != 0
    assert "checksum" in verify.stderr.lower() or "archive" in verify.stderr.lower()


def test_integrity_extracts_only_into_empty_staging_roots(tmp_path):
    runtime, auth = _write_synthetic_tree(tmp_path / "source")
    backup = tmp_path / "backup"
    backup.mkdir()
    created = _run_integrity(
        "create-archive",
        "--runtime",
        runtime,
        "--auth",
        auth,
        "--archive",
        backup / "storage-data.tar.gz",
        "--manifest",
        backup / "storage-manifest.json",
        "--storage-mode",
        "bind",
    )
    assert created.returncode == 0, created.stderr
    target_runtime = tmp_path / "target" / "runtime"
    target_auth = tmp_path / "target" / "auth"

    extracted = _run_integrity(
        "extract-archive",
        "--backup-dir",
        backup,
        "--runtime",
        target_runtime,
        "--auth",
        target_auth,
    )

    assert extracted.returncode == 0, extracted.stderr
    assert (target_runtime / "nested" / "session.json").read_bytes() == (
        runtime / "nested" / "session.json"
    ).read_bytes()
    assert (target_auth / "keys" / "signing.key").read_bytes() == (
        auth / "keys" / "signing.key"
    ).read_bytes()

    rerun = _run_integrity(
        "extract-archive",
        "--backup-dir",
        backup,
        "--runtime",
        target_runtime,
        "--auth",
        target_auth,
    )
    assert rerun.returncode == 0, rerun.stderr

    # A killed staging copy can leave expected paths partial. Retrying repairs
    # those files while still rejecting any unexpected entry below.
    (target_runtime / "nested" / "session.json").write_text(
        "partial", encoding="utf-8"
    )
    (target_auth / "keys" / "signing.key").unlink()
    resumed = _run_integrity(
        "extract-archive",
        "--backup-dir",
        backup,
        "--runtime",
        target_runtime,
        "--auth",
        target_auth,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert (target_runtime / "nested" / "session.json").read_bytes() == (
        runtime / "nested" / "session.json"
    ).read_bytes()

    (target_runtime / "unexpected.txt").write_text("must survive", encoding="utf-8")
    rejected = _run_integrity(
        "extract-archive",
        "--backup-dir",
        backup,
        "--runtime",
        target_runtime,
        "--auth",
        target_auth,
    )
    assert rejected.returncode != 0
    assert (target_runtime / "unexpected.txt").read_text(encoding="utf-8") == "must survive"


def test_integrity_rejects_archive_path_traversal_before_writing(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    archive = backup / "storage-data.tar.gz"
    escaped = tmp_path / "escaped.txt"
    payload = tmp_path / "payload.txt"
    payload.write_text("synthetic", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(payload, arcname="../escaped.txt")
    datasets = {
        "runtime": {"directories": [], "files": [], "file_count": 0, "bytes": 0},
        "auth": {"directories": [], "files": [], "file_count": 0, "bytes": 0},
    }
    tree_sha256 = hashlib.sha256(
        json.dumps(
            datasets, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    (backup / "storage-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "storage_mode": "bind",
                "datasets": datasets,
                "totals": {"file_count": 0, "bytes": 0},
                "tree_sha256": tree_sha256,
            }
        ),
        encoding="utf-8",
    )
    (backup / "storage-checksums.sha256").write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  storage-data.tar.gz\n"
        f"{hashlib.sha256((backup / 'storage-manifest.json').read_bytes()).hexdigest()}  storage-manifest.json\n",
        encoding="utf-8",
    )

    result = _run_integrity(
        "extract-archive",
        "--backup-dir",
        backup,
        "--runtime",
        tmp_path / "runtime",
        "--auth",
        tmp_path / "auth",
    )

    assert result.returncode != 0
    assert "unsafe archive member" in result.stderr.lower()
    assert not escaped.exists()


def test_preflight_view_exports_only_required_volume_metadata(tmp_path):
    runtime = tmp_path / "runtime"
    auth = tmp_path / "auth"
    (runtime / "config").mkdir(parents=True)
    (runtime / "workspaces" / "ws-1").mkdir(parents=True)
    (runtime / "data" / "chats").mkdir(parents=True)
    (auth / "keys").mkdir(parents=True)
    (runtime / "config" / "system_config.json").write_text("{}", encoding="utf-8")
    (runtime / "workspaces" / "ws-1" / "ontology-data-sources.json").write_text(
        '{"sources": {}}', encoding="utf-8"
    )
    (runtime / "data" / "chats" / "large-session.json").write_text(
        "must-not-copy", encoding="utf-8"
    )
    (auth / "users.json").write_text('{"users": []}', encoding="utf-8")
    (auth / "keys" / "signing.key").write_text("must-not-copy", encoding="utf-8")
    output = tmp_path / "view"

    result = _run_integrity(
        "export-preflight-view",
        "--runtime",
        runtime,
        "--auth",
        auth,
        "--output",
        output,
    )

    assert result.returncode == 0, result.stderr
    assert (output / ".onto-runtime" / "config" / "system_config.json").is_file()
    assert (
        output
        / ".onto-runtime"
        / "workspaces"
        / "ws-1"
        / "ontology-data-sources.json"
    ).is_file()
    assert (output / ".onto-auth" / "users.json").is_file()
    assert not (output / ".onto-runtime" / "data").exists()
    assert not (output / ".onto-auth" / "keys").exists()


def test_volume_preflight_uses_a_read_only_minimal_storage_view():
    preflight = (DEPLOY_ROOT / "preflight.sh").read_text(encoding="utf-8")

    assert "prepare_preflight_storage_view" in preflight
    assert '"${ACTIVE_RUNTIME_VOLUME}:/source/runtime:ro"' in preflight
    assert '"${ACTIVE_AUTH_VOLUME}:/source/auth:ro"' in preflight
    assert "export-preflight-view" in preflight
    assert "trap cleanup_preflight_storage_view EXIT" in preflight


def test_bind_backup_creates_and_verifies_canonical_storage_bundle(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "bind")
    runtime, auth = _write_synthetic_tree(bundle / "source")
    shutil.move(runtime, bundle / ".onto-runtime")
    shutil.move(auth, bundle / ".onto-auth")

    result = _run_script(
        bundle, env, "backup.sh", "--confirm-writes-stopped", "v2", "v1"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    backups = list((bundle / "deploy" / "backups").iterdir())
    assert len(backups) == 1
    manifest = json.loads(
        (backups[0] / "storage-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["storage_mode"] == "bind"
    verified = _run_integrity("verify-backup", "--backup-dir", backups[0])
    assert verified.returncode == 0, verified.stderr
    assert not any(call[:1] == ["run"] for call in _docker_calls(log_path))


def test_volume_backup_uses_read_only_sources_and_release_pinned_utility(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "volume")
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    runtime = volume_root / "consumer-runtime-data"
    auth = volume_root / "consumer-auth-data"
    (runtime / "nested").mkdir(parents=True)
    (auth / "keys").mkdir(parents=True)
    (runtime / "nested" / "session.json").write_text(
        '{"synthetic": true}\n', encoding="utf-8"
    )
    (runtime / "empty.txt").write_bytes(b"")
    (auth / "keys" / "signing.key").write_text("synthetic-key\n", encoding="utf-8")

    result = _run_script(
        bundle, env, "backup.sh", "--confirm-writes-stopped", "v2", "v1"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    backup = next((bundle / "deploy" / "backups").iterdir())
    manifest = json.loads(
        (backup / "storage-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["storage_mode"] == "volume"
    calls = [call for call in _docker_calls(log_path) if call[:1] == ["run"]]
    assert calls
    flattened = " ".join(calls[0])
    assert "consumer-runtime-data:/source/runtime:ro" in flattened
    assert "consumer-auth-data:/source/auth:ro" in flattened
    assert ":/backup:rw" in flattened
    assert "onto-agent-backend:v2" in calls[0]
    assert "latest" not in flattened


def test_volume_backup_rejects_unpinned_utility_image_before_docker_run(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "volume")
    env["STORAGE_UTILITY_IMAGE"] = "synthetic/storage-utility:latest"

    result = _run_script(
        bundle, env, "backup.sh", "--confirm-writes-stopped", "v2", "v1"
    )

    assert result.returncode != 0
    assert "pinned" in result.stderr.lower()
    assert not any(call[:1] == ["run"] for call in _docker_calls(log_path))


def test_standalone_backup_requires_explicit_writer_freeze_confirmation(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "bind")
    runtime, auth = _write_synthetic_tree(bundle / "source")
    shutil.move(runtime, bundle / ".onto-runtime")
    shutil.move(auth, bundle / ".onto-auth")

    result = _run_script(bundle, env, "backup.sh", "v2", "v1")

    assert result.returncode != 0
    assert "--confirm-writes-stopped" in result.stderr
    assert not list((bundle / "deploy" / "backups").iterdir())
    assert not any(call[:1] == ["run"] for call in _docker_calls(log_path))


def test_deploy_and_rollback_confirm_writes_stopped_before_backup():
    deploy = (DEPLOY_ROOT / "deploy.sh").read_text(encoding="utf-8")
    rollback = (DEPLOY_ROOT / "rollback.sh").read_text(encoding="utf-8")

    assert '"${SCRIPT_DIR}/backup.sh" --confirm-writes-stopped' in deploy
    assert '"${SCRIPT_DIR}/backup.sh" --confirm-writes-stopped' in rollback


def test_release_manifest_pins_storage_utility_to_the_release_backend_image():
    build_text = (DEPLOY_ROOT / "build_release.sh").read_text(encoding="utf-8")

    assert '"storage_utility_image": "onto-agent-backend:${VERSION}"' in build_text


def test_upgrade_preextract_snapshot_is_storage_mode_aware_and_not_a_data_restore():
    upgrade_text = (DEPLOY_ROOT / "upgrade.sh").read_text(encoding="utf-8")

    assert 'CURRENT_STORAGE_MODE="${CURRENT_STORAGE_MODE:-bind}"' in upgrade_text
    assert 'if [ "${CURRENT_STORAGE_MODE}" = "bind" ]; then' in upgrade_text
    assert 'PROTECTED_ITEMS+=" .onto-runtime .onto-auth"' in upgrade_text
    assert "volume data backup runs after writers stop" in upgrade_text
    assert "restore.sh" not in upgrade_text


def test_operator_guide_documents_mode_aware_backup_restore_and_migration():
    readme = (DEPLOY_ROOT / "README.md").read_text(encoding="utf-8")

    for required in (
        "STORAGE_MODE=volume ./deploy/deploy.sh",
        "./deploy/backup.sh",
        "./deploy/restore.sh <backup-dir> --target-mode volume",
        "./deploy/migrate_storage.sh --to volume --dry-run",
        "./deploy/migrate_storage.sh --to volume",
        "storage-manifest.json",
        "storage-data.tar.gz",
        "storage-checksums.sha256",
        "code-only",
        "数据恢复必须显式执行",
    ):
        assert required in readme


def test_bind_restore_verifies_then_switches_and_rerun_is_idempotent(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "bind")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    (bundle / ".onto-runtime").mkdir()
    (bundle / ".onto-auth").mkdir()
    (bundle / ".onto-runtime" / "active-only.txt").write_text(
        "old-runtime", encoding="utf-8"
    )
    (bundle / ".onto-auth" / "active-only.txt").write_text(
        "old-auth", encoding="utf-8"
    )

    result = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "bind"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    _assert_tree_matches_backup(bundle / ".onto-runtime", bundle / ".onto-auth", backup)
    runtime_rollbacks = list(bundle.glob(".onto-runtime.rollback-*"))
    auth_rollbacks = list(bundle.glob(".onto-auth.rollback-*"))
    assert len(runtime_rollbacks) == len(auth_rollbacks) == 1
    assert (runtime_rollbacks[0] / "active-only.txt").read_text() == "old-runtime"
    assert (auth_rollbacks[0] / "active-only.txt").read_text() == "old-auth"
    state_text = (bundle / "deploy" / "state" / "storage-mode.env").read_text(
        encoding="utf-8"
    )
    assert "STORAGE_MODE_STATUS=installed" in state_text
    assert "STORAGE_MODE=bind" in state_text
    assert f"LAST_STORAGE_OPERATION_DIGEST={_tree_digest(backup)}" in state_text
    calls_before = _docker_calls(log_path)

    rerun = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "bind"
    )

    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "already matches" in rerun.stdout.lower()
    assert _docker_calls(log_path) == calls_before
    assert len(list(bundle.glob(".onto-runtime.rollback-*"))) == 1


def test_corrupt_restore_never_stops_or_overwrites_active_bind_data(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "bind")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    (bundle / ".onto-runtime").mkdir()
    (bundle / ".onto-auth").mkdir()
    active = bundle / ".onto-runtime" / "active.txt"
    active.write_text("must-remain", encoding="utf-8")
    archive = backup / "storage-data.tar.gz"
    payload = bytearray(archive.read_bytes())
    payload[len(payload) // 2] ^= 0xFF
    archive.write_bytes(payload)

    result = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "bind"
    )

    assert result.returncode != 0
    assert "checksum" in result.stderr.lower() or "archive" in result.stderr.lower()
    assert active.read_text(encoding="utf-8") == "must-remain"
    assert not any("stop" in call for call in _docker_calls(log_path))
    assert not list(bundle.glob(".onto-runtime.restore-*"))


def test_volume_restore_switches_pointer_only_after_staging_verification(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "volume")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    old_runtime = volume_root / "consumer-runtime-data"
    old_auth = volume_root / "consumer-auth-data"
    old_runtime.mkdir()
    old_auth.mkdir()
    (old_runtime / "old.txt").write_text("old-runtime", encoding="utf-8")
    (old_auth / "old.txt").write_text("old-auth", encoding="utf-8")

    result = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "volume"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    digest_prefix = _tree_digest(backup)[:12]
    runtime_name = f"consumer-runtime-data.restore-{digest_prefix}"
    auth_name = f"consumer-auth-data.restore-{digest_prefix}"
    _assert_tree_matches_backup(volume_root / runtime_name, volume_root / auth_name, backup)
    assert (old_runtime / "old.txt").read_text() == "old-runtime"
    assert (old_auth / "old.txt").read_text() == "old-auth"
    state_text = (bundle / "deploy" / "state" / "storage-mode.env").read_text()
    assert f"RUNTIME_VOLUME_NAME={runtime_name}" in state_text
    assert f"AUTH_VOLUME_NAME={auth_name}" in state_text
    calls = _docker_calls(log_path)
    extract_call = next(call for call in calls if "extract-archive" in call)
    assert f"{runtime_name}:/target/runtime:rw" in " ".join(extract_call)
    assert f"{auth_name}:/target/auth:rw" in " ".join(extract_call)
    assert f"{backup.as_posix()}:/backup:ro" in " ".join(extract_call) or ":/backup:ro" in " ".join(extract_call)
    create_count = sum(call[:2] == ["volume", "create"] for call in calls)
    compose_count = sum(call[:1] == ["compose"] for call in calls)

    rerun = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "volume"
    )
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    rerun_calls = _docker_calls(log_path)
    assert sum(call[:2] == ["volume", "create"] for call in rerun_calls) == create_count
    assert sum(call[:1] == ["compose"] for call in rerun_calls) == compose_count


def test_failed_volume_restore_verification_keeps_old_active_pointer(tmp_path):
    bundle, env, _ = _prepare_bundle(tmp_path, "volume")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    old_runtime = volume_root / "consumer-runtime-data"
    old_auth = volume_root / "consumer-auth-data"
    old_runtime.mkdir()
    old_auth.mkdir()
    (old_runtime / "old.txt").write_text("old-runtime", encoding="utf-8")
    (old_auth / "old.txt").write_text("old-auth", encoding="utf-8")
    env["FAKE_DOCKER_FAIL_COMMAND"] = "verify-tree"

    result = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "volume"
    )

    assert result.returncode != 0
    assert "staging verification failed" in result.stderr.lower()
    assert (old_runtime / "old.txt").read_text() == "old-runtime"
    assert (old_auth / "old.txt").read_text() == "old-auth"
    assert (bundle / "deploy" / "state" / "current.env").read_text().endswith(
        "CURRENT_STORAGE_MODE=volume\n"
    )
    state_text = (bundle / "deploy" / "state" / "storage-mode.env").read_text()
    assert state_text == "STORAGE_MODE_STATUS=installed\nSTORAGE_MODE=volume\n"


def test_volume_restore_health_failure_reverts_pointer_and_keeps_both_volumes(tmp_path):
    bundle, env, _ = _prepare_bundle(tmp_path, "volume")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    old_runtime = volume_root / "consumer-runtime-data"
    old_auth = volume_root / "consumer-auth-data"
    old_runtime.mkdir()
    old_auth.mkdir()
    (old_runtime / "old.txt").write_text("old-runtime", encoding="utf-8")
    (old_auth / "old.txt").write_text("old-auth", encoding="utf-8")
    env["FAKE_DOCKER_HEALTH"] = "unhealthy"

    result = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "volume"
    )

    assert result.returncode != 0
    assert "source storage remains active" in result.stderr.lower()
    assert (old_runtime / "old.txt").read_text() == "old-runtime"
    assert (old_auth / "old.txt").read_text() == "old-auth"
    digest_prefix = _tree_digest(backup)[:12]
    target_runtime = volume_root / f"consumer-runtime-data.restore-{digest_prefix}"
    target_auth = volume_root / f"consumer-auth-data.restore-{digest_prefix}"
    _assert_tree_matches_backup(target_runtime, target_auth, backup)
    state_text = (bundle / "deploy" / "state" / "storage-mode.env").read_text()
    assert "STORAGE_MODE=volume" in state_text
    assert "RUNTIME_VOLUME_NAME=" not in state_text
    current_text = (bundle / "deploy" / "state" / "current.env").read_text()
    assert "CURRENT_RUNTIME_VOLUME=consumer-runtime-data" in current_text
    assert "CURRENT_AUTH_VOLUME=consumer-auth-data" in current_text


@pytest.mark.parametrize("operation", ("restore", "migration"))
def test_retry_after_volume_target_health_rollback_reuses_verified_target(
    tmp_path, operation
):
    bundle, env, log_path = _prepare_bundle(tmp_path, "bind")
    runtime, auth = _write_synthetic_tree(bundle / "source")
    shutil.move(runtime, bundle / ".onto-runtime")
    shutil.move(auth, bundle / ".onto-auth")
    backup = None
    if operation == "restore":
        backup = _create_backup(tmp_path, tmp_path / "desired")
        command = (
            "restore.sh",
            backup.as_posix(),
            "--target-mode",
            "volume",
        )
    else:
        command = ("migrate_storage.sh", "--to", "volume")

    env["FAKE_DOCKER_HEALTH"] = "unhealthy"
    first = _run_script(bundle, env, *command)

    assert first.returncode != 0
    assert "source storage remains active" in first.stderr.lower()
    if backup is None:
        backup = next(
            path
            for path in (bundle / "deploy" / "backups").iterdir()
            if path.name.startswith("storage-migration-")
        )
    digest_prefix = _tree_digest(backup)[:12]
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    target_runtime = volume_root / f"consumer-runtime-data.{operation}-{digest_prefix}"
    target_auth = volume_root / f"consumer-auth-data.{operation}-{digest_prefix}"
    _assert_tree_matches_backup(target_runtime, target_auth, backup)
    calls_before_retry = len(_docker_calls(log_path))

    env["FAKE_DOCKER_HEALTH"] = "healthy"
    retry = _run_script(bundle, env, *command)

    assert retry.returncode == 0, retry.stdout + retry.stderr
    retry_calls = _docker_calls(log_path)[calls_before_retry:]
    assert not any("extract-archive" in call for call in retry_calls)
    _assert_tree_matches_backup(target_runtime, target_auth, backup)
    operation_state = (
        bundle / "deploy" / "state" / "storage-operation.env"
    ).read_text(encoding="utf-8")
    assert "STORAGE_OPERATION_STATUS=complete" in operation_state


def test_restore_refuses_to_overwrite_mismatched_existing_target_volumes(tmp_path):
    bundle, env, _ = _prepare_bundle(tmp_path, "bind")
    runtime, auth = _write_synthetic_tree(bundle / "source")
    shutil.move(runtime, bundle / ".onto-runtime")
    shutil.move(auth, bundle / ".onto-auth")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    digest_prefix = _tree_digest(backup)[:12]
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    target_runtime = volume_root / f"consumer-runtime-data.restore-{digest_prefix}"
    target_auth = volume_root / f"consumer-auth-data.restore-{digest_prefix}"
    shutil.copytree(tmp_path / "desired" / "runtime", target_runtime)
    shutil.copytree(tmp_path / "desired" / "auth", target_auth)
    conflicting_file = target_runtime / "nested" / "session.json"
    conflicting_file.write_text("must-not-be-overwritten", encoding="utf-8")

    result = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "volume"
    )

    assert result.returncode != 0
    assert "staging verification failed" in result.stderr.lower()
    assert conflicting_file.read_text(encoding="utf-8") == "must-not-be-overwritten"


@pytest.mark.parametrize("operation", ("restore", "migration"))
@pytest.mark.parametrize("phase", ("starting", "healthy"))
def test_started_target_resume_preserves_post_start_writes(tmp_path, operation, phase):
    bundle, env, _ = _prepare_bundle(tmp_path, "volume")
    backup = _create_backup(
        tmp_path, tmp_path / "desired", storage_mode="bind"
    )
    digest = _tree_digest(backup)
    runtime_name = f"consumer-runtime-data.{operation}-{digest[:12]}"
    auth_name = f"consumer-auth-data.{operation}-{digest[:12]}"
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    source_runtime = bundle / ".onto-runtime"
    source_auth = bundle / ".onto-auth"
    source_runtime.mkdir()
    source_auth.mkdir()
    (source_runtime / "old.txt").write_text("old-runtime", encoding="utf-8")
    (source_auth / "old.txt").write_text("old-auth", encoding="utf-8")
    shutil.copytree(tmp_path / "desired" / "runtime", volume_root / runtime_name)
    shutil.copytree(tmp_path / "desired" / "auth", volume_root / auth_name)
    post_start_write = volume_root / runtime_name / "post-start-write.txt"
    post_start_write.write_text("must-survive-resume", encoding="utf-8")
    _set_volume_pointer(bundle, runtime_name, auth_name)
    _write_storage_operation(
        bundle,
        status=phase,
        operation=operation,
        target_mode="volume",
        target_runtime_volume=runtime_name,
        target_auth_volume=auth_name,
        source_mode="bind",
        source_runtime_volume="consumer-runtime-data",
        source_auth_volume="consumer-auth-data",
        digest=digest,
        backup=backup,
    )

    if operation == "restore":
        result = _run_script(
            bundle,
            env,
            "restore.sh",
            backup.as_posix(),
            "--target-mode",
            "volume",
        )
    else:
        result = _run_script(bundle, env, "migrate_storage.sh", "--to", "volume")

    assert result.returncode == 0, result.stdout + result.stderr
    assert post_start_write.read_text(encoding="utf-8") == "must-survive-resume"
    marker = (bundle / "deploy" / "state" / "storage-mode.env").read_text()
    current = (bundle / "deploy" / "state" / "current.env").read_text()
    assert f"RUNTIME_VOLUME_NAME={runtime_name}" in marker
    assert f"CURRENT_RUNTIME_VOLUME={runtime_name}" in current
    operation_state = (
        bundle / "deploy" / "state" / "storage-operation.env"
    ).read_text(encoding="utf-8")
    assert "STORAGE_OPERATION_STATUS=complete" in operation_state


@pytest.mark.parametrize("operation", ("restore", "migration"))
def test_verified_target_pointer_split_failure_recovers_source(tmp_path, operation):
    bundle, env, _ = _prepare_bundle(tmp_path, "bind")
    runtime, auth = _write_synthetic_tree(bundle / "source")
    shutil.move(runtime, bundle / ".onto-runtime")
    shutil.move(auth, bundle / ".onto-auth")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    digest = _tree_digest(backup)
    runtime_name = f"consumer-runtime-data.{operation}-{digest[:12]}"
    auth_name = f"consumer-auth-data.{operation}-{digest[:12]}"
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    (volume_root / runtime_name).mkdir()
    (volume_root / auth_name).mkdir()
    _set_volume_pointer(bundle, runtime_name, auth_name, current=False)
    _write_storage_operation(
        bundle,
        status="verified",
        operation=operation,
        target_mode="volume",
        target_runtime_volume=runtime_name,
        target_auth_volume=auth_name,
        source_mode="bind",
        source_runtime_volume="consumer-runtime-data",
        source_auth_volume="consumer-auth-data",
        digest=digest,
        backup=backup,
    )

    if operation == "restore":
        result = _run_script(
            bundle,
            env,
            "restore.sh",
            backup.as_posix(),
            "--target-mode",
            "volume",
        )
    else:
        result = _run_script(bundle, env, "migrate_storage.sh", "--to", "volume")

    assert result.returncode != 0
    marker = (bundle / "deploy" / "state" / "storage-mode.env").read_text()
    current = (bundle / "deploy" / "state" / "current.env").read_text()
    assert "STORAGE_MODE=bind" in marker
    assert "RUNTIME_VOLUME_NAME=" not in marker
    assert "CURRENT_STORAGE_MODE=bind" in current
    assert "CURRENT_RUNTIME_VOLUME=consumer-runtime-data" in current
    assert "CURRENT_AUTH_VOLUME=consumer-auth-data" in current
    assert (bundle / ".onto-runtime" / "nested" / "session.json").exists()
    assert (bundle / ".onto-auth" / "keys" / "signing.key").exists()


@pytest.mark.parametrize("operation", ("restore", "migration"))
@pytest.mark.parametrize("target_state", ("runtime-only", "both-partial"))
def test_staging_resume_uses_unique_target_for_incomplete_volumes(
    tmp_path, operation, target_state
):
    bundle, env, log_path = _prepare_bundle(tmp_path, "bind")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    shutil.copytree(tmp_path / "desired" / "runtime", bundle / ".onto-runtime")
    shutil.copytree(tmp_path / "desired" / "auth", bundle / ".onto-auth")
    digest = _tree_digest(backup)
    runtime_name = f"consumer-runtime-data.{operation}-{digest[:12]}"
    auth_name = f"consumer-auth-data.{operation}-{digest[:12]}"
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    original_runtime = volume_root / runtime_name
    original_auth = volume_root / auth_name
    original_runtime.mkdir()
    (original_runtime / "partial.txt").write_text(
        "partial-runtime", encoding="utf-8"
    )
    if target_state == "both-partial":
        original_auth.mkdir()
        (original_auth / "partial.txt").write_text(
            "partial-auth", encoding="utf-8"
        )
    _write_storage_operation(
        bundle,
        status="staging",
        operation=operation,
        target_mode="volume",
        target_runtime_volume=runtime_name,
        target_auth_volume=auth_name,
        source_mode="bind",
        source_runtime_volume="consumer-runtime-data",
        source_auth_volume="consumer-auth-data",
        digest=digest,
        backup=backup,
    )

    if operation == "restore":
        result = _run_script(
            bundle,
            env,
            "restore.sh",
            backup.as_posix(),
            "--target-mode",
            "volume",
        )
    else:
        result = _run_script(bundle, env, "migrate_storage.sh", "--to", "volume")

    assert result.returncode == 0, result.stdout + result.stderr
    marker = dict(
        line.split("=", 1)
        for line in (bundle / "deploy" / "state" / "storage-mode.env")
        .read_text(encoding="utf-8")
        .splitlines()
        if "=" in line
    )
    final_runtime_name = marker["RUNTIME_VOLUME_NAME"]
    final_auth_name = marker["AUTH_VOLUME_NAME"]
    assert final_runtime_name != runtime_name
    assert final_auth_name != auth_name
    _assert_tree_matches_backup(
        volume_root / final_runtime_name, volume_root / final_auth_name, backup
    )
    assert (original_runtime / "partial.txt").read_text() == "partial-runtime"
    if target_state == "both-partial":
        assert (original_auth / "partial.txt").read_text() == "partial-auth"
    else:
        assert not original_auth.exists()
    assert not any(
        call[:2] == ["volume", "rm"] for call in _docker_calls(log_path)
    )
    operation_state = (
        bundle / "deploy" / "state" / "storage-operation.env"
    ).read_text(encoding="utf-8")
    assert "STORAGE_OPERATION_STATUS=complete" in operation_state


@pytest.mark.parametrize("new_operation", ("restore", "migration"))
def test_unfinished_restore_journal_blocks_a_different_operation(
    tmp_path, new_operation
):
    bundle, env, log_path = _prepare_bundle(tmp_path, "bind")
    backup_a = _create_backup(tmp_path / "a", tmp_path / "a" / "desired")
    runtime_b, auth_b = _write_synthetic_tree(tmp_path / "b" / "desired")
    (runtime_b / "nested" / "session.json").write_text(
        '{"different": true}\n', encoding="utf-8"
    )
    backup_b = tmp_path / "b" / "synthetic-backup"
    backup_b.mkdir()
    create_b = _run_integrity(
        "create-archive",
        "--runtime",
        runtime_b,
        "--auth",
        auth_b,
        "--archive",
        backup_b / "storage-data.tar.gz",
        "--manifest",
        backup_b / "storage-manifest.json",
        "--storage-mode",
        "bind",
    )
    assert create_b.returncode == 0, create_b.stderr
    digest_a = _tree_digest(backup_a)
    suffix_a = digest_a[:12]
    staged_runtime_name = f".onto-runtime.restore-{suffix_a}"
    staged_auth_name = f".onto-auth.restore-{suffix_a}"
    rollback_runtime_name = f".onto-runtime.rollback-restore-{suffix_a}-source"
    rollback_auth_name = f".onto-auth.rollback-restore-{suffix_a}-source"
    shutil.copytree(tmp_path / "a" / "desired" / "runtime", bundle / staged_runtime_name)
    shutil.copytree(tmp_path / "a" / "desired" / "auth", bundle / staged_auth_name)
    (bundle / ".onto-runtime").mkdir()
    (bundle / ".onto-auth").mkdir()
    (bundle / ".onto-runtime" / "source-runtime.txt").write_text(
        "old-runtime", encoding="utf-8"
    )
    (bundle / ".onto-auth" / "source-auth.txt").write_text(
        "old-auth", encoding="utf-8"
    )
    shutil.move(bundle / ".onto-runtime", bundle / rollback_runtime_name)
    _write_storage_operation(
        bundle,
        status="bind-move-old-runtime",
        operation="restore",
        target_mode="bind",
        target_runtime_volume="consumer-runtime-data",
        target_auth_volume="consumer-auth-data",
        source_mode="bind",
        source_runtime_volume="consumer-runtime-data",
        source_auth_volume="consumer-auth-data",
        digest=digest_a,
        backup=backup_a,
        staged_runtime_name=staged_runtime_name,
        staged_auth_name=staged_auth_name,
        rollback_runtime_name=rollback_runtime_name,
        rollback_auth_name=rollback_auth_name,
    )
    journal = bundle / "deploy" / "state" / "storage-operation.env"
    journal_before = journal.read_bytes()
    calls_before = _docker_calls(log_path)

    if new_operation == "restore":
        result = _run_script(
            bundle, env, "restore.sh", backup_b.as_posix(), "--target-mode", "bind"
        )
    else:
        result = _run_script(bundle, env, "migrate_storage.sh", "--to", "volume")

    assert result.returncode != 0
    assert "unfinished storage operation" in result.stderr.lower()
    assert journal.read_bytes() == journal_before
    assert _docker_calls(log_path) == calls_before
    assert not (bundle / ".onto-runtime").exists()
    assert (bundle / ".onto-auth" / "source-auth.txt").read_text() == "old-auth"
    assert (bundle / rollback_runtime_name / "source-runtime.txt").read_text() == "old-runtime"
    assert (bundle / staged_runtime_name).is_dir()
    assert (bundle / staged_auth_name).is_dir()


def test_bind_to_volume_migration_retains_source_and_is_idempotent(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "bind")
    runtime, auth = _write_synthetic_tree(bundle / "source")
    shutil.move(runtime, bundle / ".onto-runtime")
    shutil.move(auth, bundle / ".onto-auth")

    result = _run_script(bundle, env, "migrate_storage.sh", "--to", "volume")

    assert result.returncode == 0, result.stdout + result.stderr
    backup = next(
        path
        for path in (bundle / "deploy" / "backups").iterdir()
        if path.name.startswith("storage-migration-")
    )
    digest_prefix = _tree_digest(backup)[:12]
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    runtime_name = f"consumer-runtime-data.migration-{digest_prefix}"
    auth_name = f"consumer-auth-data.migration-{digest_prefix}"
    _assert_tree_matches_backup(volume_root / runtime_name, volume_root / auth_name, backup)
    _assert_tree_matches_backup(bundle / ".onto-runtime", bundle / ".onto-auth", backup)
    state_text = (bundle / "deploy" / "state" / "storage-mode.env").read_text()
    assert "STORAGE_MODE=volume" in state_text
    assert "ROLLBACK_STORAGE_MODE=bind" in state_text
    assert f"RUNTIME_VOLUME_NAME={runtime_name}" in state_text
    calls_before = _docker_calls(log_path)

    rerun = _run_script(bundle, env, "migrate_storage.sh", "--to", "volume")

    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "already uses" in rerun.stdout.lower()
    assert _docker_calls(log_path) == calls_before


def test_volume_to_bind_migration_retains_source_volume_and_stale_bind_rollback(tmp_path):
    bundle, env, _ = _prepare_bundle(tmp_path, "volume")
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    runtime = volume_root / "consumer-runtime-data"
    auth = volume_root / "consumer-auth-data"
    (runtime / "nested").mkdir(parents=True)
    (auth / "keys").mkdir(parents=True)
    (runtime / "nested" / "session.json").write_text("synthetic", encoding="utf-8")
    (auth / "keys" / "signing.key").write_text("synthetic-key", encoding="utf-8")
    (bundle / ".onto-runtime").mkdir()
    (bundle / ".onto-auth").mkdir()
    (bundle / ".onto-runtime" / "stale.txt").write_text("stale", encoding="utf-8")
    (bundle / ".onto-auth" / "stale.txt").write_text("stale", encoding="utf-8")

    result = _run_script(bundle, env, "migrate_storage.sh", "--to", "bind")

    assert result.returncode == 0, result.stdout + result.stderr
    backup = next(
        path
        for path in (bundle / "deploy" / "backups").iterdir()
        if path.name.startswith("storage-migration-")
    )
    _assert_tree_matches_backup(bundle / ".onto-runtime", bundle / ".onto-auth", backup)
    assert (runtime / "nested" / "session.json").read_text() == "synthetic"
    assert (auth / "keys" / "signing.key").read_text() == "synthetic-key"
    assert any((path / "stale.txt").exists() for path in bundle.glob(".onto-runtime.rollback-*"))
    state_text = (bundle / "deploy" / "state" / "storage-mode.env").read_text()
    assert "STORAGE_MODE=bind" in state_text
    assert "ROLLBACK_STORAGE_MODE=volume" in state_text
    assert "ROLLBACK_RUNTIME_VOLUME=consumer-runtime-data" in state_text
    assert "ROLLBACK_AUTH_VOLUME=consumer-auth-data" in state_text


def test_failed_bind_to_volume_migration_keeps_bind_active(tmp_path):
    bundle, env, _ = _prepare_bundle(tmp_path, "bind")
    runtime, auth = _write_synthetic_tree(bundle / "source")
    shutil.move(runtime, bundle / ".onto-runtime")
    shutil.move(auth, bundle / ".onto-auth")
    env["FAKE_DOCKER_FAIL_COMMAND"] = "verify-tree"

    result = _run_script(bundle, env, "migrate_storage.sh", "--to", "volume")

    assert result.returncode != 0
    assert "staging verification failed" in result.stderr.lower()
    state_text = (bundle / "deploy" / "state" / "storage-mode.env").read_text()
    assert state_text == "STORAGE_MODE_STATUS=installed\nSTORAGE_MODE=bind\n"
    assert (bundle / ".onto-runtime" / "nested" / "session.json").exists()
    assert (bundle / ".onto-auth" / "keys" / "signing.key").exists()


def test_registered_migrations_mount_active_volumes_in_one_shot_containers(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "volume")
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    (volume_root / "consumer-runtime-data").mkdir()
    (volume_root / "consumer-auth-data").mkdir()

    result = _run_script(bundle, env, "migrate.sh", "v1", "v2", "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    runs = [call for call in _docker_calls(log_path) if call[:1] == ["run"]]
    assert len(runs) == 4
    for call in runs:
        flattened = " ".join(call)
        assert "consumer-runtime-data:/release/.onto-runtime:ro" in flattened
        assert "consumer-auth-data:/release/.onto-auth:ro" in flattened
        assert ":/deploy-tools:ro" in flattened
    assert any(any(value.endswith("migrate_auth_workspace.py") for value in call) for call in runs)
    assert any(any(value.endswith("migrate_persistent_storage.py") for value in call) for call in runs)
    assert any(any(value.endswith("migrate_system_config.py") for value in call) for call in runs)
    assert any("migrations.runner" in call for call in runs)
    assert not (bundle / ".onto-runtime").exists()
    assert not (bundle / ".onto-auth").exists()


def test_registered_migrations_mount_active_storage_read_write_only_after_stop_confirmation(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "volume")
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    (volume_root / "consumer-runtime-data").mkdir()
    (volume_root / "consumer-auth-data").mkdir()

    result = _run_script(
        bundle, env, "migrate.sh", "v1", "v2", "--confirm-writes-stopped"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    runs = [call for call in _docker_calls(log_path) if call[:1] == ["run"]]
    assert len(runs) == 4
    for call in runs:
        flattened = " ".join(call)
        assert "consumer-runtime-data:/release/.onto-runtime:rw" in flattened
        assert "consumer-auth-data:/release/.onto-auth:rw" in flattened


def test_release_state_write_preserves_versioned_active_volume_pointer(tmp_path):
    bundle, env, _ = _prepare_bundle(tmp_path, "volume")
    current = bundle / "deploy" / "state" / "current.env"
    marker = bundle / "deploy" / "state" / "storage-mode.env"
    current.write_text(
        current.read_text(encoding="utf-8")
        + "CURRENT_RUNTIME_VOLUME=consumer-runtime-data.restore-123456789abc\n"
        + "CURRENT_AUTH_VOLUME=consumer-auth-data.restore-123456789abc\n",
        encoding="utf-8",
    )
    marker.write_text(
        "STORAGE_MODE_STATUS=installed\n"
        "STORAGE_MODE=volume\n"
        "RUNTIME_VOLUME_NAME=consumer-runtime-data.restore-123456789abc\n"
        "AUTH_VOLUME_NAME=consumer-auth-data.restore-123456789abc\n"
        "LAST_STORAGE_OPERATION=restore\n"
        "LAST_STORAGE_OPERATION_DIGEST=" + "1" * 64 + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(bundle / "deploy" / "common.sh").as_posix()}"; '
            "load_state; write_state v2 v1 backup backend frontend old-backend "
            "old-frontend auth old-auth sandbox old-sandbox",
        ],
        cwd=bundle,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    current_text = current.read_text(encoding="utf-8")
    marker_text = marker.read_text(encoding="utf-8")
    assert "CURRENT_RUNTIME_VOLUME=consumer-runtime-data.restore-123456789abc" in current_text
    assert "CURRENT_AUTH_VOLUME=consumer-auth-data.restore-123456789abc" in current_text
    assert "RUNTIME_VOLUME_NAME=consumer-runtime-data.restore-123456789abc" in marker_text
    assert "AUTH_VOLUME_NAME=consumer-auth-data.restore-123456789abc" in marker_text
    assert "LAST_STORAGE_OPERATION=restore" in marker_text


def test_restore_resumes_startup_after_pointer_switch_instead_of_early_exit(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "bind")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    shutil.copytree(tmp_path / "desired" / "runtime", bundle / ".onto-runtime")
    shutil.copytree(tmp_path / "desired" / "auth", bundle / ".onto-auth")
    digest = _tree_digest(backup)
    _write_storage_operation(
        bundle,
        status="pointer-switched",
        operation="restore",
        target_mode="bind",
        target_runtime_volume="consumer-runtime-data",
        target_auth_volume="consumer-auth-data",
        source_mode="bind",
        source_runtime_volume="consumer-runtime-data",
        source_auth_volume="consumer-auth-data",
        digest=digest,
        backup=backup,
    )

    result = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "bind"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = _docker_calls(log_path)
    assert any(call[:1] == ["compose"] and "up" in call for call in calls)
    operation_state = (
        bundle / "deploy" / "state" / "storage-operation.env"
    ).read_text(encoding="utf-8")
    assert "STORAGE_OPERATION_STATUS=complete" in operation_state


def test_migration_resumes_startup_after_pointer_switch(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "volume")
    backup = _create_backup(tmp_path, tmp_path / "desired", storage_mode="bind")
    digest = _tree_digest(backup)
    runtime_name = f"consumer-runtime-data.migration-{digest[:12]}"
    auth_name = f"consumer-auth-data.migration-{digest[:12]}"
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    shutil.copytree(tmp_path / "desired" / "runtime", volume_root / runtime_name)
    shutil.copytree(tmp_path / "desired" / "auth", volume_root / auth_name)
    _set_volume_pointer(bundle, runtime_name, auth_name)
    _write_storage_operation(
        bundle,
        status="pointer-switched",
        operation="migration",
        target_mode="volume",
        target_runtime_volume=runtime_name,
        target_auth_volume=auth_name,
        source_mode="bind",
        source_runtime_volume="consumer-runtime-data",
        source_auth_volume="consumer-auth-data",
        digest=digest,
        backup=backup,
    )

    result = _run_script(bundle, env, "migrate_storage.sh", "--to", "volume")

    assert result.returncode == 0, result.stdout + result.stderr
    calls = _docker_calls(log_path)
    assert any(call[:1] == ["compose"] and "up" in call for call in calls)
    operation_state = (
        bundle / "deploy" / "state" / "storage-operation.env"
    ).read_text(encoding="utf-8")
    assert "STORAGE_OPERATION_STATUS=complete" in operation_state


def test_restore_recovers_marker_current_split_during_health_rollback(tmp_path):
    bundle, env, log_path = _prepare_bundle(tmp_path, "volume")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    digest = _tree_digest(backup)
    target_runtime = f"consumer-runtime-data.restore-{digest[:12]}"
    target_auth = f"consumer-auth-data.restore-{digest[:12]}"
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    (volume_root / "consumer-runtime-data").mkdir()
    (volume_root / "consumer-auth-data").mkdir()
    shutil.copytree(tmp_path / "desired" / "runtime", volume_root / target_runtime)
    shutil.copytree(tmp_path / "desired" / "auth", volume_root / target_auth)

    # Crash point: the source marker is durable, while current.env still points
    # at the failed target and the rollback journal is not yet terminal.
    _set_volume_pointer(bundle, "consumer-runtime-data", "consumer-auth-data", current=False)
    current = bundle / "deploy" / "state" / "current.env"
    current.write_text(
        current.read_text(encoding="utf-8")
        + f"CURRENT_RUNTIME_VOLUME={target_runtime}\n"
        + f"CURRENT_AUTH_VOLUME={target_auth}\n",
        encoding="utf-8",
    )
    _write_storage_operation(
        bundle,
        status="rollback-pointer-switched",
        operation="restore",
        target_mode="volume",
        target_runtime_volume=target_runtime,
        target_auth_volume=target_auth,
        source_mode="volume",
        source_runtime_volume="consumer-runtime-data",
        source_auth_volume="consumer-auth-data",
        digest=digest,
        backup=backup,
    )

    result = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "volume"
    )

    assert result.returncode != 0
    assert "source storage remains active" in result.stderr.lower()
    current_text = current.read_text(encoding="utf-8")
    assert "CURRENT_RUNTIME_VOLUME=consumer-runtime-data" in current_text
    assert "CURRENT_AUTH_VOLUME=consumer-auth-data" in current_text
    operation_state = (
        bundle / "deploy" / "state" / "storage-operation.env"
    ).read_text(encoding="utf-8")
    assert "STORAGE_OPERATION_STATUS=failed" in operation_state
    assert any(call[:1] == ["compose"] and "up" in call for call in _docker_calls(log_path))


@pytest.mark.parametrize(
    ("phase", "moves"),
    (
        ("bind-move-old-runtime", ("old-runtime",)),
        ("bind-move-old-auth", ("old-runtime", "old-auth")),
        (
            "bind-promote-new-runtime",
            ("old-runtime", "old-auth", "new-runtime"),
        ),
        (
            "bind-promote-new-auth",
            ("old-runtime", "old-auth", "new-runtime", "new-auth"),
        ),
    ),
)
def test_bind_switch_resumes_each_rename_boundary_without_mixing_rollback_pair(
    tmp_path, phase, moves
):
    bundle, env, log_path = _prepare_bundle(tmp_path, "bind")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    digest = _tree_digest(backup)
    suffix = digest[:12]
    staged_runtime_name = f".onto-runtime.restore-{suffix}"
    staged_auth_name = f".onto-auth.restore-{suffix}"
    rollback_runtime_name = f".onto-runtime.rollback-restore-{suffix}-source"
    rollback_auth_name = f".onto-auth.rollback-restore-{suffix}-source"
    staged_runtime = bundle / staged_runtime_name
    staged_auth = bundle / staged_auth_name
    rollback_runtime = bundle / rollback_runtime_name
    rollback_auth = bundle / rollback_auth_name
    shutil.copytree(tmp_path / "desired" / "runtime", staged_runtime)
    shutil.copytree(tmp_path / "desired" / "auth", staged_auth)
    (bundle / ".onto-runtime").mkdir()
    (bundle / ".onto-auth").mkdir()
    (bundle / ".onto-runtime" / "source-runtime.txt").write_text(
        "old-runtime", encoding="utf-8"
    )
    (bundle / ".onto-auth" / "source-auth.txt").write_text(
        "old-auth", encoding="utf-8"
    )
    if "old-runtime" in moves:
        shutil.move(bundle / ".onto-runtime", rollback_runtime)
    if "old-auth" in moves:
        shutil.move(bundle / ".onto-auth", rollback_auth)
    if "new-runtime" in moves:
        shutil.move(staged_runtime, bundle / ".onto-runtime")
    if "new-auth" in moves:
        shutil.move(staged_auth, bundle / ".onto-auth")
    _write_storage_operation(
        bundle,
        status=phase,
        operation="restore",
        target_mode="bind",
        target_runtime_volume="consumer-runtime-data",
        target_auth_volume="consumer-auth-data",
        source_mode="bind",
        source_runtime_volume="consumer-runtime-data",
        source_auth_volume="consumer-auth-data",
        digest=digest,
        backup=backup,
        staged_runtime_name=staged_runtime_name,
        staged_auth_name=staged_auth_name,
        rollback_runtime_name=rollback_runtime_name,
        rollback_auth_name=rollback_auth_name,
    )

    result = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "bind"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    _assert_tree_matches_backup(bundle / ".onto-runtime", bundle / ".onto-auth", backup)
    assert (rollback_runtime / "source-runtime.txt").read_text() == "old-runtime"
    assert (rollback_auth / "source-auth.txt").read_text() == "old-auth"
    assert not list(bundle.glob(".onto-runtime.rollback-*")) == []
    operation_state = (
        bundle / "deploy" / "state" / "storage-operation.env"
    ).read_text(encoding="utf-8")
    assert "STORAGE_OPERATION_STATUS=complete" in operation_state
    assert f"ROLLBACK_RUNTIME_NAME={rollback_runtime_name}" in operation_state
    assert f"ROLLBACK_AUTH_NAME={rollback_auth_name}" in operation_state
    assert any(call[:1] == ["compose"] and "up" in call for call in _docker_calls(log_path))


def test_bind_health_rollback_resume_keeps_already_restored_source_pair(tmp_path):
    bundle, env, _ = _prepare_bundle(tmp_path, "bind")
    backup = _create_backup(tmp_path, tmp_path / "desired")
    digest = _tree_digest(backup)
    suffix = digest[:12]
    rollback_runtime_name = f".onto-runtime.rollback-restore-{suffix}-source"
    rollback_auth_name = f".onto-auth.rollback-restore-{suffix}-source"
    (bundle / ".onto-runtime").mkdir()
    (bundle / ".onto-auth").mkdir()
    (bundle / ".onto-runtime" / "source-runtime.txt").write_text(
        "old-runtime", encoding="utf-8"
    )
    (bundle / ".onto-auth" / "source-auth.txt").write_text(
        "old-auth", encoding="utf-8"
    )
    _write_storage_operation(
        bundle,
        status="rollback-pointer-switched",
        operation="restore",
        target_mode="bind",
        target_runtime_volume="consumer-runtime-data",
        target_auth_volume="consumer-auth-data",
        source_mode="bind",
        source_runtime_volume="consumer-runtime-data",
        source_auth_volume="consumer-auth-data",
        digest=digest,
        backup=backup,
        rollback_runtime_name=rollback_runtime_name,
        rollback_auth_name=rollback_auth_name,
    )

    result = _run_script(
        bundle, env, "restore.sh", backup.as_posix(), "--target-mode", "bind"
    )

    assert result.returncode != 0
    assert "source storage remains active" in result.stderr.lower()
    assert (bundle / ".onto-runtime" / "source-runtime.txt").read_text() == "old-runtime"
    assert (bundle / ".onto-auth" / "source-auth.txt").read_text() == "old-auth"
    assert not list(bundle.glob(".onto-runtime.failed-*"))
    assert not list(bundle.glob(".onto-auth.failed-*"))


@pytest.mark.parametrize(
    ("operation", "source_mode", "target_mode"),
    (
        ("migration", "bind", "volume"),
        ("migration", "volume", "bind"),
        ("restore", "bind", "volume"),
        ("restore", "volume", "bind"),
        ("restore", "bind", "bind"),
        ("restore", "volume", "volume"),
    ),
)
@pytest.mark.parametrize("phase", ("staging", "verified"))
def test_staging_resume_materializes_and_verifies_explicit_target_before_switch(
    tmp_path, operation, source_mode, target_mode, phase
):
    bundle, env, _ = _prepare_bundle(tmp_path, source_mode)
    backup = _create_backup(
        tmp_path, tmp_path / "desired", storage_mode=source_mode
    )
    digest = _tree_digest(backup)
    volume_root = Path(env["FAKE_DOCKER_VOLUME_ROOT"])
    if source_mode == "bind":
        shutil.copytree(tmp_path / "desired" / "runtime", bundle / ".onto-runtime")
        shutil.copytree(tmp_path / "desired" / "auth", bundle / ".onto-auth")
    else:
        shutil.copytree(
            tmp_path / "desired" / "runtime", volume_root / "consumer-runtime-data"
        )
        shutil.copytree(
            tmp_path / "desired" / "auth", volume_root / "consumer-auth-data"
        )

    target_runtime = "consumer-runtime-data"
    target_auth = "consumer-auth-data"
    if target_mode == "volume":
        target_runtime = f"consumer-runtime-data.{operation}-{digest[:12]}"
        target_auth = f"consumer-auth-data.{operation}-{digest[:12]}"
    _write_storage_operation(
        bundle,
        status=phase,
        operation=operation,
        target_mode=target_mode,
        target_runtime_volume=target_runtime,
        target_auth_volume=target_auth,
        source_mode=source_mode,
        source_runtime_volume="consumer-runtime-data",
        source_auth_volume="consumer-auth-data",
        digest=digest,
        backup=backup,
    )

    if operation == "migration":
        result = _run_script(bundle, env, "migrate_storage.sh", "--to", target_mode)
    else:
        result = _run_script(
            bundle,
            env,
            "restore.sh",
            backup.as_posix(),
            "--target-mode",
            target_mode,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    if target_mode == "volume":
        _assert_tree_matches_backup(
            volume_root / target_runtime, volume_root / target_auth, backup
        )
    else:
        _assert_tree_matches_backup(
            bundle / ".onto-runtime", bundle / ".onto-auth", backup
        )
    operation_state = (
        bundle / "deploy" / "state" / "storage-operation.env"
    ).read_text(encoding="utf-8")
    assert "STORAGE_OPERATION_STATUS=complete" in operation_state
