"""发布就绪（ADR-0103）：校验之后按发布检查同一份代码再查一遍，会挡发布的问题交给助手修。

只看「内容做错了」的门禁：求解契约、知识包结构（渲染时的结构问题、consumer 导入规则、发布树安全）、包声明的门禁。
数据绑定不在内（发布时对不上的对象自动降级，不挡发布）；建模阶段、流程关卡是「没做完」，由流程管，也不在内。

修过几轮还在的问题写进 `reports/publish-check.md`（平台维护，每条一个 `[ ]`）：报告里有待处理项，校验节点就不算完成，
待处理里能看到；之后的校验查干净了，平台删掉这份清单。
"""

from __future__ import annotations

import logging
import re

from ..workspace.errors import WorkspaceError

LEFTOVER_PATH = "reports/publish-check.md"
# 陪伴建模里校验之后平台代发修复的轮数（自主构建按策略里的 max_fix_rounds）
ROUNDS = 2
VALIDATE_NODE = "core.validate"
PROMPT_LIMIT = 20
LEFTOVER_LIMIT = 100

_log = logging.getLogger(__name__)


def findings_at(manager, project_id: str, treeish: str) -> list[dict]:
    """treeish（提交或树对象）上会挡发布、降级也绕不过的内容问题（Finding 列表，空表示就绪）。"""
    from . import gates
    from .service import content_checks

    root = manager.project_root(project_id)
    parts = content_checks(manager, project_id, root, treeish)
    chain = gates.gate_chain(
        [], parts["solver"], parts["script_findings"] + parts["structure"]["findings"], parts["tree_error"],
        parts["pack_gates"], deterministic=parts["deterministic"],
    )
    out: list[dict] = []
    for gate in gates.blocking(chain, degrade=True):  # 降级只放宽数据绑定
        found = [item for item in gate.get("findings") or [] if item.get("level", "error") == "error"]
        out.extend(found or [{"code": gate["id"], "level": "error", "message": gate.get("summary") or gate["title"]}])
    return out


def mainline_findings(manager, project_id: str) -> list[dict] | None:
    """主线最新提交上的问题；还没有提交、或检查跑不起来时返回 None（不当成就绪，也不当成有问题）。"""
    from .service import resolve_commit

    try:
        commit = resolve_commit(manager.project_root(project_id), None)
        return findings_at(manager, project_id, commit)
    except Exception:  # noqa: BLE001 - 检查跑不起来不挡建模，记一笔
        _log.warning("项目 %s 的发布就绪检查没跑起来", project_id, exc_info=True)
        return None


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _where(item: dict) -> str:
    path = item.get("path") or ""
    if path and item.get("line"):
        return f"{path} 第 {item['line']} 行"
    return path


def _line(item: dict) -> str:
    where = _where(item)
    message = _flat(item.get("message") or item.get("code"))
    return f"{message}（{where}）" if where else message


def fix_prompt(findings: list[dict]) -> str:
    """交给助手修的话：发布检查查出的问题不限这一步写的内容，要业务判断的问用户。"""
    lines = [f"- {_line(item)}" for item in findings[:PROMPT_LIMIT]]
    more = f"\n- 另有 {len(findings) - PROMPT_LIMIT} 个，修完这些后会再查" if len(findings) > PROMPT_LIMIT else ""
    return (
        f"发布检查发现 {len(findings)} 个会挡住发布的问题，逐个修掉，改到哪个文档都可以：\n"
        + "\n".join(lines) + more
        + "\n要业务判断才能定的，用确认卡问，选项里给「从文档里删掉这一条」。不要改 "
        + f"`{LEFTOVER_PATH}`，它由平台维护。"
    )


def leftover_text(findings: list[dict]) -> str:
    rows = [f"- [ ] ❌ [发布检查] {_line(item)}" for item in findings[:LEFTOVER_LIMIT]]
    if len(findings) > LEFTOVER_LIMIT:
        rows.append(f"- [ ] ❌ [发布检查] 另有 {len(findings) - LEFTOVER_LIMIT} 个问题，修掉上面的后重新校验会列出")
    return "# 发布检查\n\n校验之后按发布规则检查，下面的问题修过还在，会挡住发布。修好后重新校验，这份清单由平台更新。\n\n" \
        + "\n".join(rows) + "\n"


def record_leftover(manager, project_id: str, findings: list[dict], actor: str) -> str | None:
    """把还没修掉的问题写进 `reports/publish-check.md`，没有了就删掉；内容不变不提交。返回提交号。"""
    from ...kernel.writepath import ChangeTx, Origin

    path = manager.project_root(project_id) / LEFTOVER_PATH
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    wanted = leftover_text(findings) if findings else None
    if current == wanted:
        return None
    message = (f"chore: 发布检查还有 {len(findings)} 个问题没修掉" if wanted
               else "chore: 发布检查通过，移除待处理清单")
    try:
        with ChangeTx(manager, project_id, Origin("plugin", actor or "system"), summary="发布检查") as tx:
            if wanted is None:
                tx.remove(LEFTOVER_PATH)
            else:
                tx.write(LEFTOVER_PATH, wanted.encode("utf-8"))
            return tx.commit(message).commit
    except WorkspaceError:
        _log.warning("项目 %s 的发布检查清单没写进去", project_id, exc_info=True)
        return None


def is_validate(record: dict | None) -> bool:
    """变更集或执行记录属于校验这一步。"""
    if not record:
        return False
    node = record.get("node")
    node_id = node.get("id") if isinstance(node, dict) else node
    return node_id == VALIDATE_NODE or VALIDATE_NODE in (record.get("nodes") or [])
