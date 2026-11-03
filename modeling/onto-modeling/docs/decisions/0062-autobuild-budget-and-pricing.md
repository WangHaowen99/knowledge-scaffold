# 0062 自主构建的预算与模型单价表

## Context

方案 B14：token 用 counted_usage；费用 = token × 单价表（模型配置里新增，Jiuwen 现在没有费用）；时长用墙钟；80% 提醒，
100% 停在安全点。详设 8.5、4.10.1：预算只能设在配额之内。契约 AB-01/AB-02 的 `platform_limits`、`quota_remaining`，
SY-17 平台限制，SY-18 模型单价表，EP-16 `autobuild.budget_warning`，错误 `budget_exceeds_quota {dimension, max}`。
配额（ADR-0043）只有 token 维度，B4c 留了 `autobuild_budget_cap`。

## Decision

1. **模型单价表**（`services/model_pricing.py`，SY-18）：数据目录 `governance/model-pricing.json`，
   `{currency: USD|CNY, items: [{engine, model, input_per_mtok, output_per_mtok, cache_read_per_mtok}]}`（每百万 token），
   默认美元、空表（C-01 `billing.currency` 是美元）。查价先按（引擎, 模型）精确匹配，再取同一引擎 `model="*"` 的一行；
   缓存读没单价按输入单价算。模型名取项目所有者在这个引擎下的有效配置（个人 Key 写了模型就用它，否则管理员配置）。
2. **费用**：有单价按单价算；没有单价时用引擎报的费用（Claude SDK 的 `total_cost_usd`，只在单价表是美元时用，不换汇）；
   都没有记 0 并标估算（Jiuwen 不报费用，没配单价时只能按 token 记账，预算的 token 维度照常生效）。
3. **记账**（`services/autobuild/budget.py`）：只看运行对话里的用量事件（`conversation_store.counted_usage`，与配额同一
   口径：输入 + 缓存读 + 输出），按事件序号增量累加，不每次全量扫历史；每一轮结束、安全点、等待中每 5 秒记一次。
   时长按墙钟，只算跑着的时间（暂停、排队、停机不算）。
4. **提醒与停下**：每个维度第一次到 80% 发一次 EP-16；到 100% 改为停在最近的安全点（这一段做完生效后结束，
   `budget_exhausted`，EP-15 `next=stopped`）。发一步时配额（用户当日、项目当月）不够了（会话池的关口抛 429）同样按
   预算用完结束。
5. **上限**：AB-02、AB-03 校验预算——时长 ≤ 平台 `max_duration_min`，token ≤ 平台 `max_tokens` 与剩余配额的较小者，
   费用 ≤ 平台 `max_cost`；超了回 `422 budget_exceeds_quota {dimension, max}`。默认预算（没存过策略时）是
   120 分钟、100 万 token、30（单价表币种），再按上限截。平台限制（SY-17）存数据目录 `governance/autobuild-limits.json`，
   金额的币种跟单价表走；`allow_production_collection`、`night_priority` 保存回显、本期不生效（`not_enforced`）。

## Alternatives considered

- **单价放进模型接入配置（SY-01）**：模型接入按引擎存一份网关与 Key，个人 Key 还能各自指定模型；单价按（引擎, 模型）
  列表管更直接，也和契约 SY-18 的形状一致。
- **费用按 SDK 报的美元直接用**：私有网关下 SDK 的单价不对，Jiuwen 也不报；单价表优先。
- **超预算立即打断**：打断会留下半截；按详设停在最近的安全点。

## Consequences

- 没配单价时费用可能偏低（Jiuwen 记 0），运行报告与运维总览里的费用标估算的口径在单价表补齐之前不准；token 维度不受影响。
- 预算到了之后还要把这一段做完，实际用量会超出一点。

## 审查修复（2026-11-02）

- 第 4 条「配额不够按预算用完结束」只指 token 配额；并发会话配额不再对运行器发的步骤生效（ADR-0061 审查修复）。
