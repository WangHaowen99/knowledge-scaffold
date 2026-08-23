# Benchmark 自动化测试工具

该目录只保留通用测试工具，不包含客户用例、工作空间配置或历史执行结果。运行前请使用隔离的测试账号和测试工作空间；`.env`、翻译缓存、日志、截图及 Excel/Markdown 报告均为本地运行资产，不得提交到代码仓库。

对后端 Agent 服务进行批量基准测试，支持中/英/日/韩多语言，自动记录性能指标、工具调用统计、中文混入检测等。

## 文件清单

```
├── test_benchmark_suite.py          核心测试脚本
├── run_benchmark_multilang.py     多语言循环执行脚本
├── lang.py                        语言工具 (翻译/检测)
├── .env.example                   配置模板 ← 复制为 .env 使用
├── original_cases/                原始测试数据
│   ├── cases.json                 用例定义 (飞轮/评估/其他)
│   ├── 数据飞轮用例 1 - *.json
│   ├── 数据飞轮用例 2 - *.json
│   ├── 数据飞轮用例 3 - *.json
│   └── 数据飞轮用例 4 - *.json
├── translated_cases/              翻译缓存 (含 en/ja/ko 等 19 种语言)
│   ├── en/cases_en.json
│   ├── ja/cases_ja.json
│   ├── ko/cases_ko.json
│   └── ...
├── result/                        所有输出 (自动创建)
    ├── 20260513_093609/           单语言测试结果 (直接运行 test_benchmark_suite.py)
    │   ├── benchmark_*.xlsx
    │   ├── benchmark_*.md
    │   ├── time_breakdown.png     时间分解图
    │   ├── tool_latency_dist.png 工具调用耗时分布直方图
    │   └── logs/
    └── multilang_20260513_100000/ 多语言测试结果 (运行 run_benchmark_multilang.py)
        ├── report.md              汇总报告 (Markdown)
        ├── time_breakdown.png     多语言时间分解图 (含各语言对比 + 汇总平均)
        ├── tool_latency_dist.png  工具调用耗时分布直方图
        ├── zh/                    中文结果
        │   ├── benchmark_*.xlsx
        │   ├── benchmark_*.md
        │   ├── time_breakdown.png
        │   └── logs/
        ├── en/                    英文结果
        │   └── ...
        ├── ja/                    日文结果
        │   └── ...
        ├── ko/                    韩文结果
        │   └── ...
        └── logs/                  各语言执行原始日志
            ├── benchmark_zh_*.log
            ├── benchmark_en_*.log
            └── ...
```

## 快速开始

### 1. 安装依赖

```bash
pip install requests openpyxl langdetect openai matplotlib
```

> `openai` 和 `langdetect` 仅在需要翻译或 LLM 语言检测时使用；`matplotlib` 用于生成时间分解图表。不安装不影响基本测试。

### 2. 配置

```bash
# 复制配置模板
copy .env.example .env    # Windows
cp .env.example .env      # Linux/Mac

# 编辑 .env，至少设置:
#   BASE_URL=http://localhost:5055   ← 后端服务地址
#   AUTH_USERNAME=<测试账号>         ← 必填，不提供默认值
#   AUTH_PASSWORD=<测试账号密码>     ← 必填，不提供默认值
#   LLM_API_KEY=your-key            ← 翻译功能需要 (可选)
```

### 3. 运行

```bash
# 单语言测试 (默认英文)
python test_benchmark_suite.py

# 多语言批量测试 (语言列表可配置，默认 中→英→日→韩)
python run_benchmark_multilang.py

# 从已有日志重新生成报告
python run_benchmark_multilang.py --replay
```

## 配置项

所有参数通过 `.env` 文件或环境变量设置，**无需修改源码**：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `BASE_URL` | `http://localhost:5055` | 后端服务地址 |
| `STREAM_PATH` | `/agentic/runs/stream` | Agent 流式执行接口（agentic 模式），发送消息并接收 SSE 事件流（含推理过程、工具调用、最终输出） |
| `SESSIONS_PATH` | `/agentic/sessions` | 会话管理接口，每次测试先创建 session 再通过流式接口执行 |
| `ACP_EXECUTE_STREAM_PATH` | `/acp/runs/execute_stream` | ACP 模式快捷流式执行接口（`API_MODE=acp` 且 `ACP_SUBMODE=execute_stream` 时使用） |
| `ACP_INTERACT_STREAM_PATH` | `/acp/runs/{run_id}/interact_stream` | ACP 模式交互接口，处理 `run.awaiting` |
| `ACP_SUBMODE` | `execute_stream` | ACP 子模式选择，见下表 |
| `WORKSPACE_ID` | `default` | 工作区 ID |
| `AUTH_USERNAME` | _(空，必填)_ | 隔离测试账号的认证用户名 |
| `AUTH_PASSWORD` | _(空，必填)_ | 隔离测试账号的认证密码 |
| `API_MODE` | `agentic` | 执行模式: `agentic`（默认）/ `acp` |
| `ACP_LANG` | _(空)_ | ACP 模式下的语言参数，告知 Agent 以指定语言输出，仅在 `runtime_config.lang.enabled=true` 时生效。不设置则使用 `TEST_LANGUAGE` 的值 |
| `ACP_SUPPRESS_SCRIPT_PATTERNS` | _(空)_ | ACP 输出过滤脚本模式，逗号分隔 (如 `mindsdb,work_order_sync`)。设置后会在测试前通过 API 写入工作区的 `acp_suppress_script_patterns` 配置，后端会过滤匹配脚本的 `tool.progress (output/stream)` 事件，并在报告中展示过滤验证结果 |
| `RUNS_PER_QUESTION` | `1` | 每个问题重复次数，增大可观察稳定性 |
| `RUNS_EXECUTION_MODE` | `serial` | 执行模式: `serial` (串行) / `parallel` (并行)，仅当 `RUNS_PER_QUESTION > 1` 时生效 |
| `RUNS_MAX_CONCURRENCY` | `4` | 并行模式下的最大并发数 |
| `ACTIVE_TEST_GROUP` | `eval` | 测试组: `eval` / `flywheel` / `other` / `all` |
| `EVAL_SAMPLE_SIZE` | `2` | 评估用例随机抽取数，`0` = 全部 |
| `TEST_LANGUAGE` | `en` | 测试语言: `zh` / `en` / `ja` / `ko` / `随机` |
| `MULTILANG_LANGUAGES` | `zh,en,ja,ko` | 多语言测试语言列表，逗号分隔；设为 `all` 则执行 `translated_cases/` 下所有支持的语言 (含 zh) |
| `CHINESE_DETECT_MIN_SEVERITY` | `info` | 中文混入检测最低报告级别，见下表 |
| `LLM_API_KEY` | _(空)_ | LLM API Key (翻译/语言检测用) |
| `LLM_BASE_URL` | `https://llm.example.com/v1` | LLM API 地址示例 |
| `LLM_MODEL_NAME` | `example-chat-model` | LLM 模型名称示例 |

> **优先级**: 环境变量 > `.env` 文件 > 默认值
> **注意**: `run_benchmark_multilang.py` 会自动设置 `BENCHMARK_LANG` 环境变量覆盖 `TEST_LANGUAGE`。

### 认证说明

本项目后端 API 需要 **Bearer Token** 认证。测试脚本会自动通过 `POST /auth/login` 获取 token 并附加到所有 API 请求的 `Authorization` 头中。

必须显式配置一个只用于测试的账号。工具不再内置管理员账号或密码。

### 中文混入检测级别说明

非中文测试场景下，Agent 输出可能包含中文字符。`CHINESE_DETECT_MIN_SEVERITY` 控制最低报告级别，低于该级别的发现会被静默忽略：

| 级别 | 含义 | 典型场景 | 设为最低时的报告范围 |
|------|------|---------|-------------------|
| `info` | 信息级 | 引用中文文件名 (如 `知识图谱.py`) | 报告全部 (info + warn + error) |
| `warn` | 警告级 | 少量中文混入、引用中文指令片段 | 仅报告 warn + error |
| `error` | 严重级 | 中英混杂、纯中文输出（期望非中文） | 仅报告 error |

- **`info`**（默认）：最详细，适合开发调试阶段，能看到所有中文出现情况
- **`warn`**：适合日常回归，过滤掉文件名引用等无害的 info 级发现，只关注真正的混入问题
- **`error`**：最严格过滤，只报告严重的中文混入（如整段输出变成中文），适合 CI 流水线

### ACP 子模式 (`ACP_SUBMODE`)

当 `API_MODE=acp` 时，通过 `ACP_SUBMODE` 选择调用哪个 ACP 端点：

| 值 | 调用路径 | 说明 |
|----|---------|------|
| `execute_stream`（默认） | `POST /acp/runs/execute_stream` | 快捷流式：一步创建+执行+流式返回 |
| `stream` | ① `POST /acp/runs` → ② `POST /acp/runs/{id}/stream` | 两步流式：先创建 run，再流式执行 |
| `execute` | ① `POST /acp/runs` → ② `POST /acp/runs/{id}/execute` | 两步同步：先创建 run，再阻塞等待 JSON 结果 |

> 三种子模式均自动处理 `run.awaiting`（通过 `interact_stream` 回复），无需额外配置。

### 语言跟随（Language Follow）

语言跟随功能通过后端 `runtime_config.json` 的 `lang.enabled` 控制。开启后，Agent 会根据 `ACP_LANG` 参数（ACP 模式）或自动检测用户语言，在系统提示词中注入语言指令，从而控制输出语言。

| 语言跟随 | `ACP_LANG` | 预期输出 | 说明 |
|:--------:|:----------:|:--------:|------|
| ON | `en` | 英文 | ACP 传入 `lang=en`，Agent 以英文输出 |
| ON | `ja` | 日文 | ACP 传入 `lang=ja`，Agent 以日文输出 |
| ON | `ko` | 韩文 | ACP 传入 `lang=ko`，Agent 以韩文输出 |
| ON | 不设置 | 跟随 `TEST_LANGUAGE` | 未设 `ACP_LANG` 时自动使用 `TEST_LANGUAGE` |
| OFF | 任意 | 中文（问题语言） | 语言跟随关闭时 ACP 传入的语言参数被忽略 |

> **注意**：语言跟随关闭时，Agent 始终跟随用户问题的语言（中文）输出，无论 `ACP_LANG` 设置为什么值。

## 测试用例分组

| 组名 | 范围 | 说明 |
|------|------|------|
| `eval` | 问题5-14 | 评估用例 (故障诊断) |
| `flywheel` | 问题1-4 | 数据飞轮用例 (需 JSON 文件) |
| `other` | 问题15等 | 其他补充用例 |
| `all` | 问题1-15 | 串行执行全部 |

> 用例定义在 `original_cases/cases.json` 中，可直接编辑 JSON 增减用例，无需修改源码。

## 常见场景

### 快速冒烟 (2个评估用例 × 1次)

```ini
# .env
ACTIVE_TEST_GROUP=eval
EVAL_SAMPLE_SIZE=2
RUNS_PER_QUESTION=1
TEST_LANGUAGE=zh
```

### 完整回归 (全量 × 3次)

```ini
ACTIVE_TEST_GROUP=all
EVAL_SAMPLE_SIZE=0
RUNS_PER_QUESTION=3
```

### 切换测试服务器

```ini
BASE_URL=http://127.0.0.1:5055
```

### 仅测数据飞轮

```ini
ACTIVE_TEST_GROUP=flywheel
```

### 稳定性测试 (并行 × 10次)

```ini
RUNS_PER_QUESTION=10
RUNS_EXECUTION_MODE=parallel
RUNS_MAX_CONCURRENCY=5
```

## 串行 vs 并行执行

当 `RUNS_PER_QUESTION > 1` 时，同一个问题会执行多次。可通过 `RUNS_EXECUTION_MODE` 控制执行方式：

| 模式 | 值 | 说明 |
|------|-----|------|
| 串行 | `serial` | 逐次执行，前一次完成后再开始下一次，结果按顺序写入 Excel |
| 并行 | `parallel` | 使用线程池并发执行，`RUNS_MAX_CONCURRENCY` 控制最大并发数 |

**并行模式注意事项**：
- 并行执行可大幅缩短总耗时（10次 × 30s 串行需 5 分钟，并行 5 并发仅需约 1 分钟）
- Excel 写入使用线程锁保护，确保数据不冲突
- 结果按 `run_index` 排序后写入，与串行模式输出一致
- 控制台输出可能交错，但不影响最终报告

## 输出说明

### 目录结构

每次运行自动在 `result/` 下创建时间戳子目录：

- **单语言**: `result/YYYYMMDD_HHMMSS/` — 含 Excel + Markdown 报告 + 时间分解图 + logs/
- **多语言**: `result/multilang_YYYYMMDD_HHMMSS/` — 每种语言一个子目录 + Markdown 汇总报告 + 时间分解图 + 各语言原始日志

### Excel 文件

每个问题对应一个 Sheet，每行记录一次执行：耗时、tokens、LLM 调用统计、脚本调用统计、首 token 时延等。

### Markdown 报告

自动生成，包含：环境配置（LLM 厂商/模型、后端地址、工作空间、执行模式等）、测试参数、全局统计、脚本执行统计、中文混入检测、knowledge_graph_tool 参数/输出语言检测、`<think_process>` 标签分析等。

多语言汇总报告 (`report.md`) 还包含：跨语言环境配置、全局统计汇总、各语言核心指标对比表格、工具执行统计跨语言汇总。

### 时间分解图

每次测试自动生成 `time_breakdown.png`，展示每次执行的时间构成：

- **LLM 时间** = 总平均耗时 − 工具调用耗时分摊
- **工具时间** = KG 工具 + MindsDB + WOS 的总调用时间 ÷ 总执行次数

| 场景 | 生成文件 | 说明 |
|------|----------|------|
| 单语言测试 | `result/<时间戳>/time_breakdown.png` | 该语言的水平条形图 |
| 单语言测试 | `result/<时间戳>/tool_latency_dist.png` | 该语言各工具的调用耗时分布直方图 |
| 多语言测试 | `result/multilang_<时间戳>/time_breakdown.png` | 左: 各语言堆叠柱状对比 右: 汇总平均水平条形 |
| 多语言测试 | `result/multilang_<时间戳>/tool_latency_dist.png` | 所有语言合并的工具调用耗时分布直方图 |

> 需要 `matplotlib`，未安装时自动跳过，不影响测试本身。

### knowledge_graph_tool 检测项

测试过程中会自动捕获 `knowledge_graph_tool.py` 的输入/输出，并进行以下检测：

#### 1. 返回中文检测

检测 KG 工具返回数据是否包含中文字符。仅在非中文测试场景下生效。报告中展示中文字符数、占比、涉及的 JSON 键名和值示例。

#### 2. language 参数检测

验证 KG 工具被调用时传入的 language 参数是否正确：

| 检测状态 | 说明 |
|----------|------|
| ✅ 参数正确 | language 参数与目标语言一致，且为合法的语言 key（如 `en`、`ja`） |
| 🔴 传入显示名称 | 传入了 `_LANG_DISPLAY_NAMES` 的 value（如 `"English"`、`"日本語"`）而非 key |
| 🔴 参数不匹配 | language 参数与期望的目标语言不一致 |
| 🔴 缺少参数 | 非中文场景下未传入 language 参数 |
| 🟡 未捕获 | 未捕获到执行命令（可能工具未被调用） |

#### 3. 输出语言匹配检测

检测 KG 工具返回的文本语言是否与目标测试语言匹配：

- **CJK / 独特文字语言**（ja/ko/ru/ar/th 等）：通过 Unicode 字符范围启发式检测
- **拉丁字母语言**（en/fr/de/es/pt 等）：**优先使用 LLM 进行精确语言识别**，LLM 不可用时 fallback 到启发式检测（无法区分共享拉丁字母的语言）

> 所有检测均实时打印到控制台，并写入 Markdown 报告和汇总报告。

### 控制台

实时打印进度和汇总统计。

## 性能数据采集

本项目后端**不发射 `dag_performance` SSE 事件**，性能数据写入 workspace runtime 下日期分片的 `otel_trace-YYYY-MM-DD.log`。

测试脚本在每次执行完成后，会自动按 `run_id` 从 perf log 中提取：

| 指标 | perf log 事件 | 说明 |
|------|--------------|------|
| LLM 调用次数 | `llm.collect.completed` | 统计匹配 run_id 的 LLM 完成事件数 |
| LLM 总耗时 | `llm.collect.completed` → `duration_ms` | 累加所有 LLM 调用的耗时 |
| Worker 耗时 | `main_agent.worker.completed` → `duration_ms` | Agent Loop 总执行时间 |

## 注意事项

- 默认串行执行，可通过 `RUNS_EXECUTION_MODE=parallel` 开启并行
- 每次运行创建独立结果目录，互不干扰
- Excel 从空模板复制而来，无需手动清理
- 确保后端服务在测试期间保持可用
- 后端 API 需要 Bearer Token 认证，请确保 `.env` 中的 `AUTH_USERNAME` / `AUTH_PASSWORD` 正确
- 不配置 `LLM_API_KEY` 时，翻译功能不可用，但中文测试仍可正常执行
- `LLM_API_KEY` 同时用于 KG 工具输出语言匹配检测（拉丁字母语言精确识别），不配置时拉丁字母语言检测会降级为启发式
- `translated_cases/` 包含预翻译缓存，提供此目录可避免在线翻译（省时间、不依赖 LLM Key）
- 安装 `matplotlib` 后自动生成时间分解图表，不安装不影响测试，仅跳过图表
- 多语言汇总报告为 Markdown 格式 (`report.md`)，自动从子语言报告中提取环境配置信息

## 与本项目的适配差异

> 以下差异相对于原始测试脚本（来自另一个项目），已在本项目完成适配。

| 差异项 | 原项目 | 本项目 |
|--------|--------|--------|
| API 路径前缀 | `/api/agentic/...` | `/agentic/...` (无 `/api` 前缀) |
| 默认端口 | `8000` | `5055` |
| 认证 | 无需认证 | Bearer Token (`POST /auth/login`) |
| SSE 脚本名称 | 文件名如 `knowledge_graph_tool.py` | operation_key 如 `故障知识更新`，需映射 |
| 性能数据 | SSE `dag_performance` 事件 | OTel 本地文件 (`otel_trace-YYYY-MM-DD.log`) |
| Agent 架构 | Planner + Worker + Finalizer | 仅 Worker |
| 日志文件 | 单一 `app.log` | 日期分片 `agent-YYYY-MM-DD.log` |
