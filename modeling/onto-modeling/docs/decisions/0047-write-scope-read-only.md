# 0047 助手运行中写入范围只读：一处检查、节点声明范围、守卫按范围收窄

## Context

详设 4.3、14#13 定下：助手的修改确认之前，写入范围内的条目在所有编辑器里只读，持续到确认生效或撤回；范围外
照常编辑、保存即进主线；助手写不出这个范围（守卫按 ㊹ 拦），所以人和助手不会改到同一个条目。方案 C10 要求
ChangeTx 统一检查（命中 423）、MCP 继续回 `PROJECT_EDITING_BUSY` 并附实体列表、删掉 Studio 30 分钟租约和整项目
忙的判断、加 `GET /scope` 与 SSE；D11 要求把守卫里反馈演进的特判泛化成「读 `.onto/run/active.json`，只放行
节点的 produces」，从 P3 提到 P2。

## Decision

1. **范围怎么算（`kernel/changesets/service.scope_lock`，契约 7.6 ScopeLock）**：
   - 助手在跑时：正在执行的节点声明的 `produces` 与 `writes`（见第 3 条），落成 `paths`（fnmatch 模式）；
   - 当前变更集里已改的条目、插件产物（`entries`、`artifacts`）和改动文件的路径（转义后放进 `paths`）；
     变更集里执行过的节点的声明范围也一直算，直到确认生效或撤回；
   - 等你决定的决策卡在改动预览里列出的条目（`Question.affected`，会话宿主登记的数据源给）；
   - `state`：助手在跑是 `assistant_running`，这一轮结束、修改待确认是 `pending_confirm`；自主构建一片的
     `autobuild_slice` 在 P3。按切片收窄（`produces` 落到当前切片的条目）要等切片规划（见 gaps）；现在节点不按
     切片执行，范围就是声明的整类条目（详设 4.3 原文）。
2. **一处检查（`kernel/writepath/scope.py`）**：写入事务登记要写的路径时问一次锁（同一事务复用），落在范围里就
   `423 write_scope_locked`（details 是 ScopeLock），什么都还没写。网页、Studio、MCP、插件产物、知识、求解契约的
   保存都经写入事务，不用各自再查。合入变更集本身（`Origin.changeset` 就是锁住范围的那个）不受限；回退（V-06，
   整树恢复）在有没确认的变更集或助手在跑时一律 423；派生文件（index.md、描述符）不经这里。
   - MCP：写之前按同一份锁判断，命中回 `PROJECT_EDITING_BUSY`，带 `entities`（范围里的条目与产物）；
     「助手在跑就整项目忙」与「条目有 Studio 租约」两条删掉，只保留「条目有你没提交的 Studio 草稿」。
   - Studio：存草稿不再占租约，`acquire_lease` 等删掉；图谱、条目表、条目详情、文件读接口（F-02）、求解契约
     （SV-01）按锁标 `locked` / `lock`；插件产物经 B4c 留下的 `register_lock_provider` 用同一份锁。
   - `GET /scope`（CS-06，`?target=` 只问一个）；`scope.updated`（EP-03）在一轮开始、结束、跑的过程中副本里的改动
     变了、确认生效、撤回时发。
3. **节点声明写入范围（D11）**：节点的 `produces` 是输出（组装时检查两节点不能声明同一路径），另加可选的
   `writes`：本节点运行期间还会改、但不归它产出的路径（修复型节点、跨类改动）。两者合起来就是这个节点运行期间
   允许写入的范围。core 与官方包的声明：

   | 节点 | produces | writes |
   |---|---|---|
   | core.research | `research/*` | — |
   | core.object | `ontology/object/*` | — |
   | core.operation | `ontology/operation/*` | `ontology/logic/*`、`object/*`、`rule/*`、`metric/*`（ADR-0012 分流） |
   | core.rule_metric | `ontology/rule/*`、`metric/*` | `operation/*`、`logic/*`、`object/*` |
   | core.validate | `reports/*-rule.md`、`*-semantic.md` | `ontology/*`（修复） |
   | core.update | — | `ontology/*`、`reports/*` |
   | diagram.flowchart | `ontology/diagrams/*`、`diagrams-summary/*`、`excel/*`、`mermaid/*` | — |
   | trace.mock | `ontology/data-mock/*` | — |
   | trace.trace | `ontology/testcase/*`、`ext/trace/*` | `ontology/data-mock/*` |
   | codegen.codegen | `ontology/scripts/*` | `ontology/operation/*`、`data-mock/*` |
   | codegen.solver | — | `ontology/scripts/*` |
   | evo.evolution | — | 会打包的目录 + `testcase/*`、`data-mock/*`（原守卫特判） |

4. **守卫按范围收窄**：`state_helper acquire <命令>` 把命令所属节点与它的范围写进副本的 `.onto/run/active.json`
   （`{commands: {命令: {node, allow, phase, since}}}`），`release`、`force-release` 时去掉。内核守卫 `scope` 只在
   本体目录（`ontology/`）里收窄：有在执行的命令时，写入要落在它们范围的并集里，越界拒绝并说明能写什么；有不属于
   任何节点的命令（如 `/ontology` 总控）、或文件读不出时不收窄。反馈演进的特判（`lock-evolution.json`）删掉，由
   反馈演进节点的 `writes` 表达。

## Alternatives considered

- **各写入方自己查锁**：Studio、网页、MCP、插件产物各写一份，漏一处就有后写覆盖先写；ChangeTx 是所有写入方的
  必经之路（ADR-0031），放这里只有一处。
- **只锁助手这一轮改过的条目，一轮结束就解锁**：陪伴建模一个变更集跨好几轮，用户改了助手碰过的条目，合入时
  必然冲突（方案第 1 章更正 4.3）。
- **把 `writes` 也算进 produces**：组装时的「同一路径不被两个节点同时声明为输出」检查就没法做了（校验、增量
  更新都会改所有条目）。
- **守卫收窄到 reports/、research/ 等全部目录**：这些目录不是本体条目，用户在编辑器里改不到助手正在写的东西；
  收窄只会多误拒。

## Consequences

- 助手执行对象提取时，所有对象在编辑器里只读，直到这一轮确认生效或撤回；按切片收窄后会小很多。
- 节点包（P3）声明 `produces` / `writes` 后，守卫与只读自动跟上，不用改内核。
- 旧的 Studio 租约文件（`studio/<项目>.leases.json`）不再读写，删除项目时一并清掉。
- 守卫只看本体目录，助手仍能写报告、临时文件；越界拒绝的提示会列出这一步能写的路径。

## 审查修复（2026-11-02）

- 守卫与节点「进行中」只认锁还在、持有者还活着的命令（锁记的引擎实例是工作区当前的引擎，或记的进程号还在）：
  打断、重启没跑到 release 时旧范围不再收窄；撤回、放弃一步、启动恢复时清掉副本里的锁与 `.onto/run/active.json`。
- 资料区守卫同时比对 `resource` 链接后面的真实目录（副本里经主线路径写资料也拦）；内核守卫拦
  `git sparse-checkout`、`git worktree`、改 skip-worktree 与稀疏、排除文件、钩子路径相关配置。
