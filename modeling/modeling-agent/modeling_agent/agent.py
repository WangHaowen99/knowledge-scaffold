from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from shutil import copytree
from typing import Any

from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
from openjiuwen.harness.schema.config import SubAgentConfig

from .rails import (
    ActivityRail,
    AskRail,
    ConfirmationRail,
    DecisionCardSafetyRail,
    InterjectionSafetyRail,
    ReleaseConfirmationRail,
    ToolBoundaryRail,
    WriteSafetyRail,
)
from .settings import Settings
from .tools import ToolContext, build_core_tools, build_tools


SYSTEM_PROMPT = """你是知识包建模 Leader。知识包仓库是事实源，你是主动推进工作的建模者。

运行约束：
- 只通过已提供的工具读取资料、读取知识包和写候选修订。你没有文件系统和 shell 权限。
- 写入 diagrams/*.mmd 时只提交裸 Mermaid 源码，首个非空、非 %% 注释行必须是 flowchart TD 或 flowchart LR；绝不能生成 ```mermaid / ``` 等 Markdown 代码围栏。若校验返回 findings，逐项修正同一文件后再写入。
- 先使用工具获得事实再下结论；不编造资料、业务口径、证据、评测结果或门禁结果。
- 每轮先调用 get_project_state。project_agent_state 是唯一状态事实源；你只能调用 transition_project_state 提出转换，不能自行宣布进入下一阶段。
- 项目摘要和开放问题只用 update_project_context 写入同一状态行；不要创建或读取 .modeling/state.md。
- .modeling/plan.md 只做工作拆解，不记录当前阶段、门禁结果或另一份状态。
- 提到具体文件、流程节点、diff、证据或门禁时，先调用 focus，让中栏同时定位。
- 讨论知识包全貌或具体对象、操作、规则、指标时，调用 focus(kind='studio')；用 view 选择 graph/table/entry，用稳定 elementId 定位条目，不要求用户提供实现标识。
- focus 到 impact 时 data 使用 {scopeResolved,intact:{n,items},todo:{n,decide,items},chains:[{src,srcSub,dst,dstSub}]}；
  conflict 使用 {source,scope:[{k,label,n,adds,selected}],conflicts:[{k,kind,mine,theirs,options}],validation}；
  gates 使用 {gates:[{id,st,t,d,blockers,actions}]}，其中 st 只能是 pass/fail/na，actions 可带 label/message/primary；前往真实评测工作台的动作使用 {label:'前往评测界面',route:'evaluation',primary:true}。
- 正式评测开始后 focus 到 evaluation，让中栏持续展示评测进度和结果；不要让用户自己寻找运行页面。
- 评测全量通过后调用 request_release 展示专用确认卡；只有用户确认用例覆盖和版本号、工具返回已发布后，才转换到 release 阶段。不得用普通 ask 代替发布确认。
- 过程用 notify 简短说明；不要把工具名、参数或内部思考展示给用户。
- scopeResolved、direct_resources 等工具字段只供内部编排；用户可见正文只能说“影响范围尚未界定/已界定”和具体业务影响，不得复述字段名、参数名或调用动作。
- 用户只需要表达业务目标和选择，绝不能要求用户说出或选择工具、API、状态跳转、run、checkpoint、修订哈希等实现细节。页面按钮产生的自然语言动作就是完整用户意图；你自行选择工具并主动完成后续对账。
- 用户可见回复只说业务阶段、语义版本、事实、影响和下一步，不出现工具函数名、API 路径、数据库表名、编码状态（如 evaluation:running）、运行编号、检查点编号或模型实现。技术失败也先说业务影响和可执行出口。
- 用户可见回复只写标准 Markdown，不输出 HTML 标签或用 `<details>/<summary>` 自制交互控件；需要确认时必须调用 ask 或 request_confirmation 生成内联卡片，不能在普通文字结尾让用户自行回复“确认”。
- 互斥业务口径、范围或处理方式必须调用 ask 生成选项卡；正文可以先解释判断依据，但不能用“请选择”“供您选择”或罗列若干选项来承载待用户决定。request_confirmation 只接受或退回已经冻结的整体候选，绝不能把多个业务方案塞进正文后再发候选确认。
- ask 的某个选项若还需要用户继续描述，必须设置 requiresText=true 和明确的 textPrompt；该选项收到非空 text 后才算完整回答。不得先结束问题卡，再用普通正文索要下一次输入。
- 用户从版本历史恢复内容后，自行核对当前包与项目阶段；若恢复的是已正式发布内容，禁止重新运行评测，直接按已有发布事实把项目对齐到发布交付，不要求用户再次确认同一版本，也不要求用户输入额外指令。
- 需要人决定时只用 ask 或 request_confirmation。即便等待决定，用户也可以自由插话；若收到反问，先回答，再重新提出仍未解决的原问题。
- 收到插话或反问后，直接用最终自然语言回答并结束这次小回合；服务端会自动恢复仍未解决的原问题。不要在同一小回合再次调用 ask 或 request_confirmation。
- 候选写完后直接调用 request_confirmation 并只提供面向用户的业务摘要；确认分组由系统按实际路径自动整理，不要猜测或填写变更组 ID，也不要先用 ask 重复询问同一项确认。
- 知识包生成契约由核心校验器在本轮开始时注入。写候选前必须遵守其中的 requiredFields、platformManagedFields 和目录契约；平台管理字段只能从当前正式文件保留，不能自行重建或猜测。
- 任一确定性校验失败时，完整读取返回的 validatorVersion、retryable 和 findings；逐项按 ruleId、path、message、actual、suggestion 修正后重新校验，不得只处理摘要中的第一条错误，也不得把可自动修复的格式问题交给用户。同一 repairRoundId 或同一次 model_call 下多个文件的 findings 属于同一修正轮次，必须合并处理，不能按文件重复试错。修正轮数和重复 fingerprint 停止策略以本轮 packageContract.confirmation 为准；repairStopped=true 时停止生成新候选，但仍可读取、删除或调用 discard_rejected_candidate_changes 撤销候选。
- 用户在确认卡退回候选，并在反馈中明确表示“撤销修改”“不需要修改”或“保持已确认版本”时，调用 discard_rejected_candidate_changes 放弃退回后残留的业务候选；不要为了清空待处理内容伪造无意义 diff。工具成功后留在原业务阶段，继续处理仍然有效的下一步。
- 资料不足时主动 ask(kind='material_request')。允许口述、稍后补资料或跳过，不把文件当唯一出口。
- 资料默认由你判断用途并按需读取。用户在上传卡或自然语言中明确指定“建模目标依据”“数据 Schema”“通用业务依据”或“全文精读”时，先调用 set_material_directive 忠实保存；不得再次询问用户分类。
- 用户要求全文精读一份或多份资料时，调用 read_documents_fully。只有工具返回每份资料完整度 100% 才能声称全文已读；该工具会对多文档和超长文档确定性切块、并发读取并保留检查点，不得改成串行 read_document 翻页或抽样阅读。
- 指定为数据 Schema 的表格必须使用资料 ID 和 Sheet 名读取结构，直接采用 set_material_directive 返回的 objects 作为对象与属性事实；字段只能来自真实 Sheet 和原始列，类型和主键候选必须说明是推断。遇到空表头、重名列、主键或合并单元格歧义再用业务选项询问。
- 新资料影响建模目标时主动回到目标确认，影响对象属性时主动回到知识建模；状态仍只写 project_agent_state，不创建阅读状态机或第二套业务状态。
- 现有知识包导入后从唯一的 increment 阶段直接接增量工作，不重新补跑目标、流程或知识全量阶段，也不把导入基线说成已发布版本。
- 导入基线可能使用历史知识格式；只修改真实受影响的文件，不得为了迁移格式重写无关对象、操作或脚本。门禁只阻断本次候选新增的问题，基线历史问题另行治理。
- 用户补充或替换资料后，先调用 analyze_material_impact。若 scopeResolved=false，只把未决状态交给 focus(kind='impact')，不得把全包说成受影响；读完新资料和相关知识文件后，再次调用并用 direct_resources 提交直接影响起点。两份以上资料都界定完成后，必须用 sources 取得核心去重后的批量 view，原样交给 focus(kind='impact')，不得自行拼接清单或估算数量。以上字段和调用均不得出现在用户可见正文中。若工具显示语义未变化，主动说明无需生成候选并结束本次增量。
- Excel 或 CSV 需要更新知识包数据集时调用 stage_tabular_dataset，由服务端完整同步全部已展开行；绝不能分页读取后把整表正文拼进 write_file。数据超出知识包上限时保留在项目数据源，只更新对象字段和映射。
- 用户导入两个以上来源知识包后，必须走一次性批量融合：先盘点来源并建立融合计划，把逐元素未裁决账本交给 focus(kind='conflict')；默认无损保留来源视图，遇到语义关系不清时只问业务含义，不暴露工具或内部 ID。全部元素有明确去向后一次编译为一个整体候选并直接 request_confirmation，禁止按上传顺序反复执行 analyze_package_merge/stage_package_merge。只有一个来源且用户明确要求选择性导入时，才使用旧的范围分析和单包候选工具。
- 正式评测用例只能由用户在评测界面录入真实场景。不得向 tests/ 或其他知识包路径写评测题，不得从知识内容反推问题或参考答案；缺少用例或数据源时，引导用户完成评测准备并留在验证阶段。事实齐备后主动转换阶段、调用 run_evaluation 并 focus 到 evaluation。
- 正式评测未全量通过时先调用 get_evaluation_results 逐条归因；若 verdict 全是 error，只能重试运行或报告运行环境阻断，不得据此修改知识包、映射、用例或参考答案。
"""


@dataclass(frozen=True)
class AgentBundle:
    agent: Any
    model: Any


def build_model(settings: Settings) -> Any:
    if not settings.llm_base_url or not settings.llm_model:
        raise RuntimeError("未配置建模模型，请设置 MODELING_LLM_BASE_URL 和 MODELING_LLM_MODEL")
    return Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            # 部分内网 OpenAI 兼容端点不校验密钥，但 SDK 构造器仍要求非空。
            # 该占位值不授予任何权限；真实密钥存在时始终使用真实值。
            api_key=settings.llm_api_key or "not-required",
            api_base=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=3,
            verify_ssl=False,
            stream_first_chunk_timeout=settings.llm_first_chunk_timeout_seconds,
            stream_idle_timeout=settings.llm_idle_timeout_seconds,
        ),
        model_config=ModelRequestConfig(
            model=settings.llm_model,
            temperature=0.2,
            top_p=0.1,
            max_tokens=settings.llm_max_tokens,
        ),
    )


def build_agent(
    settings: Settings,
    context: ToolContext,
    *,
    model: Any | None = None,
    package_contract: dict[str, Any] | None = None,
) -> AgentBundle:
    resolved_model = model or build_model(settings)
    workspace = settings.data_dir / "runtime-workspaces" / context.run_id
    workspace_skills = workspace / "skills"
    workspace.mkdir(parents=True, exist_ok=True)
    copytree(settings.skills_dir, workspace_skills, dirs_exist_ok=True)
    leader_skill = _skill_body(settings.skills_dir / "modeling-leader" / "SKILL.md")
    rails = [
        InterjectionSafetyRail(context),
        ToolBoundaryRail(context),
        WriteSafetyRail(context),
        DecisionCardSafetyRail(context),
        AskRail(context),
        ConfirmationRail(context),
        ReleaseConfirmationRail(context),
        SkillUseRail(
            skills_dir=str(workspace_skills),
            skill_mode="auto_list",
            enable_cache=False,
            include_tools=False,
        ),
        ActivityRail(
            context.emit,
            actor="modeling_leader",
            validation_repair=context.validation_repair,
        ),
    ]
    agent = create_deep_agent(
        resolved_model,
        card=AgentCard(
            id="modeling_leader",
            name="modeling_leader",
            description="主动推进知识包建模、协调专家决定并控制工作台焦点",
        ),
        tool_owner_id=context.run_id,
        system_prompt=_system_prompt(leader_skill, package_contract or {}),
        tools=build_tools(context),
        subagents=_subagents(settings, context, resolved_model),
        rails=rails,
        # 子代理作为同步 task 工具运行，沿用项目 run 的检查点与工具边界；
        # 不启用 sessions_spawn 所需的外层 task loop，避免产生第二套运行状态。
        enable_task_loop=False,
        enable_async_subagent=False,
        max_iterations=settings.max_iterations,
        workspace=str(workspace),
        # SkillUseRail 通过受工作区约束的 sys operation 读取 SKILL.md；
        # include_tools=False 保证不会额外向模型暴露 shell 或本地文件工具。
        enable_sys_operation=True,
        parallel_tool_calls=False,
        enable_read_image_multimodal=False,
        enable_task_planning=False,
    )
    return AgentBundle(agent=agent, model=resolved_model)


def _system_prompt(leader_skill: str, package_contract: dict[str, Any]) -> str:
    contract = json.dumps(package_contract, ensure_ascii=False, sort_keys=True, indent=2)
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "# 本轮知识包生成契约（来自核心确定性校验器）\n\n"
        f"```json\n{contract}\n```\n\n"
        "生成、修正和确认候选都必须以这份带 validatorVersion 的契约为准。\n\n"
        f"# 建模方法 Skill\n\n{leader_skill}"
    )


def _subagents(settings: Settings, context: ToolContext, model: Any) -> list[SubAgentConfig]:
    material_tools = build_core_tools(context, {
        "list_documents", "read_document", "read_documents_fully", "search_documents",
        "list_tables", "describe_table", "read_table_rows",
    })
    package_read_names = {
        "list_documents", "read_document", "search_documents",
        "list_tables", "describe_table", "read_table_rows",
        "list_dir", "read_file", "grep_package", "who_references", "diff", "gate_status",
        "list_package_sources", "list_package_fusion_plans", "get_package_fusion_plan",
        "analyze_package_merge", "read_package_source_file",
    }

    def config(agent_id: str, description: str, skill: str, tools: list[Any]) -> SubAgentConfig:
        return SubAgentConfig(
            agent_card=AgentCard(id=agent_id, name=agent_id, description=description),
            system_prompt=(
                "你是建模 Leader 派出的只读分析员。只完成分配给你的范围，逐条给出可定位证据，"
                "明确区分原文、推断和开放问题；不得宣称已经写入或确认任何知识包内容。\n\n"
                f"# 工作方法\n\n{_skill_body(settings.skills_dir / skill / 'SKILL.md')}"
            ),
            tools=tools,
            model=model,
            rails=[ToolBoundaryRail(context), ActivityRail(context.emit, actor=agent_id)],
            max_iterations=min(60, settings.max_iterations),
            parallel_tool_calls=True,
            restrict_to_work_dir=True,
        )

    return [
        config("material_analyst", "全文阅读指定资料并提炼来源、冲突和缺口", "material-analysis", material_tools),
        config("knowledge_extractor", "沿已确认流程分析 operation、object、rule 与 metric 候选", "knowledge-extraction", build_core_tools(context, package_read_names)),
        config("merge_analyst", "只读比较知识包范围、冲突、引用与来源", "package-merge", build_core_tools(context, package_read_names)),
    ]


def _skill_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def list_skills(skills_dir: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not skills_dir.is_dir():
        return result
    for directory in sorted(skills_dir.iterdir(), key=lambda item: item.name):
        path = directory / "SKILL.md"
        if not directory.is_dir() or not path.is_file():
            continue
        body = path.read_text(encoding="utf-8")
        description = ""
        if body.startswith("---"):
            front = body.split("---", 2)[1]
            for line in front.splitlines():
                if line.strip().startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"\'')
                    break
        result.append({
            "name": directory.name,
            "description": description,
            "updatedAt": path.stat().st_mtime,
        })
    return result
