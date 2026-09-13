# Modeling Agent

面向新工作台的独立 openJiuwen Agent 运行时。它是直接开发的新后端，不桥接或调用旧 `modeling-agent` 运行时。

职责边界：

- Python 进程持有 Agent、Skill、Rail、session/checkpoint、插话队列和持久化事件流。
- Python 进程在 `project_agent_state` 中为每个项目只保存一份当前状态；阶段与执行状态合并为一个 `state` 值。
- Node modeling core 持有项目鉴权、资料、候选工作区、Git 修订、确认审计和确定性门禁。
- Agent 没有宿主文件系统和 shell 工具；知识包写入只能经过 core 的候选 API。
- 对象、规则、操作和对象数据绑定通过领域工具更新结构化规格，由 core 确定性生成详情、索引和映射；`upsert_object` 可在创建对象时一次完成数据绑定。
- 大型校验报告完整保存在 core，Agent 主上下文只接收有界摘要并按需分页读取 findings。

运行时使用 `create_deep_agent`，并通过 openJiuwen 扩展点实现：

- `SkillUseRail`：按情形加载资料分析、知识抽取、增量资料、包融合和失败修复方法。
- `WriteSafetyRail`：在工具边界阻止不安全路径和不可逆写入。
- `AskRail` / `ConfirmationRail`：挂起、恢复、自由插话和候选确认。
- `ActivityRail`：把进度和工具轨迹写入同一事件流。

```bash
docker build --target test -t modeling-agent-test .
docker build --target runtime -t modeling-agent .
docker run --rm -p 5110:5110 --env-file .env modeling-agent
```

编排部署时，Agent 通过受 `MODELING_SERVICE_TOKEN` 保护的内部接口按当前账号读取 Modeling 平台运行时 LLM 配置，admin 可在 Workbench 配置中心统一设置，无需重启 Agent。`MODELING_LLM_BASE_URL` 和 `MODELING_LLM_MODEL` 仅作为本地独立运行兜底。`MODELING_CORE_BASE_URL` 默认指向 `http://127.0.0.1:5100`；容器编排中使用 `http://modeling:5100`。

运行事实、项目唯一状态和候选自动修复状态保存在 `runtime.sqlite`，openJiuwen checkpoint 的物理文件为 `checkpoints.sqlite.db`。服务重启会把唯一状态从 `*:running` 转为 `*:suspended`，不会伪造完成状态，也不会清空同一候选的修复轮次。

模型预算配置：`MODELING_GOAL_OUTPUT_TOKENS` 控制目标和流程候选的单次输出（默认 8192），`MODELING_ANALYST_OUTPUT_TOKENS` 控制资料分段分析（默认 8192），`MODELING_SUMMARY_OUTPUT_TOKENS` 仅用于空正文恢复摘要（默认 1024）。目标和资料分析使用独立预算，避免结构化结果被短摘要额度截断。
