# 消费智能体规格

本文记录当前消费智能体运行规格。这些规格属于部署级策略，不在系统设置页展示，也不通过公开系统配置 API 给用户编辑。后端读取优先级为：先读 `.onto-runtime/config/ops_config.json` 的 `limits` section（或 `OPS_CONFIG_PATH` 指定文件），字段缺失时读环境变量，最后使用内置默认值。

## 已实现规格

| 规格项 | 当前值 | 配置字段 | 行为 |
| --- | ---: | --- | --- |
| 用户请求 TPS | 50/s，burst=50 | `ops_config.limits.run_tps` / `AGENT_RUN_TPS` | 单进程全局 token bucket；超限直接返回 HTTP 429 |
| 同时运行请求数 | 普通用户 1，管理员 100 | 现有固定规则 | 保持现有排队语义；等待槽位最多 300 秒，超时返回 HTTP 503 |
| 用户数 | 10 个 | `ops_config.limits.user_max_count` / `USER_MAX_COUNT` | 创建用户时按用户文件中的现有用户总数校验；默认 admin 计入总数 |
| 工作空间数 | 5 个 | `ops_config.limits.workspace_max_count` / `WORKSPACE_MAX_COUNT` | 只限制新增工作空间；不迁移、不删除已有工作空间 |
| 总本体数 | 每 workspace 10 个 | `ops_config.limits.ontology_max_count_per_workspace` / `ONTOLOGY_MAX_COUNT_PER_WORKSPACE` | 导入新本体时按当前 workspace 下本体目录数量校验；覆盖或新增版本不增加本体数 |
| 单本体实体数 | 200 个 | `ops_config.limits.ontology_object_max_count` / `ONTOLOGY_OBJECT_MAX_COUNT` | 导入 converted/source 两类包及编辑器新增对象文件时，按最终 `objects/*.md` 数量校验 |
| 单本体流程数 | 100 个 | `ops_config.limits.ontology_operation_max_count` / `ONTOLOGY_OPERATION_MAX_COUNT` | 导入 converted/source 两类包及编辑器新增流程文件时，按最终 `operations/*.md` 数量校验 |
| 单实体属性数 | 100 个 | `ops_config.limits.entity_attribute_max_count` / `ENTITY_ATTRIBUTE_MAX_COUNT` | 导入本体、编辑器新增或更新对象文件时，按对象 Markdown 的首个属性表行数校验 |
| 模型数量 | 10 个 | `ops_config.limits.model_profile_max_count` / `MODEL_PROFILE_MAX_COUNT` | 保存 `llm_profiles` 时限制模型 profile 数量 |
| 历史会话数 | 每 workspace 下每用户 100 条 | `ops_config.limits.session_max_per_user_workspace` / `SESSION_MAX_PER_USER_WORKSPACE` | 创建新会话后保留最新 100 条，老会话及对应 execution 记录自动老化；不删除长期记忆 |
| 单个逻辑长度 | 15000 字符 | `ops_config.limits.operation_max_chars` / `ONTOLOGY_OPERATION_MAX_CHARS` | 对最终 `operations/*.md` 按字符数限制，不再按行数限制 |
| 输入 Token 长度 | 50000 | `ops_config.limits.model_input_token_limit` / `MODEL_INPUT_TOKEN_LIMIT` | LLM 请求前用轻量估算器按平台硬上限预检，超限拒绝；上下文压缩策略另行设计 |
| 输出 Token 长度 | 100000 | `ops_config.limits.model_output_token_limit` / `MODEL_OUTPUT_TOKEN_LIMIT` | 发送给模型前把请求 `max_tokens` 裁剪到平台硬上限 |
| 普通日志保留 | 180 天 | `ops_config.limits.agent_log_retention_days` / `AGENT_LOG_RETENTION_DAYS` | `agent-YYYY-MM-DD.log` 按日期分区、大小轮转，超过保留期删除 |
| 普通日志压缩 | 开启 | `ops_config.limits.agent_log_compress_old` / `AGENT_LOG_COMPRESS_OLD` | 非当天普通日志自动 gzip 压缩 |
| 普通日志级别 | `INFO` | `ops_config.logging.level` | 文件变更后，由下一次日志级别检查触发重读；后端和 MCP 进程分别生效，无需重启 |

## 运维配置示例

```json
{
  "limits": {
    "run_tps": 50,
    "user_max_count": 10,
    "workspace_max_count": 5,
    "ontology_max_count_per_workspace": 10,
    "ontology_object_max_count": 200,
    "ontology_operation_max_count": 100,
    "entity_attribute_max_count": 100,
    "model_profile_max_count": 10,
    "model_input_token_limit": 50000,
    "model_output_token_limit": 100000,
    "session_max_per_user_workspace": 100,
    "operation_max_chars": 15000,
    "agent_log_retention_days": 180,
    "agent_log_compress_old": true
  },
  "logging": {
    "level": "INFO"
  }
}
```

## 说明

- 当前 TPS 是后端进程内限制，不是跨多进程/多副本的分布式限流。
- `direct_execution` 暂不单独拆限流，仍共用全局 TPS 和现有并发槽。
- `agent_perf` 性能分析日志不跟随普通日志 180 天策略，继续由 `observability.local.retention_days` 独立控制，并保持可直接读取分析。
- `logging.level` 支持 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL` 和 `NOTSET`。字段缺失时使用进程启动级别，启动级别未配置时为 `INFO`。运行时修改建议先写临时文件，再原子替换 `ops_config.json`；读取到无效 JSON 或无效级别时继续使用上一次有效值。
- 上下文压缩策略不在本文实现范围内，需要单独评估。
- 机器规格为部署建议：消费智能体建议 4U / 8G / 200G，可按实际负载调整；代码不在运行时强制校验 CPU、内存或磁盘容量。
