# ACP 协议 + 多语言能力 测试指南

保姆级测试教程，覆盖 ACP 协议接口、多语言语言跟随、输出抑制三大功能的验证。

---

## 一、测试环境准备

### 1.1 后端启动

```bash
cd d:\project\onto-agent\backend
python app.py
```

确认后端在 `http://localhost:5055` 正常运行。

### 1.2 测试工具目录

```
d:\project\onto-agent\backend\tests\benchmark\
├── test_benchmark_suite.py         核心测试脚本
├── run_benchmark_multilang.py    多语言批量测试
├── run_acp_full_matrix.py        [新] ACP 全场景自动测试矩阵（一键运行全部 35 测试项）
├── test_acp_full.py              [新] ACP 协议端点结构化测试（HTTP 状态码/字段断言）
├── .env.example                  配置模板
└── .env                          实际配置（需手动创建）
```

### 1.3 安装依赖

```bash
pip install requests openpyxl langdetect openai matplotlib
```

### 1.4 创建配置文件

```bash
cd d:\project\onto-agent\backend\tests\benchmark
copy .env.example .env
```

---

## 二、测试矩阵

### 2.1 场景概览（10 种条件）

| 编号 | 测试场景 | API_MODE | 语言跟随 | ACP_LANG | 抑制配置 | 验证目标 |
|:----:|---------|:--------:|:--------:|:--------:|---------|---------|
| T1 | Agentic 基线 | `agentic` | OFF | - | 空 | 基线对比，所有工具输出可见 |
| T2 | ACP 基线 | `acp` | OFF | - | 空 | ACP 协议正常工作 |
| T3 | ACP + 语言跟随(中文) | `acp` | ON | `zh` | 空 | 中文输出，无中文混入问题 |
| T4 | ACP + 语言跟随(英文) | `acp` | ON | `en` | 空 | 英文输出，检测中文混入 |
| T5 | ACP + 语言跟随(日文) | `acp` | ON | `ja` | 空 | 日文输出，检测中文混入 |
| T6 | ACP + 语言跟随(韩文) | `acp` | ON | `ko` | 空 | 韩文输出，检测中文混入 |
| T7 | ACP + 抑制工具名 | `acp` | OFF | - | `business_data_search` | business_data_search 结果不可见 |
| T8 | ACP + 抑制操作名 | `acp` | OFF | - | `故障历史` | 匹配"调用故障历史记录接口"的结果不可见 |
| T9 | ACP + 抑制多个 | `acp` | OFF | - | `business_data_search,故障历史` | 两个都被抑制 |
| T10 | ACP + 语言跟随 + 抑制 | `acp` | ON | `en` | `故障历史` | 语言跟随 + 抑制同时生效 |

### 2.2 子模式扩展（每种条件 × 3 种 ACP 子模式）

除 T1（Agentic）外，每个 ACP 场景在自动化矩阵中扩展为 3 种子模式：

| 子模式 | 对应端点 | 说明 |
|:------:|---------|------|
| `execute_stream` | `POST /acp/runs/execute_stream` | 快捷流式 — 一键创建+流式执行 |
| `stream` | `POST /acp/runs/{id}/stream` | 两步流式 — 先创建，再流式执行 |
| `execute` | `POST /acp/runs/{id}/execute` | 同步执行 — 等待完整结果返回 |

**完整矩阵共 35 个测试项**：
- 前置检查: P1（API 端点 11 用例）+ P2（错误场景 9 用例）+ P2b（用户隔离 4 用例）
- 核心矩阵: T1（Agentic，1 个）+ T2-T10（9 种条件 × 3 子模式 = 27 个）
- 后置检查: P3（完整流程 6 用例）

使用 `run_acp_full_matrix.py` 自动化脚本可一键运行全部 35 个测试项（详见第八节）。

---

## 三、逐场景测试配置

### 前置：开启/关闭语言跟随

测试脚本会自动检测 `runtime_config.lang.enabled` 的值。你需要在前端 **工作空间管理 → 运行时参数** 中手动切换：

- **开启语言跟随**：勾选"语言跟随" → 保存
- **关闭语言跟随**：取消勾选"语言跟随" → 保存

或者通过 API 操作：

```bash
# 开启语言跟随
curl -X PATCH http://localhost:5055/agentic/workspaces/default/runtime-config ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer <token>" ^
  -d "{\"lang\": {\"enabled\": true, \"default\": \"zh\"}}"

# 关闭语言跟随
curl -X PATCH http://localhost:5055/agentic/workspaces/default/runtime-config ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer <token>" ^
  -d "{\"lang\": {\"enabled\": false, \"default\": \"zh\"}}"
```

---

### T1: Agentic 基线

**目的**：确认基础功能正常，所有工具输出可见。

**.env 配置**：
```ini
API_MODE=agentic
RUNS_PER_QUESTION=1
ACTIVE_TEST_GROUP=eval
EVAL_SAMPLE_SIZE=2
TEST_LANGUAGE=zh
```

**验证要点**：
- 执行成功率 100%
- 日志中 `business_data_search` 和 `execute_script` 的结果都可见
- 无报错

---

### T2: ACP 基线

**前置**：前端关闭语言跟随

**.env 配置**：
```ini
API_MODE=acp
RUNS_PER_QUESTION=1
ACTIVE_TEST_GROUP=eval
EVAL_SAMPLE_SIZE=2
TEST_LANGUAGE=zh
```

**验证要点**：
- 执行成功率 100%
- ACP SSE 事件流正常（answer、thinking、result 事件）
- `ontology_context` 事件不出现（被服务器过滤）
- `business_data_search` 和 `execute_script` 结果可见

---

### T3: ACP + 语言跟随(中文)

**前置**：前端开启语言跟随

**.env 配置**：
```ini
API_MODE=acp
ACP_LANG=zh
RUNS_PER_QUESTION=1
ACTIVE_TEST_GROUP=eval
EVAL_SAMPLE_SIZE=2
TEST_LANGUAGE=zh
```

**验证要点**：
- Agent 输出全中文
- 无异常

---

### T4: ACP + 语言跟随(英文)

**前置**：前端开启语言跟随

**.env 配置**：
```ini
API_MODE=acp
ACP_LANG=en
RUNS_PER_QUESTION=1
ACTIVE_TEST_GROUP=eval
EVAL_SAMPLE_SIZE=2
TEST_LANGUAGE=en
CHINESE_DETECT_MIN_SEVERITY=info
```

**验证要点**：
- Agent 输出全英文
- 报告中"中文混入检测"应为 ✅ 或仅有 info 级（文件名引用等）
- `knowledge_graph_tool` 返回数据中无中文

---

### T5/T6: ACP + 语言跟随(日文/韩文)

与 T4 相同，仅改 `ACP_LANG=ja` 或 `ACP_LANG=ko`，`TEST_LANGUAGE=ja` 或 `TEST_LANGUAGE=ko`。

---

### T7: ACP + 抑制工具名

**前置**：前端关闭语言跟随

**.env 配置**：
```ini
API_MODE=acp
ACP_SUPPRESS_SCRIPT_PATTERNS=business_data_search
RUNS_PER_QUESTION=1
ACTIVE_TEST_GROUP=eval
EVAL_SAMPLE_SIZE=2
TEST_LANGUAGE=zh
```

**验证要点**：
- 测试脚本自动调用 API 设置 `acp_suppress_script_patterns`
- 日志"用户实际可见内容"中**不应出现** `🔧 business_data_search` 的结果
- `execute_script` 结果仍然可见（未被抑制）

---

### T8: ACP + 抑制操作名

**前置**：前端关闭语言跟随

**.env 配置**：
```ini
API_MODE=acp
ACP_SUPPRESS_SCRIPT_PATTERNS=故障历史
RUNS_PER_QUESTION=1
ACTIVE_TEST_GROUP=eval
EVAL_SAMPLE_SIZE=2
TEST_LANGUAGE=zh
```

**验证要点**：
- "调用故障历史记录接口"的 `execute_script` 结果**不应出现**
- "调用产品研发机理接口"的结果**应该可见**（不匹配"故障历史"关键词）

---

### T9: ACP + 抑制多个

**前置**：前端关闭语言跟随

**.env 配置**：
```ini
API_MODE=acp
ACP_SUPPRESS_SCRIPT_PATTERNS=business_data_search,故障历史
RUNS_PER_QUESTION=1
ACTIVE_TEST_GROUP=eval
EVAL_SAMPLE_SIZE=2
TEST_LANGUAGE=zh
```

**验证要点**：
- `business_data_search` 结果不可见
- "调用故障历史记录接口"结果不可见
- "调用产品研发机理接口"结果应该可见

---

### T10: ACP + 语言跟随 + 抑制（组合）

**前置**：前端开启语言跟随

**.env 配置**：
```ini
API_MODE=acp
ACP_LANG=en
ACP_SUPPRESS_SCRIPT_PATTERNS=故障历史
RUNS_PER_QUESTION=1
ACTIVE_TEST_GROUP=eval
EVAL_SAMPLE_SIZE=2
TEST_LANGUAGE=en
CHINESE_DETECT_MIN_SEVERITY=info
```

**验证要点**：
- 英文输出 + 无中文混入
- "调用故障历史记录接口"结果被抑制
- 两个功能互不干扰

---

## 四、运行测试

### 4.1 单场景运行

```bash
cd d:\project\onto-agent\backend\tests\benchmark

# 编辑 .env 为目标场景配置后执行：
python test_benchmark_suite.py
```

### 4.2 多语言批量运行

```bash
# 确保 .env 中 API_MODE=acp，前端已开启语言跟随
python run_benchmark_multilang.py
```

`run_benchmark_multilang.py` 会按 `MULTILANG_LANGUAGES` 配置的语言列表依次执行，每种语言生成独立结果目录。

### 4.3 从已有日志重新生成报告

```bash
python run_benchmark_multilang.py --replay
```

---

## 五、查看结果

### 5.1 结果目录结构

```
tests/benchmark/result/
└── 20260525_093249/              ← 时间戳目录
    ├── benchmark_20260525_093249.xlsx    Excel 报告
    ├── benchmark_20260525_093723.md      Markdown 报告
    ├── time_breakdown.png                时间分解图
    ├── tool_latency_dist.png             工具耗时分布
    ├── failure_details.json              失败详情
    ├── script_call_details.json          脚本调用详情
    └── logs/
        └── 问题6_第1次_20260525_093249_None.txt   单次执行日志
```

### 5.2 关键检查项

**在 Markdown 报告中查看**：

| 检查项 | 位置 | 通过标准 |
|--------|------|---------|
| 成功率 | 报告顶部 | 100% |
| 中文混入检测 | `knowledge_graph_tool 返回中文检测` | ✅ 未检测到中文 |
| 语言参数检测 | `knowledge_graph_tool language参数检测` | ✅ language 参数与目标语言一致 |
| 输出语言匹配 | `knowledge_graph_tool 输出语言匹配检测` | ✅ 输出语言与目标语言匹配 |
| ACP 输出过滤 | `ACP 输出过滤验证` | ✅ 被抑制的工具/脚本结果不可见 |

**在日志文件中查看**：

日志路径：`logs/问题X_第Y次_时间戳_None.txt`

```
--- 用户实际可见内容 ---
这是 Agent 的回答文本...                    ← answer 事件
🔧 execute_script(调用故障历史记录接口)     ← result 事件（如未被抑制）
[{...工具结果JSON...}]
```

**验证抑制是否生效**：
- 被抑制的工具/脚本 → 在"用户实际可见内容"中**不应出现** `🔧 xxx` 行
- 未被抑制的工具/脚本 → **应该出现** `🔧 xxx` 行和完整结果

---

## 六、抑制功能的验证方法详解

### 6.1 当前抑制机制

onto-agent 的 ACP 输出抑制采用**方案 A**：

| 匹配目标 | 匹配字段 | 示例 |
|---------|---------|------|
| 工具名 | `tool_name` | `business_data_search` → 抑制该工具的全部 `tool.finished` 结果 |
| 操作名关键词 | `operation_key` | `故障历史` → 抑制"调用**故障历史**记录接口"的 `execute_script` 结果 |

**注意**：被抑制的是 `tool.finished` 事件中的结果内容，工具的 `tool.started`（thinking 事件"正在调用 xxx"）仍可见。

### 6.2 手动验证步骤

1. 设置抑制配置（通过 .env 的 `ACP_SUPPRESS_SCRIPT_PATTERNS` 或前端 UI）
2. 运行测试
3. 打开日志文件，查看 `--- 用户实际可见内容 ---` 段落
4. 确认被抑制的工具/脚本结果不出现

### 6.3 对比验证

可以跑两次测试做对比：

```ini
# 第一次：不抑制（.env）
ACP_SUPPRESS_SCRIPT_PATTERNS=

# 第二次：抑制（.env）
ACP_SUPPRESS_SCRIPT_PATTERNS=business_data_search,故障历史
```

对比两次日志中"用户实际可见内容"的差异。

---

## 七、常见问题

### Q: 测试脚本报认证失败？

确认 `.env` 中 `AUTH_USERNAME` 和 `AUTH_PASSWORD` 与后端 `data/users.json` 一致。默认是 `admin` / `admin123`。

### Q: ACP 模式下事件流为空？

确认后端 ACP Blueprint 已注册。检查 `backend/agentic/routes/__init__.py` 中是否注册了 `acp_bp`。

### Q: 语言跟随设置了但没生效？

1. 确认前端**已保存**运行时配置（点了"保存运行时配置"按钮）
2. 确认 `API_MODE=acp`（agentic 模式不走 ACP 语言逻辑）
3. 确认 `ACP_LANG` 已设置或 `TEST_LANGUAGE` 已设置

### Q: 抑制配置设置了但没生效？

1. 确认 `API_MODE=acp`（抑制仅在 ACP 模式生效）
2. 确认关键词正确：工具名要完全匹配（如 `business_data_search`），操作名只需包含关键词（如 `故障历史`）
3. 检查日志中的打印信息：`🔧 ACP 输出过滤: 已设置 acp_suppress_script_patterns = 'xxx'`

### Q: 测试脚本中的抑制验证逻辑与实际不符？

测试脚本 `test_benchmark_suite.py` 的抑制验证逻辑（L3089-3118）基于旧的 `tool.progress` 机制。当前实现改为在 `tool.finished` 阶段抑制，验证方式应以日志中"用户实际可见内容"为准。测试脚本的验证逻辑后续需要同步更新。

---

## 八、自动化全场景矩阵

### 8.1 概述

`run_acp_full_matrix.py` 一键执行全部 31 个测试项，无需手动修改 `.env` 或操作前端。

执行流程：
1. **前置检查** — 运行 `test_acp_full.py --group api,error`，验证所有 ACP 端点 HTTP 状态码和字段正确性（16 用例）
2. **核心矩阵** — 依次运行 28 个 benchmark 场景，每个场景自动设置服务端 `runtime_config`（语言跟随/抑制模式），运行 `test_benchmark_suite.py`
3. **后置检查** — 运行 `test_acp_full.py --group flow`，验证端到端完整流程（6 用例）

全部运行完成后自动恢复原始 `runtime_config`。

### 8.2 使用方法

```bash
cd d:\project\onto-agent\backend\tests\benchmark

# 执行全部 31 个测试项（前置检查 + 28 benchmark 场景 + 后置检查）
python run_acp_full_matrix.py

# 快捷模式（每个场景只跑 1 个问题）
python run_acp_full_matrix.py --quick

# 仅列出所有场景（含 P1/P2/P3 结构测试）
python run_acp_full_matrix.py --list

# 仅运行指定 benchmark 场景，跳过结构测试
python run_acp_full_matrix.py --ids T1,T2a,T2b,T2c

# 跳过 T1 Agentic 基线
python run_acp_full_matrix.py --skip-agentic
```

### 8.3 完整测试项列表

```
── 前置检查: ACP 协议结构测试 ──
 P1      ACP-API端点验证                 test_acp_full.py --group api      8 用例
 P2      ACP-错误场景验证                test_acp_full.py --group error    8 用例

── 核心矩阵: benchmark 场景（28 个）──
 ID      名称                          模式                   语言跟随   语言   抑制
 ─────── ──────────────────────────── ──────────────────── ────────── ────── ─────────────────────
 T1      Agentic基线                   Agentic               OFF        -      -
 T2a     ACP基线-execute_stream         ACP/execute_stream    OFF        -      -
 T2b     ACP基线-stream                 ACP/stream            OFF        -      -
 T2c     ACP基线-execute                ACP/execute           OFF        -      -
 T3a     ACP+中文-execute_stream        ACP/execute_stream    ON         zh     -
 T3b     ACP+中文-stream                ACP/stream            ON         zh     -
 T3c     ACP+中文-execute               ACP/execute           ON         zh     -
 T4a     ACP+英文-execute_stream        ACP/execute_stream    ON         en     -
 T4b     ACP+英文-stream                ACP/stream            ON         en     -
 T4c     ACP+英文-execute               ACP/execute           ON         en     -
 T5a     ACP+日文-execute_stream        ACP/execute_stream    ON         ja     -
 T5b     ACP+日文-stream                ACP/stream            ON         ja     -
 T5c     ACP+日文-execute               ACP/execute           ON         ja     -
 T6a     ACP+韩文-execute_stream        ACP/execute_stream    ON         ko     -
 T6b     ACP+韩文-stream                ACP/stream            ON         ko     -
 T6c     ACP+韩文-execute               ACP/execute           ON         ko     -
 T7a     抑制工具名-execute_stream      ACP/execute_stream    OFF        -      business_data_search
 T7b     抑制工具名-stream              ACP/stream            OFF        -      business_data_search
 T7c     抑制工具名-execute             ACP/execute           OFF        -      business_data_search
 T8a     抑制操作名-execute_stream      ACP/execute_stream    OFF        -      故障历史
 T8b     抑制操作名-stream              ACP/stream            OFF        -      故障历史
 T8c     抑制操作名-execute             ACP/execute           OFF        -      故障历史
 T9a     抑制多个-execute_stream        ACP/execute_stream    OFF        -      business_data_search,故障历史
 T9b     抑制多个-stream                ACP/stream            OFF        -      business_data_search,故障历史
 T9c     抑制多个-execute               ACP/execute           OFF        -      business_data_search,故障历史
 T10a    语言+抑制-execute_stream       ACP/execute_stream    ON         en     故障历史
 T10b    语言+抑制-stream               ACP/stream            ON         en     故障历史
 T10c    语言+抑制-execute              ACP/execute           ON         en     故障历史

── 后置检查: ACP 完整流程测试 ──
 P3      ACP-完整流程验证                test_acp_full.py --group flow    6 用例
```

总计 31 个测试项（3 结构测试 + 28 benchmark 场景）。

### 8.4 输出结构

运行后生成：

```
tests/benchmark/result/
└── full_matrix_20260526_093249/        ← 时间戳目录
    ├── full_matrix_report.md            ← 汇总报告（Markdown）
    ├── _precheck/                       ← 前置检查结果
    │   ├── P1.log                       ← ACP-API端点验证 日志
    │   └── P2.log                       ← ACP-错误场景验证 日志
    ├── T1/                              ← 各 benchmark 场景独立子目录
    │   ├── T1.log                       ← benchmark 完整日志
    │   └── benchmark_*.md               ← benchmark 原始报告（含图表）
    ├── T2a/
    ├── T2b/
    │   └── ...
    └── _postcheck/                      ← 后置检查结果
        └── P3.log                       ← ACP-完整流程验证 日志
```

汇总报告中包含：协议结构验证结果、各场景成功率/耗时对比、中文混入检测摘要、输出抑制验证摘要、异常场景高亮。

### 8.5 配置项

可通过 `.env` 配置自动化脚本的行为：

```ini
# 每个场景抽取的评估问题数（默认 1）
ACP_MATRIX_SAMPLE_SIZE=1

# 每个问题重复执行次数（默认 1）
ACP_MATRIX_RUNS=1
```

---

## 九、并发测试（可选）

### 9.1 配置示例

```ini
# 10 并发
API_MODE=acp
RUNS_PER_QUESTION=10
RUNS_EXECUTION_MODE=parallel
RUNS_MAX_CONCURRENCY=10
ACTIVE_TEST_GROUP=eval
EVAL_SAMPLE_SIZE=2
TEST_LANGUAGE=zh
```

### 9.2 并发级别参考

| 并发数 | RUNS_PER_QUESTION | RUNS_MAX_CONCURRENCY | 说明 |
|--------|-------------------|---------------------|------|
| 10 串行 | 10 | - | RUNS_EXECUTION_MODE=serial |
| 10 并行 | 10 | 10 | RUNS_EXECUTION_MODE=parallel |
| 50 并行 | 50 | 50 | |
| 100 并行 | 100 | 100 | 高负载，关注脚本失败率 |
