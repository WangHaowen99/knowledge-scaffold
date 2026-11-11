"""外部反馈批次的写入（ADR-0020、ADR-0092）：落盘与提交、幂等、校验、附件。

原先经 X-05 接口验证的行为，X-05 退役后改在写入函数上验证（评测优化走同一个写入函数）；
工作区、用户名与反馈内容全部为合成数据。
"""

import json
from pathlib import Path

import pytest

from app import config
from app.services import feedback_inbox, publish
from app.services.workspace import WorkspaceManager, _git


@pytest.fixture()
def project(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    manager = WorkspaceManager(tmp_path / "data", assets)
    pid = manager.create_project("反馈回归", owner="operator_a")["id"]
    return manager, pid, manager.project_root(pid)


def feedback_body(**fields) -> dict:
    return {
        "feedback_id": "round-001",
        "source": {"service": "合成服务", "run": "run-7"},
        "summary": {"score": 72.5, "total": 2, "fail": 1, "error": 1},
        "items": [
            {"case_id": "c1", "question": "合成问题一？", "expected": "合成答案一", "actual": "合成回答一",
             "loss_reason": "缺少合成概念", "suspected_artifacts": ["ontology/object/订单.md"],
             "tool_failures": ["query · TABLE_NOT_FOUND"]},
            {"case_id": "c2", "question": "合成问题二？", "verdict": "error"},
        ],
        **fields,
    }


def test_feedback_lands_in_workspace_and_commits_only_its_file(project):
    manager, pid, root = project
    (root / "ontology" / "rule").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "rule" / "未提交.md").write_text("# 未提交\n", encoding="utf-8")
    result, created = feedback_inbox.receive(manager, pid, feedback_body(), "operator_a")
    assert created
    assert result["path"].startswith("feedback/") and result["path"].endswith("-round-001.json")
    assert result["item_count"] == 2 and result["verdicts"] == {"fail": 1, "partial": 0, "error": 1}
    assert result["received_at"].endswith("Z") and result["source"] == {"service": "合成服务", "run": "run-7"}

    stored = json.loads((root / result["path"]).read_text(encoding="utf-8"))
    assert stored["format"] == feedback_inbox.FORMAT and stored["received_by"] == "operator_a"
    assert stored["items"][0]["tool_failures"] == ["query · TABLE_NOT_FOUND"]
    assert stored["items"][0]["verdict"] == "fail"  # 缺省按失分处理
    assert "attachments" not in stored
    changed = _git(root, "-c", "core.quotePath=false", "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert changed == [result["path"]]
    assert "ontology/rule/未提交.md" in _git(root, "-c", "core.quotePath=false", "status", "--porcelain")
    assert feedback_inbox.latest_batch(root) == result["path"]
    assert feedback_inbox.batch_path(root, "round-001") == result["path"]
    assert feedback_inbox.batch_path(root, "round-404") is None


def test_feedback_id_takes_effect_once(project):
    manager, pid, root = project
    first, _ = feedback_inbox.receive(manager, pid, feedback_body(), "operator_a")
    head = _git(root, "rev-parse", "HEAD")
    repeat, created = feedback_inbox.receive(manager, pid, feedback_body(summary={"score": 1}), "operator_a")
    assert not created and repeat["path"] == first["path"]
    assert _git(root, "rev-parse", "HEAD") == head
    assert len(list((root / "feedback").glob("*.json"))) == 1


def test_feedback_release_version_must_exist(project, monkeypatch):
    manager, pid, _ = project
    monkeypatch.setattr(publish, "releases", lambda manager, pid: [{"version": "0.1.3"}])
    with pytest.raises(feedback_inbox.FeedbackRejected) as missing:
        feedback_inbox.receive(manager, pid, feedback_body(release_version="0.1.99"), "operator_a")
    assert missing.value.status == 422 and missing.value.code == "feedback_invalid"
    result, _ = feedback_inbox.receive(manager, pid, feedback_body(release_version="0.1.3"), "operator_a")
    assert result["release_version"] == "0.1.3"


@pytest.mark.parametrize("body", [
    [],
    {"source": {"service": "s"}, "items": [{"case_id": "c", "question": "q"}]},
    {"feedback_id": "f", "items": [{"case_id": "c", "question": "q"}]},
    {"feedback_id": "f", "source": {"service": "s"}, "items": []},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"case_id": "c", "question": ""}]},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"question": "q"}]},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"case_id": "c", "question": "q", "verdict": "pass"}]},
    {"feedback_id": "../escape", "source": {"service": "s"}, "items": [{"case_id": "c", "question": "q"}]},
    {"feedback_id": "f", "source": {"service": "s"}, "release_version": "latest", "items": [{"case_id": "c", "question": "q"}]},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"case_id": "c", "question": "q"}] * 201},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"case_id": "c", "question": "q"}],
     "attachments": {"report": "../outside.md"}},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"case_id": "c", "question": "q"}],
     "attachments": {"report": "ontology/object/订单.md"}},
    {"feedback_id": "f", "source": {"service": "s"}, "items": [{"case_id": "c", "question": "q"}],
     "attachments": {"url": "reports/a.md"}},
])
def test_invalid_feedback_is_rejected(project, body):
    manager, pid, root = project
    with pytest.raises(feedback_inbox.FeedbackRejected) as rejected:
        feedback_inbox.receive(manager, pid, body, "operator_a")
    assert rejected.value.status == 422 and rejected.value.code == "feedback_invalid"
    assert rejected.value.details["errors"]
    assert not (root / "feedback").exists()


def test_batches_of_an_evaluation_carry_attachments_and_may_be_empty():
    attachments = {"report": "reports/eval-20261031-101010-abc123.md",
                   "tables": ".ontology-state/eval-service/tables/eval-20261031-101010-abc123"}
    document = feedback_inbox.normalize(feedback_body(items=[], attachments=attachments), allow_empty=True)
    assert document["items"] == [] and document["attachments"] == attachments
    with pytest.raises(feedback_inbox.FeedbackRejected):
        feedback_inbox.normalize(feedback_body(items=[]))
