# Operation Clarity Evaluation 设计记录

## 背景

在当前 operation evolution 功能设计中，先采用一个基础假设：

> 固定 agent 形态；如果 operation 流程写得足够清楚，agent 能够稳定理解并执行。

在这个假设下，agent 的实际执行结果可以作为 operation evolution 的事实依据，用来优化 operation 的可执行性、流程遵从性和执行效率。

但还有一个独立问题需要评估：当 agent 没有按预期执行时，问题到底出在 agent 执行器，还是出在 operation 文本不够清楚？

这个问题不放进当前 evolution 主流程里，而作为一个独立的外部评测 / 诊断工具：**Operation Clarity Evaluation**。

## 目标

Operation Clarity Evaluation 的目标是评估一个 operation 文本是否足够清楚，使固定形态的 agent 能稳定、低歧义地执行。

它不直接判断业务结论是否正确，也不直接生成 operation patch。它输出的是：

- operation 文本的可执行清晰度评分
- 哪些步骤不够明确
- 哪些部分无法转成可验证执行约束
- 更可能是 spec 问题还是 agent runtime 问题
- 可作为后续 evolution 或人工重写的诊断依据

## 核心原则

“写清楚”不能靠人读起来觉得清楚，也不能靠模型说它清楚，而要看 operation 文本是否能被稳定转换成可验证的执行约束。

因此，清晰度的核心定义是：

> 一个 operation 步骤是否能被唯一地、稳定地转换成输入、动作、对象、字段、分支、停止条件和输出约束。

如果能转换成明确约束，且 agent trace 没有满足这些约束，优先归因为 agent/runtime/prompt/tooling 问题。

如果无法转换成明确约束，或多个合理解释都成立，优先归因为 operation 文本不清楚。

## Clarity Rubric

每个 operation 或每个步骤可以按以下维度评分。

| 维度 | 评估问题 | 分数 |
|---|---|---:|
| Trigger | 何时执行该步骤是否明确 | 0-2 |
| Inputs | 所需输入是否明确，是否说明必填/可选 | 0-2 |
| Entities / Fields | 关联实体、字段、查询条件是否明确 | 0-2 |
| Action | 该步骤要执行的动作是否明确 | 0-2 |
| Order / Dependency | 步骤顺序和前后依赖是否明确 | 0-2 |
| Branching | 分支条件是否可计算，每个分支动作是否明确 | 0-2 |
| Stop / Continue | 何时停止、何时继续是否明确 | 0-2 |
| Output | 输出字段、格式、结构是否明确 | 0-2 |
| Error / Empty Handling | 工具错误、空结果、数据不足如何处理是否明确 | 0-2 |
| Validatability | 是否能转成规则检查 | 0-2 |

建议解释：

- `0`：缺失或完全依赖自然语言猜测。
- `1`：有描述，但存在多个合理解释。
- `2`：明确、可执行、可验证。

## 示例

### 不清楚的写法

```text
必要时查询相关物料信息，并根据情况判断是否继续。
```

问题：

- “必要时”没有触发条件。
- “相关物料”没有实体和字段。
- “根据情况”没有分支条件。
- “是否继续”没有停止 / 继续规则。
- 无法转成可验证执行约束。

### 清楚的写法

```text
1. 如果输入 `物料编号` 为空，停止流程并返回 `missing_input: 物料编号`。
2. 读取 `objects/物料.md`。
3. 查询“物料”实体中 `物料编号 = 输入物料编号` 的记录，取字段：`物料编号`、`库存数量`、`可用日期`。
4. 如果查询结果为空，停止流程并返回 `missing_data: 未找到物料记录`。
5. 如果查询工具报错，停止流程并返回 `tool_error`，不要将工具错误解释为业务无数据。
6. 如果查询结果非空，继续执行库存可用性判断。
```

可验证约束：

```json
[
  {
    "rule": "material_id_required_before_search",
    "condition": "input.material_id is empty",
    "expected": "stop_with_missing_input"
  },
  {
    "rule": "read_object_definition_before_search",
    "entity": "物料",
    "expected": "objects/物料.md read before ontology_search"
  },
  {
    "rule": "empty_result_handling",
    "entity": "物料",
    "expected": "stop_with_missing_data"
  },
  {
    "rule": "tool_error_handling",
    "expected": "stop_with_tool_error"
  }
]
```

## 判断执行器问题还是文本问题

### 归因为 operation 文本不清楚

满足以下情况时，优先认为是文本问题：

- operation 没有明确实体或字段。
- operation 没有明确输入和输出。
- operation 的步骤顺序或依赖不明确。
- operation 使用“必要时”“根据情况”“相关信息”等无法计算的表达。
- 空结果、工具错误、数据不足没有处理规则。
- 同一句文本存在多个合理解释。
- 多次执行或多个模型对同一步骤产生不同轨迹。

### 归因为 agent/runtime 问题

满足以下情况时，优先认为是 agent/runtime/prompt/tooling 问题：

- operation 已明确要求某步骤，但 agent 没执行。
- operation 已明确实体/字段，但 agent 查询了不存在或错误字段。
- operation 已明确 script-backed，但 agent 手动重放流程。
- operation 已明确 stop condition，但 agent 继续执行。
- operation 已明确 object definition 前置读取，但 agent 没读。
- 多次执行中只有少数偶发违反，其他执行稳定遵守。

核心规则：

```text
如果 operation 文本中的要求能被转换成明确、可检查的执行约束，而 trace 显示 agent 没满足，则归因为执行器问题。

如果 operation 文本无法明确转换成执行约束，或多个合理解释都成立，则归因为 operation 文本不清楚。
```

## 输出格式建议

```json
{
  "operation_key": "物料可用周期分析",
  "overall_score": 15,
  "max_score": 20,
  "clarity_level": "clear | partially_clear | ambiguous",
  "dimension_scores": {
    "trigger": 2,
    "inputs": 2,
    "entities_fields": 1,
    "action": 2,
    "order_dependency": 2,
    "branching": 1,
    "stop_continue": 1,
    "output": 2,
    "error_empty_handling": 1,
    "validatability": 2
  },
  "ambiguous_steps": [
    {
      "step": "查询相关物料信息",
      "reason": "实体和字段不明确",
      "suggestion": "明确实体为“物料”，并列出查询字段和过滤条件。"
    }
  ],
  "derived_validation_rules": [
    {
      "rule": "read_object_definition_before_search",
      "entity": "物料"
    }
  ],
  "likely_fault_domain": "operation_spec | agent_runtime | mixed",
  "notes": "operation 大体可执行，但空结果处理和字段引用不够明确。"
}
```

## 与 Operation Evolution 的关系

Operation Clarity Evaluation 是外部诊断工具，不是当前 operation evolution 主流程的必需阶段。

它可以用于：

- 新 ontology / operation 上传前的质量检查。
- operation evolution 发现执行分歧后，判断是否应改 spec。
- 管理员 review proposal 时辅助判断 patch 是否合理。
- 对 agent 系统做回归评测：同一清晰流程下，agent 是否稳定遵守。

当前 operation evolution 功能仍然保留基础假设：

```text
固定 agent 形态；清晰流程可被 agent 稳定执行。
```

在这个假设下，evolution 的优化目标仍然是 operation 的：

- 可执行性
- 流程遵从性
- 执行效率

