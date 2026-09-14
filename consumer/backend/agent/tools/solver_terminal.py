# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Deterministic Agent termination for public solver results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any

from agent.tools.base import ToolOutcome


_CODE_ALIASES = {
    "verification_runtime_failed": "verification_failed",
}
_ERROR_MESSAGES = {
    "agent_model_timeout": "模型连接超时，求解请求尚未执行。",
    "solver_unavailable": "求解服务暂不可用。",
    "solver_timed_out": "求解服务执行超时。",
    "fact_snapshot_invalid": "求解所需事实快照无效。",
    "knowledge_package_solver_contract_incompatible": (
        "知识包求解器契约版本不兼容，请升级或重新导入知识包。"
    ),
    "solver_contract_drift": "求解契约已变化，本次请求已停止。",
    "solver_result_rejected": "求解结果未通过发布校验。",
    "verification_failed": "求解结果独立验证失败。",
    "input_alias_conflict": "求解输入中的等价字段存在冲突。",
    "invalid_solver_input": "求解输入无效。",
    "artifact_persist_failed": "求解结果工件保存失败。",
    "quality_requirement_not_met": "求解结果未达到要求的质量等级。",
}
_NONTERMINAL_STATUSES = {
    "accepted",
    "pending",
    "queued",
    "running",
    "submitted",
    "waiting",
    "waiting_solver",
}


@dataclass(frozen=True)
class SolverTerminalOutcomeProjector:
    def project(self, payload: Mapping[str, Any]) -> ToolOutcome:
        if not isinstance(payload, Mapping):
            return self.error(
                code="solver_result_rejected",
                phase="publish",
                retryable=False,
            )
        public = {str(key): value for key, value in payload.items()}
        raw_code = str(public.get("code") or "").strip()
        if raw_code:
            public["code"] = _CODE_ALIASES.get(raw_code, raw_code)
        content = json.dumps(
            public,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        status = str(public.get("status") or "").strip().lower()
        authoritative = public.get("authoritative") is True
        if status in _NONTERMINAL_STATUSES and not authoritative:
            return ToolOutcome(
                content=content,
                state_updates={"results": {"solver": public}},
            )
        final_output = self._final_output(public)
        return ToolOutcome(
            content=content,
            state_updates={
                "final_output": final_output,
                "results": {
                    "output": final_output,
                    "solver": public,
                },
            },
            terminal=True,
        )

    def error(
        self,
        *,
        code: str,
        phase: str,
        retryable: bool,
        run_id: str = "",
        job_id: str = "",
    ) -> ToolOutcome:
        payload: dict[str, Any] = {
            "status": "error",
            "code": code,
            "phase": phase,
            "retryable": retryable,
        }
        if run_id:
            payload["run_id"] = run_id
        if job_id:
            payload["job_id"] = job_id
        return self.project(payload)

    @staticmethod
    def _final_output(payload: Mapping[str, Any]) -> str:
        summary = payload.get("summary")
        if isinstance(summary, Mapping):
            rendered = summary.get("text")
            if isinstance(rendered, str) and rendered.strip():
                return rendered.strip()
        if payload.get("authoritative") is True:
            feasibility = str(payload.get("feasibility") or "").strip()
            if feasibility == "infeasible":
                return "求解完成：独立验证已证明当前问题不可行。"
            candidate_id = ""
            if isinstance(summary, Mapping):
                candidate_id = str(summary.get("candidate_id") or "").strip()
            suffix = " 最优候选：{}。".format(candidate_id) if candidate_id else ""
            return "求解完成，结果已通过独立验证。{}".format(suffix).strip()
        status = str(payload.get("status") or "").strip().lower()
        if status == "awaiting_input":
            missing = [
                str(value).strip()
                for value in payload.get("missing") or ()
                if str(value).strip()
            ]
            if missing:
                return "求解需要补充必填参数：{}。".format("、".join(missing))
            return "求解需要补充必填参数。"
        if status == "cancelled":
            return "求解已取消。"
        code = str(payload.get("code") or "solver_result_rejected").strip()
        return _ERROR_MESSAGES.get(code, "求解未完成，错误码：{}。".format(code))


__all__ = ["SolverTerminalOutcomeProjector"]
