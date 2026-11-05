import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_SCRIPT = REPO_ROOT / "modeling" / "onto-modeling" / "deploy" / "migrate-from-modeling.sh"

# docker stub：容器内路径映射到 $STUB_DOCKER_ROOT/<容器名>/<路径>；exec node 用本机 node 执行，
# 迁移程序（exec python3）只把收到的数据目录留存到 $STUB_MIGRATED 供断言。
_MIGRATE_DOCKER_STUB = """\
root="${STUB_DOCKER_ROOT}"
map_path() {
    case "$1" in
        *:/*) printf '%s/%s%s' "${root}" "${1%%:*}" "${1#*:}" ;;
        *) printf '%s' "$1" ;;
    esac
}
case "$1" in
    cp)
        target="$(map_path "$3")"
        mkdir -p "$(dirname "${target}")"
        cp -R "$(map_path "$2")" "${target}"
        ;;
    exec)
        container="$2"
        shift 2
        command_name="$1"
        shift
        args=()
        for arg in "$@"; do
            case "${arg}" in
                /data/*|/tmp/*) args+=("${root}/${container}${arg}") ;;
                *) args+=("${arg}") ;;
            esac
        done
        case "${command_name}" in
            node) node "${args[@]}" ;;
            rm) rm "${args[@]}" ;;
            python3) cp -R "${root}/${container}/tmp/modeling-data" "${STUB_MIGRATED}" ;;
            *) exit 1 ;;
        esac
        ;;
    *) exit 1 ;;
esac
"""


def _node_has_sqlite() -> bool:
    node = shutil.which("node")
    if node is None:
        return False
    # 安全审查：argv 是 PATH 中解析出的 node 绝对路径加字面量参数，以序列传入、不经 shell 解析
    probe = subprocess.run(  # NOSONAR(S4721)
        [node, "--no-warnings", "--experimental-sqlite", "-e", "require('node:sqlite')"],
        capture_output=True,
        check=False,
        shell=False,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _node_has_sqlite(), reason="需要带 node:sqlite 的 Node 22+")
def test_migration_exports_committed_rows_still_in_wal(tmp_path: Path) -> None:
    """回归：Workbench 运行中时已提交数据还在 modeling.db-wal 里，只复制主文件会报 no such table: projects。"""
    root = tmp_path / "containers"
    data_dir = root / "mw-modeling" / "data"
    (data_dir / "artifacts" / "pj_demo" / "package").mkdir(parents=True)
    (data_dir / "artifacts" / "pj_demo" / "package" / "index.md").write_text("# demo\n", encoding="utf-8")
    (root / "mw-modeling" / "tmp").mkdir()
    (root / "onto-modeling" / "tmp").mkdir(parents=True)

    # 保持连接打开模拟运行中的 Workbench：最后一个连接关闭前 WAL 不会合并回主文件
    workbench = sqlite3.connect(data_dir / "modeling.db")
    try:
        workbench.execute("PRAGMA journal_mode = WAL")
        workbench.execute(
            "CREATE TABLE projects (id TEXT, owner TEXT, name TEXT, domain TEXT, background TEXT)"
        )
        workbench.execute("INSERT INTO projects VALUES ('pj_demo', 'demo-user', '示例项目', '', '')")
        workbench.commit()
        main_file_only = tmp_path / "main-only.db"
        shutil.copyfile(data_dir / "modeling.db", main_file_only)
        assert sqlite3.connect(main_file_only).execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'projects'"
        ).fetchone() == (0,)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "docker"
        stub.write_text(f"#!/bin/bash\nset -euo pipefail\n{_MIGRATE_DOCKER_STUB}", encoding="utf-8")
        stub.chmod(0o755)
        migrated = tmp_path / "migrated"
        workdir = tmp_path / "work"
        workdir.mkdir()
        script = tmp_path / MIGRATE_SCRIPT.name
        shutil.copyfile(MIGRATE_SCRIPT, script)
        script.chmod(0o755)

        # 安全审查：argv 只有复制到 tmp_path 的仓库自带迁移脚本，以序列传入、不经 shell 解析；
        # 它调用的 docker 是本测试写入 tmp_path 的替身。
        result = subprocess.run(  # NOSONAR(S4721)
            [str(script)],
            cwd=workdir,
            env={
                "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
                "STUB_DOCKER_ROOT": str(root),
                "STUB_MIGRATED": str(migrated),
            },
            text=True,
            capture_output=True,
            check=False,
            shell=False,
        )
    finally:
        workbench.close()

    assert result.returncode == 0, result.stdout + result.stderr
    rows = sqlite3.connect(migrated / "modeling.db").execute("SELECT id, name FROM projects").fetchall()
    assert rows == [("pj_demo", "示例项目")]
    assert (migrated / "artifacts" / "pj_demo" / "package" / "index.md").is_file()
    # 快照是临时文件，迁移后不留在 Workbench 容器里
    assert not list((root / "mw-modeling" / "tmp").glob("onto-migrate-*"))
    assert not list(workdir.iterdir())
