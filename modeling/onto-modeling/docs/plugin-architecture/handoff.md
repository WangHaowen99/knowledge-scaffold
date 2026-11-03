# onto-modeling 插件化改造 · 交接文档

更新：2026-11-02。本文给接手的人：现在做到哪、代码在哪、怎么接着做、有哪些事等决定。细节都在同目录的其他文档里，这里只给入口。

## 1. 现状

目标是在一个分支上按《插件化架构详细设计》v1.1（`design/detailed-design-v1.1.md`）和前端设计稿第五版，完成 onto-modeling 的插件化改造：后端按详细设计，前端与设计稿一致（Studio 的 WebGL 渲染除外）。

- **后端：** 完成 P0、P1，P2 完成三块中的两块（B4a 会话宿主、B4c 插件机制），剩 B4b。P3、P4 未开始。
- **前端：** 设计稿 34 页全部完成，跑在 mock 数据上，还没有和真实后端联调。
- **测试基线（2026-11-02）：**
  - 交付分支 Linux 容器全量 2239 过 / 1 既有失败；
  - 前端分支 `npm test` 473 过，`npm run build` 通过。
- **2026-11-02 起暂停**，等需求方说继续。暂停点之后的第一件事是 B4b。

| 线 | 阶段 | 内容 | 状态 |
|---|---|---|---|
| 契约 | C0 | API v1 契约：321 个接口、59 种事件 | 完成，见 `../api-v1.md` |
| 后端 | B1 | 评测移出、验证回执入口、三份契约 | 完成 |
| 后端 | B2 | `/api/v1` 与兼容中间件、拆大文件、发布适配器、Jiuwen 注入 | 完成 |
| 后端 | B3 | 包仓库与装配器、`flow.yaml`、守卫、元模型、统一写入事务、治理平面、密钥与 agent 隔离 | 完成 |
| 后端 | B4a | 三条事件流、会话宿主、执行记录与续跑、启动恢复、agent 提交钩子、Jiuwen worker | 完成 |
| 后端 | B4c | 扩展项、插件产物、拆出 7 个可选包、配额、行业包、知识分层 | 完成，已合回主线，衔接见 ADR-0045，独立审查的 12 条缺陷已修 |
| 后端 | B4b | 变更集、确认生效与撤回、写入范围只读、节点与分段状态、统一下一步、待处理 | **未开始** |
| 后端 | B5 | P3：节点包、证据源（网关、提供方、快照、锚点）、自主构建、网络隔离 | 未开始 |
| 后端 | B6 | P4：版本归因与灰度、评测反馈接入、更多提供方、系统知识包、按项目隔离 agent | 未开始 |
| 前端 | F1～F4 | 34 页 | 完成（mock） |
| 集成 | I1 | 前端接真实后端、端到端走通、逐页对照、前端分支合进交付分支 | 未开始 |

关键路径：B4b → B5 → B6 → I1。

## 2. 分支

| 分支 | 内容 | 远端 |
|---|---|---|
| `feat/onto-modeling-plugin-architecture` | 交付分支：后端、资产、部署、文档。B1～B4 都在这里 | 已推送 |
| `feat/onto-modeling-plugin-frontend` | 前端线：只改 `modeling/onto-modeling/frontend/`，I1 时合进交付分支 | 已推送 |
| `feat/onto-modeling-plugin-p1-governance`、`feat/onto-modeling-plugin-p2-packs` | B3b、B4c 的阶段分支，已合进交付分支 | 不需要单独维护 |

交付分支从 develop 的 2ad2ad7b 拉出，合入目标分支等需求方确认。

## 3. 怎么接着做

### 3.1 先读

1. `design/detailed-design-v1.1.md`：目标状态。"现状"描述和代码不一致时以代码为准。
2. `design/backend-refactor-plan.md`：第 2 章 A～E 编号的改造清单（带代码位置和改法），第 4 章阶段顺序。第 6 章待拍板项一律按"建议"列执行。
3. `../api-v1.md` 加 `contract-changes.md`：前后端都按契约实现。偏离契约要在 `contract-changes.md` 追加一条，写清改了什么、为什么。
4. `progress/gaps.md`：每个阶段留下的差距，按阶段分节。下一阶段开工前先看对应的节。
5. `progress/decisions-log.md`：设计含糊处的选择和理由。
6. ADR：`modeling/onto-modeling/docs/decisions/` 0020 起是这次改造的，0034～0038 是 B4a，0040 起是 B4c。

### 3.2 B4b 从哪开始

范围见 `progress/plan.md` 的"P2 计划拆分"一条，以及详细设计第 13 章 P2 验收。B4a、B4c 已经给它留好了接口：

- **统一下一步（`suggest`）的输入：** 节点与分段状态、写入范围锁。现在只按流程和执行记录算（gaps B4a 第 1 条）。
- **`scope.updated` 的数据源：** 登记前恒为空锁。
- **变更集钩子：** `round_changes` 卡的待确认态、S-05 `open_changeset`。现在 agent 的提交直接进主线。
- **两个登记点：** `registry.register_badge_provider`（左栏角标）、`artifacts.service.register_lock_provider`（写入范围锁），见 gaps B4c。
- **元模型其余调用点改按项目锁定的版本加载**（B3 合并时排给 B4b）。
- **变更清单列表接口：** 已执行、未确认的增量变更清单要能找到所在对话（F3 提出）。

### 3.3 测试

- **后端本地：**

  ```bash
  cd modeling/onto-modeling/backend
  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt pytest
  ./venv/bin/python -m pytest tests/ -q
  ```

  - macOS 上有约 9 个环境性失败（系统自带 git、bash 3.2），不算数；
  - 本机 localhost 走了 HTTP 代理时，先 unset 各个 proxy 变量。
- **Linux 容器全量（验收以它为准）：** `python:3.11-slim` 容器挂载 worktree，装 git 和依赖后跑 `pytest tests/`。既有失败只有 1 个：`test_artifact_gate_safety.py::test_benchmark_real_current_and_baseline_cli`。每个阶段结束都不得新增失败。
- **前端：**

  ```bash
  cd modeling/onto-modeling/frontend
  npm ci && npm test && npm run build
  ```

  - `VITE_API_MOCK=1 npm run dev` 用 mock 服务端打开（生产构建不含 mock），状态切换见 `src/api/mock/index.js` 顶部注释。
  - 视觉对照用 Playwright，1440×900 截图逐页和设计稿比较。

### 3.4 工作约定

开发时给各子任务用的共用规则没有入库，因为里面有本机路径和个人环境配置。下面是其中的要点。


- **先读仓库根 `AGENTS.md` 的"代码库数据安全红线"：**
  - 示例、测试、mock、文档只用合成场景，比如连锁门店补货、外卖订单；
  - 只用保留域名和保留网段；
  - 设计稿第五版的示例场景部分来自真实培训和演示，不得进入仓库，所以设计稿本身没有入库。
- **提交：** 按可独立验证的小块提交，Conventional Commits，scope 用 `onto-modeling`，描述用中文，不加 `Co-Authored-By` 之类的尾注。
- **ADR：** 非平凡的架构决定写 ADR，沿用编号和格式。
- **文档同步：** 用户可见行为、命令、环境变量变了，同步 `modeling/onto-modeling/README.md`、`.env.example`、`deploy/README.md`。
- **测试：** 不删、不跳过、不弱化测试来让检查通过。
- **竞态：** 会话相关的竞态只在 Linux 下稳定出现，改会话宿主后要在容器里循环跑会话相关的测试文件。

## 4. 等需求方决定的事

1. **5 个设计稿没画入口的组件要不要挂上界面。** 分别是链路检索（O-05）、指定所有者（AD-06/07）、管理员审核（AD-08～10）、模型单价（SY-18）、服务令牌（X-06）。组件、mock、单测都写好了，没有挂载；挂载方法见 `progress/frontend-progress.md` F4 节。
2. **求解契约表单里的行业专用字段要不要前后端一起改名。** 这些是仓库原有字段，onto 后端和 consumer 的求解 profile 共用（decisions-log F2 节）。
3. **PL-04、PL-19 要不要在 P3 之前先做一个只落盘的版本。** 它们面向还没有的数据源提供方。
4. **设计稿第五版要不要做一版脱敏稿入库。**
5. **交付分支什么时候、以什么方式合入目标分支。**

## 5. 已知风险

- **CLI 进程持有模型 Key：** Claude CLI 进程自己还拿着模型 Key，同一系统用户能读到，要等 P3 的模型网关代理。
- **agent 未按项目隔离：** 所有项目的 agent 是同一个系统用户，能互相读工作区，也能有意绕过提交钩子，要等 P4 按项目隔离。
- **Jiuwen worker 只在假引擎下测过：** 没在真实模型下跑过端到端，I1 时要跑。
- **前端没接真实后端：** 和后端的字段差异都记在 `contract-changes.md` 各节，`progress/frontend-progress.md` 末尾列了联调前必须对齐的几处。

## 6. 本目录

| 路径 | 内容 |
|---|---|
| `design/detailed-design-v1.1.md` | 插件化架构详细设计 v1.1（评审稿） |
| `design/backend-refactor-plan.md` | 后端改造方案 |
| `design/layered-architecture.*`、`design/architecture-overview.*` | 分层架构图、全景图（HTML 源文件和 PNG） |
| `../api-v1.md` | API v1 契约 |
| `contract-changes.md` | 实现时对契约的补充与偏离，按阶段分节 |
| `progress/plan.md` | 总计划与逐阶段进度记录 |
| `progress/decisions-log.md` | 实现时的取舍 |
| `progress/gaps.md` | 各阶段的差距与遗留 |
| `progress/frontend-progress.md` | 前端逐页进度、视觉对照、未挂载组件 |

`progress/` 下的文档是开发过程中逐阶段追加的记录，文中的本机临时目录（`/tmp/...`）只在原开发机上有。
