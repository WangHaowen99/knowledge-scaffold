# 普通 Agent 脚本参数证据校验

`execute_script` 在解析实际绑定的脚本后、启动脚本前，校验业务参数与当前请求及可信工具证据的一致性。该校验独立于 `deterministic_execution_enabled`；确定性服务继续使用自己的 Schema、Parser 和包锁校验。

## 生成结果记录标识

只有负责创建结果记录的操作，才应在对应 `scripts/*.md` 描述的 YAML frontmatter 中声明 `generated_identifiers`。声明将参数内的字段路径映射到生成格式；当前支持标准带连字符的 UUID4，不根据字段后缀统一放行。

以下为完全合成的描述示例：

```markdown
---
key: create_result
bound_operation: create_result
entrypoint: scripts/create_result.py
generated_identifiers:
  record_json.calculation_result_id: uuid4
  rows_json[].calculation_result_id: uuid4
---
```

路径相对于 `arguments`，`.` 表示对象字段，`[]` 表示数组元素；对象或数组通过 JSON 字符串传参时也适用。合成 UUID 示例为 `00000000-0000-4000-8000-000000000001`。声明会出现在公开脚本目录和动态工具说明中。

该声明只用于新建结果记录。查询、修改或删除已有对象的标识不能声明为生成字段。未声明字段继续要求请求或可信输出证据；不支持的生成格式不会形成豁免。错误格式的生成值返回 `invalid_generated_identifier`。字段声明由绑定脚本描述提供，模型不能在调用参数中自行添加豁免规则。

对于已固定知识包版本的运行，校验使用经过版本验证的脚本绑定，并在同一份暂存包上执行脚本。

启用沙箱时，已通过内容版本验证的暂存副本通过内部作用域传给执行器。每次调用使用独立的附属沙箱，只读挂载该副本及标准沙箱运行文件，不挂载整个暂存目录或其他工作区。脚本结束后先释放沙箱，再清理副本；工具参数不能建立此作用域或追加挂载。

## 当前请求内的可信证据

- 仅采集最后一条用户消息之后的 `business_data_search` 和 `execute_script` 结构化输出。
- 脚本输出必须对应前面实际记录的脚本工具调用，调用 ID 和知识包匹配；模型正文、无对应调用的工具消息及其他知识包结果不作为脚本证据。
- 工具错误、显式失败状态以及成功批次内的失败记录都不能提供证据。
- 身份值按字段匹配，记录内的字段关联分别保存，不能从多个结果记录拼接身份与属性。
- 当前请求中明确给出的记录顺序和字段对应关系仍参与校验。旧轮次的输出不用于新请求的参数校验。

恢复会话历史时，Agent 同时为工具状态补入本轮用户消息；模型历史仍由 ContextEngine 接收，不重复传入当前查询。成功调用的复用保护也仅检查当前轮，失败结果不计入成功记录，避免旧结果在参数校验前被直接复用。

## 错误与重试

校验拒绝返回 JSON，脚本不会启动：

```json
{
  "ok": false,
  "error": {
    "code": "SCRIPT_ARGUMENT_VALIDATION_FAILED",
    "message": "Script arguments failed validation; the script did not run.",
    "correctable": true,
    "issues": [
      {"code": "ungrounded_identity", "path": "arguments.record_json.device_id"}
    ]
  }
}
```

分类包括 `ungrounded_identity`、`invalid_generated_identifier`、`request_field_mismatch`、`request_item_order`、`request_record_mismatch` 和 `evidence_record_mismatch`。公开诊断只包含固定分类与有限长度的字段路径，不包含参数值、原始请求、stdout 或 stderr。

首次校验拒绝允许修正输入。同一操作在后续模型迭代再次产生相同分类和字段路径的失败时，停滞保护会终止运行；仅更换错误参数值不算修正。成功调用会重置连续失败记录。其他工具错误仍沿用原有完整参数指纹逻辑。

停滞终态的 `error_code` 为 `AGENT_LOOP_STALLED`、`retriable` 为 `false`，`details.last_argument_validation` 保留最近一次安全校验原因，其中 `correctable` 描述参数错误本身是否可修正。仅包含已知停滞异常的异常组保留此分类；混入未知异常时继续使用运行时兜底。

## 聚焦验证

在仓库声明的后端依赖环境中，从 `consumer/` 执行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend backend/venv/bin/python -m pytest \
  backend/tests/test_script_argument_evidence.py \
  backend/tests/test_execute_script_structured_arguments.py \
  backend/tests/test_repeated_tool_failure_guard.py \
  backend/tests/test_jiuwen_runtime_events.py
```

上述回归使用合成模型行为与受控进程执行替身。另有 `test_sandbox_package_revision.py` 验证挂载边界、跨工作区拒绝、附属沙箱释放和异常清理；`test_jiuwen_async_engine.py`、`test_jiuwen_react_adapter.py` 覆盖历史恢复与模型适配。

真实模型、Run API 和 JiuwenBox 联调结果见 [Issue #295 端到端验证记录](issue-295-e2e-verification.md)。
