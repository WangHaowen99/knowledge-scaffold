# onto-modeling 插件化改造 · 交接文档

更新：2026-11-03（合入 develop）。本文给接手的人：现在做到哪、代码在哪、怎么接着做、有哪些事等决定。细节都在同目录的其他文档里，这里只给入口。

## 1. 现状

目标是在一个分支上按《插件化架构详细设计》（`design/detailed-design-v1.1.md`，修订记录见其附录 E）和前端设计稿第五版，完成 onto-modeling 的插件化改造。

- **后端：** P0～P3 完成。P3 的证据源（连接器网关、数据库采集、快照、锚点）只留接口（ADR-0056），其中文档转换已经落地（F4a，ADR-0073）。P4 未开始。
- **前端：** 设计稿各页完成，已接真实后端。
- **集成与验证：** 端到端走通，真实模型下做过完整功能验证；之后按需求方手测反馈做了几轮修复（F1～F4a）。
- **测试基线（2026-11-03）：** Linux 容器全量 2792 过 / 1 既有失败；前端 `npm test` 561 过，`npm run build` 通过；端到端 36 项。

| 线 | 阶段 | 内容 | 状态 |
|---|---|---|---|
| 契约 | C0 | API v1 契约 | 完成，见 `../api-v1.md`，偏离记在 `contract-changes.md` |
| 后端 | B1～B3 | 评测移出、`/api/v1`、拆分、发布适配器、包仓库与装配、`flow.yaml`、守卫、元模型、写入事务、治理平面 | 完成 |
| 后端 | B4 | 会话宿主、变更集、节点与分段状态、统一下一步、待处理、扩展项、插件产物、可选包、配额、行业包 | 完成 |
| 后端 | B5 | 节点包、自主构建、包自带编辑器；证据源只留接口 | 完成 |
| 集成 | I1、V | 前后端联调、真实模型完整验证 | 完成 |
| 修复 | F1～F2 | 验证遗留；手测 8 个问题（首次切页白屏、子代理进展、展示与字体、对话里切换构建方式等） | 完成 |
| 修复 | F3 | 去掉「确认生效」（每轮自动合入成版本，只留「撤回这一轮」）；检查在建模中只提示；资料分析停在蓝图确认；「片」改叫「段」 | 完成，ADR-0072 |
| 修复 | F4a | 连接器网关第一版：PDF、Word、Excel 转成带页码的 Markdown 文本版；界面上「证据源」改叫「数据接入」 | 完成，ADR-0073 |
| 修复 | F3b | 插件编辑器保存与脚本节点也只提示；内置包版本号把清单算进去；清理残留的内部机器名 | 完成 |
| 修复 | F4b | 蓝图「要解决的问题」以项目目标为准、去掉「不在范围」；资料分析只读 Markdown；编号这类内部机制不再问人；界面不露工作区路径 | 完成，ADR-0074、0075 |
| 修复 | F4c | 两个引擎都不能直接读资料原件；字段表头读 Excel 文本版；锁恢复不问人；资料分析视图不露路径 | 完成，ADR-0076 |
| 修复 | F4d | Studio 证据出处改成先选资料、再选位置 | 进行中 |
| 后端 | B6 | P4：版本归因与灰度、评测反馈接入、数据库等数据接入实现、系统知识包、按项目隔离 agent | 未开始 |

## 2. 分支

| 分支 | 内容 | 远端 |
|---|---|---|
| `feat/onto-modeling-plugin-architecture` | 交付分支：前后端、资产、部署、文档都在这里 | 已推送（F3、F4a 合入后的提交待推） |
| `feat/onto-modeling-plugin-*` 其他分支 | 各阶段、各修复任务的分支，都已合进交付分支 | 不需要单独维护 |

交付分支从 develop 的 2ad2ad7b 拉出。2026-11-03 从 develop（e85ffb61）拉 `feat/develop-onto-modeling-plugin-architecture`，合入交付分支（aa86e508）后提 PR 到 develop。合并时移植了 develop 上的 CLI 后台任务开关与空闲判定、子任务「已中断」兜底，目标上限改为 2000；旧前端的目标确认卡和 onto 侧评测判分配置没有移植，原因见 `contract-changes.md`「合入 develop」一节。

## 3. 怎么接着做

### 3.1 先读

1. `design/detailed-design-v1.1.md`：目标状态。"现状"描述和代码不一致时以代码为准。
2. `../api-v1.md` 加 `contract-changes.md`：前后端都按契约实现。偏离契约要在 `contract-changes.md` 追加一条，写清改了什么、为什么。
3. `progress/decisions-log.md`：需求方的决定和实现时的取舍，最后几节是最新的。
4. `progress/gaps.md`：每个阶段留下的差距，按阶段分节。
5. ADR：`modeling/onto-modeling/docs/decisions/`，0020 起是这次改造的，0074～0076 是最近三份。

### 3.2 接下来

- **F4d**：Studio 证据出处的选择器。
- **主容器里剩下的四处原件解析**（ADR-0076 遗留）：Excel 预览（`services/excel_preview.py`）、上传时判断资料用途（`services/material_detect.py`）、资料文本提取（`material_text.extract` 调工具箱 `source_extract.extract`，Word、Excel 在后端进程里解析）、评测上传表的一次性迁移（`services/eval_retirement.py`，经 `package_mapping.read_table_headers` 读资料区以外的 Excel）。
- **跳步守卫仍会问人**，按「流程走向不问人」的原则要改。
- **读原件的拦截按命令文字判断**，防误用、防不住有意绕过；彻底解决要等 B6「按项目隔离 agent」。
- **等需求方定的设计**（见第 4 节）定了以后再排。

### 3.3 测试

- **后端本地：**

  ```bash
  cd modeling/onto-modeling/backend
  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt -r connector-requirements.txt pytest
  ./venv/bin/python -m pytest tests/ -q
  ```

  - macOS 上有约 9 个环境性失败（系统自带 git、bash 3.2），不算数；
  - 本机 localhost 走了 HTTP 代理时，先 unset 各个 proxy 变量。
- **Linux 容器全量（验收以它为准）：** `python:3.11-slim` 容器挂载 worktree，装 git、`requirements.txt`、`connector-requirements.txt`（转换器测试要用）后跑 `pytest tests/ -n 6`。既有失败只有 1 个：`test_artifact_gate_safety.py::test_benchmark_real_current_and_baseline_cli`。每个阶段结束都不得新增失败。
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
- **文档只有这一份：** 契约、设计、决策、差距、契约变更只在本目录和 `../api-v1.md` 维护，写的时候就按上一条的口径写；本机的工作底稿只记进度。
- **提交：** 按可独立验证的小块提交，Conventional Commits，scope 用 `onto-modeling`，描述用中文，不加 `Co-Authored-By` 之类的尾注。
- **ADR：** 非平凡的架构决定写 ADR，先按第一性原理写清目标、约束、推导和最强的反对理由，沿用编号和格式。
- **文档同步：** 用户可见行为、命令、环境变量变了，同步 `modeling/onto-modeling/README.md`、`.env.example`、`deploy/README.md`。
- **测试：** 不删、不跳过、不弱化测试来让检查通过。迭代时跑相关测试，收尾跑一次全量。
- **竞态：** 会话相关的竞态只在 Linux 下稳定出现，改会话宿主后要在容器里循环跑会话相关的测试文件。

## 4. 等需求方决定的事

1. **主干与插件挂法：** 主干只放必经的事（资料分析 → 确认蓝图 → 逐段建模 → 检查 → 发布），会改变发布前必做事项的插件用自己的名字插进主干；插件声明「需要什么、产出什么、角色、范围」，位置由平台推算，不再写死节点编号。
2. **第三方转换器怎么安装：** 倾向由网关自己验包的签名，信任的公钥不放在主容器里；PL-04、PL-19 仍回 501。
3. **W3C 本体文件（OWL、RDF）：** 是直接导入成项目模型，还是当参考资料。
4. **上传一份资料出两个版本**（上传一次、转换一次）能不能接受。
5. **插件「安装」的叫法、管理员页头的「安装插件」按钮、普通用户能否上传插件。**
6. **运行开始前的自动决定要不要记进台账。**
7. **交付分支什么时候、以什么方式合入目标分支。**

## 5. 已知风险

- **CLI 进程持有模型 Key：** Claude CLI 进程自己还拿着模型 Key，同一系统用户能读到，要等模型网关代理。
- **agent 未按项目隔离：** 所有项目的 agent 是同一个系统用户，能互相读工作区，也能有意绕过提交钩子，要等 P4 按项目隔离。
- **连接器网关的票据私钥在后端进程里：** 助手和后端同在一个容器，理论上能读到私钥伪造票据（详设 5.8.9）；第一版网关只做转换、没有凭据，影响有限，接数据库前要解决。

## 6. 本目录

| 路径 | 内容 |
|---|---|
| `design/detailed-design-v1.1.md` | 插件化架构详细设计（修订记录在附录 E） |
| `design/backend-refactor-plan.md` | 后端改造方案 |
| `design/layered-architecture.*`、`design/architecture-overview.*` | 分层架构图、全景图（HTML 源文件和 PNG） |
| `../api-v1.md` | API v1 契约 |
| `contract-changes.md` | 实现时对契约的补充与偏离，按阶段分节 |
| `progress/decisions-log.md` | 需求方的决定与实现时的取舍 |
| `progress/gaps.md` | 各阶段的差距与遗留 |
| `progress/frontend-progress.md` | 前端逐页进度、视觉对照、未挂载组件 |
