from __future__ import annotations

from pathlib import Path

from modeling_agent.service import (
    PRODUCT_LEDGER_STATE_TOOLS,
    LiveRun,
)
from modeling_agent.schemas import RUN_QUEUED, RunRecord
from modeling_agent.settings import Settings
from modeling_agent.store import RuntimeStore

from conftest import ScriptedModel, service


def _run(run_id: str = "run_ledger", project_id: str = "project_demo") -> RunRecord:
    return RunRecord(
        id=run_id,
        project_id=project_id,
        session_id=f"project:{project_id}",
        status=RUN_QUEUED,
        prompt="盘点产品清单",
        token_fingerprint="000000000000",
    )


def test_tool_receipts_by_name_only_returns_requested_tools_in_completion_order(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    try:
        store.create_run(_run())
        store.save_tool_receipt("run_ledger", "call-1", "begin_product_ledger", {}, {"ledgerId": "ledger-a"})
        store.save_tool_receipt("run_ledger", "call-2", "read_document", {}, {"ok": True})
        store.save_tool_receipt("run_ledger", "call-3", "append_product_ledger_batch", {}, {"lastSuccessfulBatch": 2})

        receipts = store.tool_receipts_by_name("run_ledger", PRODUCT_LEDGER_STATE_TOOLS)
        assert [name for name, _ in receipts] == ["begin_product_ledger", "append_product_ledger_batch"]
        assert receipts[1][1]["lastSuccessfulBatch"] == 2
        assert store.tool_receipts_by_name("run_ledger", []) == []
        assert store.tool_receipts_by_name("run_missing", PRODUCT_LEDGER_STATE_TOOLS) == []
    finally:
        store.close()


def test_restore_product_ledger_situation_replays_persisted_batch_progress(settings: Settings) -> None:
    agent = service(settings, ScriptedModel([]))
    agent.store.create_run(_run())
    agent.store.save_tool_receipt("run_ledger", "call-1", "begin_product_ledger", {}, {
        "ledgerId": "ledger-a", "analysisRevision": "analysis-1", "status": "draft",
    })
    agent.store.save_tool_receipt("run_ledger", "call-2", "append_product_ledger_batch", {}, {
        "lastSuccessfulBatch": 3,
    })
    agent.store.save_tool_receipt("run_ledger", "call-3", "get_product_ledger_progress", {}, {
        "processed": 120, "remaining": 80,
    })

    live = LiveRun(run_id="run_ledger", project_id="project_demo", token="token")
    # 恢复前只知道"调用过哪些工具"，业务结果全在回执里。
    assert live.situation.product_ledger_status in (None, "", "unknown")
    agent._restore_product_ledger_situation(live)

    assert live.situation.product_ledger_id == "ledger-a"
    assert live.situation.product_ledger_analysis_revision == "analysis-1"
    assert live.situation.product_ledger_last_batch == 3
    assert live.situation.product_ledger_processed == 120
    assert live.situation.product_ledger_remaining == 80


def test_restore_product_ledger_situation_keeps_latest_finalized_counts(settings: Settings) -> None:
    agent = service(settings, ScriptedModel([]))
    agent.store.create_run(_run())
    agent.store.save_tool_receipt("run_ledger", "call-1", "get_product_ledger_progress", {}, {
        "processed": 10, "remaining": 190,
    })
    agent.store.save_tool_receipt("run_ledger", "call-2", "finalize_product_ledger", {}, {
        "ledgerId": "ledger-a", "status": "finalized",
        "validation": {"pass": True, "counts": {
            "raw": 200, "canonical": 198, "mergedGroups": 2, "complete": 198, "insufficient": 0,
        }},
    })

    live = LiveRun(run_id="run_ledger", project_id="project_demo", token="token")
    agent._restore_product_ledger_situation(live)

    # 回执按完成顺序重放，收尾结论不能被更早的中途进度盖回去。
    assert live.situation.product_ledger_status == "finalized"
    assert live.situation.product_ledger_counts == {
        "raw": 200, "canonical": 198, "mergedGroups": 2, "complete": 198, "insufficient": 0,
    }
    assert live.situation.product_ledger_processed == 10
