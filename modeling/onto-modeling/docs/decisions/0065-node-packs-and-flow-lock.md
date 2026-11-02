# 0065 节点包、flow.lock 与插件变更集

## Context

详设 6.9 与方案 D18 定了节点包的生命周期：上传时静态检查，启用时生成新的 `flow.lock`、作为一个变更集自动合入
且可以撤回，下一条消息生效；越过插入位置的项目新节点「可补做」、主线不回退；停用后产物保留、节点「已停用」、
被它挡住的关卡解除；升级后完成判定变了，旧的「已确认」标「需复核」。用户 2026-11-02 定：节点包是能力包的特例，
只带节点声明和节点要用的命令、子代理、脚本、模板，清单保留 `kind: node` 用来做更严的静态检查，插件目录里归进能力包。

B3a 起锁文件改动是一次 `Onto-Source: plugin` 的主线提交；B4b 的变更集（ADR-0046）同一项目同一时刻只有一个没关闭的
（`store.current`），合入走助手副本。插件变更不经过副本，也不能借用助手那个变更集。

## Decision

1. **上传静态检查**（`services/packs/checks.py`，`kind: node` 多一项 `node_pack`）：
   - 只能带 commands、agents、scripts、templates、reference、nodes、flow（及 readme、docs），带 hooks、skill、页面、
     子页、产物登记、视图、编辑器、界面代码（`ui/`）、settings.json 都不通过；
   - 至少一个节点；编号带包前缀；挂法只能是 step / branch / gate / on_demand（主线节点只能随 core 发布），要有锚点；
   - `stale_on`、`requires` 只能写已有的产物类别（不新增实体类型）；完成判定用到的探针要在本包或 stable core 里声明；
   - agent 节点的入口命令要是本包的命令；脚本、人工、外部节点各自的声明见 ADR-0066；
   - `requires_flow` 只能约束 core，并且包仓库里要有满足的 core。
   通用检查顺带修正：写入范围同时查 `produces` 与 `writes`；原来 `lstrip("./")` 会把 `.onto`、`.claude` 的点去掉，
   受保护的点目录从来查不出来，改成只去掉开头的 `./`。
   节点可以写在 `flow.yaml`，也可以按详设 5.2 写成 `nodes/*.yaml`（`contributes.nodes` 列文件），装配时合进流程声明、
   不复制进装配目录。
2. **归类**：清单 `kind: node` 的类别一律是 `capability`（清单自己写 `category: node` 也一样），PL-01 不再返回 `node`
   类别；前端插件中心去掉「节点包」分类，旧数据里的 `node` 按能力包显示。
3. **`.onto/flow.lock`**（`services/flow/flowlock.py`）：锁文件组装出的流程的节点层摘要——编号、来源包与版本、挂法、
   分组、锚点、挡住谁、完成判定签名、过期依据、写入范围，外加 `nodes_hash`。内容只由锁文件决定，启用不带节点的包时
   不变。建项目、导入、克隆时随首个提交写入；之后每次锁文件提交（`commit_lock`）都按新锁文件重组并一起提交。生效仍按
   H1：`/flow` 读生效中的装配目录，`flow.lock` 记的是锁文件要求的那一版；没生效前 FL-01 多一条 `assembly_pending` 提示。
4. **插件变更集**（`services/packs/changes.py`）：PL-06～PL-10 的切换先装配新版本（装配检查就是这类变更的门禁），
   再占一个 `source="plugin"` 的变更集，锁文件与 flow.lock 在同一个带 `Onto-Changeset` 的提交里合入，记录直接置为
   已合入；没有改动时关掉。撤回用变更集的 `revert_merged`（反向提交；之后锁文件又改过就不撤）。PL-10 撤销待生效的切换：
   这个包最近一次插件变更集仍是锁文件最后一次改动时走 `revert_merged`，否则按生效中的版本改回（原做法）。
   - 助手的变更集没关闭时：会改流程节点（`nodes_hash` 变）的切换回 `423 write_scope_locked`——流程定了写入范围和
     待确认节点，换流程会让那组修改对不上；不改流程的切换照常提交、照常记变更集。
   - 变更集记录用 `kernel/changesets/store.record_closed`（只追加已关闭的记录、不动 `current`，B5c 合并时加）：
     提交前记成已撤回并标 `committing`，提交后改成已合入。崩在提交之后的，启动核对（`startup_repair` 里的
     `reconcile_committing`）按 `Onto-Changeset` 补记已合入。
5. **节点状态上与包有关的部分**（`services/flow/overlay.py`，不改 `flow/state.py` 的算法）：
   - 可补做：插件的串入、关卡节点（主线位置 spine 的不算，场景走查这类从 core 拆出的包本来就在流程里）后面的主线节点
     已经在主线上完成、它自己还没完成，标 `backfill`，不计入进度分母（FL-01 与
     项目列表 x/N 同一口径），`suggest` 把它放到第 5 级（「补做「X」」），不抢主线下一步；
   - 已停用：锁文件里 `enabled=false` 的包，节点不在组装后的流程里（挡不住任何节点），从包仓库读出它的声明，以
     `skipped` + `flags.disabled` 列出，不进进度、没有动作；前端进度条不显示已停用节点，只在建模步骤抽屉里；
   - 熔断（O-29）：所属包版本熔断时节点算 `skipped`、没有动作，算状态前去掉它的 `blocks`（关卡解除），FL-05 回
     `409 pack_unavailable`（`reason=breaker`）；
   - 升级标需复核沿用 `state.compute` 的判定签名比对；PL-03 `impact.needs_review_after_upgrade` 用同一判据算出个数。
6. **进度条顺序**：同一锚点后面挂了两个以上主线节点（节点包串入的一步和场景走查都挂在质量检查之后）时，前端
   `trackNodes` 按后端给的节点顺序（组装时已按锚点与包顺序排好），不再只按连线。

## Alternatives considered

- **插件变更走助手副本 + `merge`**：会把助手在副本里没确认的修改一起合进主线；`merge` 还要跑两遍规则检查。不选。
- **助手变更集没关闭时一律拒绝切换**：契约 PL-06 的错误里有 `write_scope_locked`，但不改流程的切换（行业包、联网调研）
  与助手的修改无关，一律拒绝只会让用户先去确认一轮。只拒会改流程的。
- **`flow.lock` 写整份组装后的流程**：探针实现、下一步文案随 core 小版本常变，锁文件会频繁抖动；装配目录里已有整份。
- **已停用节点不列出**：详设 6.9 第 5 步要求标「已停用」；产物还在，用户需要知道它从哪来。

## Consequences

- 每次插件切换多一条变更集记录（CS-01 按 `source` 能筛出来）；存量项目第一次切换时补上 `flow.lock`。
- 助手的修改没确认时记下的插件变更集，要等那组修改确认或撤回后才能 `revert_merged`（撤回要在主线写反向提交，
  回退类写入在助手变更集没关闭时一律 423）；这期间 PL-10 按生效中的版本改回。
- `flow_state.py`（core 工具箱）改了建议算法，core 内容摘要随之变化，跟随通道的项目下一条消息前升级。
