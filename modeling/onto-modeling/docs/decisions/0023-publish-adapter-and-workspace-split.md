# 0023 发布适配器 SPI，workspace.py 拆成工作区核心、所有权、发布、求解契约

## Context

`services/workspace.py` 有 2800 多行，项目目录与元数据、文件与资料、版本、导入导出、发布渲染与门禁、
求解契约保存与检查都在一个类里（后端改造方案 2.1「workspace.py 拆分归属」）；`package_archive.py` 在
顶层 import 了 `workspace`，而 `workspace` 又在函数里 import 它，是循环依赖。详设 ⑱ 要求发布读主线
版本的提交、经发布适配器输出；方案 A8 给出 `PublishAdapter` 协议，A9 要求版本号单调；API 契约 3.20
把版本号定为 `0.{n}.0`（n 为主线版本号），`contract-changes.md` 第一条补充了它必须严格大于已有的全部
发布标签。图模式（给评测服务导入 Neo4j 用）按方案第 6 章第 1 项留作适配器的变体。

## Decision

1. **拆分**（`services/workspace/` 包，原导入路径不变）：
   - `layout`、`errors`、`gitops`：布局常量与纯函数、错误类型、git 操作，都是叶子模块；
   - `core`：项目目录与元数据、锁与原子写、建项目与进度、改名归档、克隆、删除与清扫、回收站、启动维护；
   - `files`、`versions`、`transfer`：文件与资料、主线版本与标签与整树回退、导入导出与示例注入，
     以 mixin 组合进 `WorkspaceManager`；
   - 所有权的存量迁移（删成员表、报告无主项目）并入 `services/ownership.py`；
   - 求解契约的工作区操作（检查、面板视图、草稿检查、保存、门禁、回执可绑定的修订）移到
     `services/solver/service.py`；契约格式与回执模块本身（`solver_contract`、`solver_validation`）
     随写入路径在 P1（A10）一起搬进 `services/solver/`。
   - `package_archive` 只依赖 `workspace.layout` 与 `workspace.errors`，循环依赖消除。
2. **发布适配器**（`services/publish/`）：
   - `spi`：`ReleaseSource`（项目、来源提交、只读树、元数据、版本号、回执目录，以及变体、降级、草稿、
     预先算好的数据绑定）、`Manifest`（格式、schema 版本、摘要、降级时未绑定的对象）、`PublishAdapter`
     协议（`check`、`render`、`materialize`）；
   - `ontology_package`：知识包适配器，ontology-package v0.1，启用求解契约时升为 v0.2；`variant=graph`
     是图模式变体；`materialize` 是已发布知识包的反向导入（工作区导入走它）；
   - `tree`：按提交 `git archive` 导出只读树并核对；`gates`：门禁链（数据绑定、求解契约回执、知识包结构）；
   - `versioning`：`0.{n}.0`，n 为发布提交的 `--first-parent` 计数；该提交已发布过就沿用；不大于已有最大
     发布标签（含旧版 `0.1.N`）时取最大版本的补丁号 + 1；
   - `service`：发布流程——确定提交 → 只读树 → 门禁 → 渲染 → 留存（先留存再打标签）→ 打
     `release/<版本>` 注释标签 → 写发布记录；另有发布检查、对象数据来源修复、发布记录、按版本取包、
     求解验证候选包。
3. **逐字节回归**：重构前用原渲染器对合成项目渲染五个基准包（普通、图模式、降级、求解、求解候选），
   存为 `tests/fixtures/publish_baseline/*.pkg`；`tests/test_publish_render_regression.py` 断言同一提交、
   同一版本参数下新适配器渲染的字节与基准完全相同。

## Alternatives considered

- **把 WorkspaceManager 上的发布方法留作转发**：调用方改动少，但工作区核心仍知道发布，拆分名存实亡；
  调用方（路由、三份契约、测试）改成调发布模块的函数。
- **一个类拆成多个服务对象**：调用方要改成到处传多个对象；mixin 保留 `WorkspaceManager` 这一个入口，
  模块按职责分开，后续写入路径（C1）接手文件写入时再收窄。
- **用每次重新渲染代替基准包**：重构本身会改渲染器，自比自证明不了没变；基准必须来自重构之前。

## Consequences

- 发布版本号从 `0.1.N` 改为 `0.{n}.0`。存量项目第一次发布：n 不大于已有的 `0.1.N` 时继续在旧版本上递增
  补丁号，不会倒退；下游按版本去重不受影响。
- 有意改动包格式时，要先确认 consumer 与 modeling 的导入兼容，再按测试说明重新生成基准并在提交说明
  写清原因。
- 求解契约与写入路径在 P1 继续收口（A10、C1）；包清单的 `packs`、`flow_hash` 随插件运行时（P1）填上。
