"""I1 审查修复第 4 条：恢复演练在最后一个项目抽检期间被取消，任务停在「已取消」，演练跑完也不改回成功、不记演练记录。"""

import tarfile
import threading
from pathlib import Path

from app import config
from app.services import backup, ops_center


def test_cancel_during_last_check_stays_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backups"))
    (tmp_path / "backups").mkdir()
    src = tmp_path / "src" / "data" / "workspaces" / "task-1"
    src.mkdir(parents=True)
    (src / ".onto-web.json").write_text('{"id": "task-1"}', encoding="utf-8")
    name = "onto-modeling-backup-20261102-000000.tar.gz"
    with tarfile.open(tmp_path / "backups" / name, "w:gz") as tar:
        tar.add(tmp_path / "src" / "data", arcname="data")

    entered, release = threading.Event(), threading.Event()

    def slow_check(root):
        entered.set()
        release.wait(5)
        return None

    monkeypatch.setattr(ops_center, "_check_project", slow_check)
    task = ops_center.start_drill(name, 1, "ops1")
    worker = threading.Thread(target=ops_center.run_drill, args=(task["id"], name, 1))
    worker.start()
    assert entered.wait(5)
    cancelled = ops_center.act(task["id"], "cancel", "ops1")
    print("after cancel:", cancelled["status"])
    release.set()
    worker.join(5)
    final = ops_center.get_task(task["id"])
    print("final:", final["status"], "| drills recorded:", len(ops_center.list_drills()))
    assert cancelled["status"] == "cancelled" and final["status"] == "cancelled"
    assert ops_center.list_drills() == [] or all(item["id"] != task["id"] for item in ops_center.list_drills())
