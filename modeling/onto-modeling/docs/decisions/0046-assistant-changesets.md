# 0046 助手变更集：副本、确认生效与撤回、实体级差异

## Context

详设 1.6、8.2 把变更集定为内核机制：单人项目里用来「确认生效 / 撤回这一轮」、隔离助手的改动、崩溃恢复。
P1 之前助手直接在项目工作区里改、自己 `git commit` 进主线，带来四个问题（详设 8.2）：助手的写入可能盖掉你在
同一轮里的保存；产物变更卡把你这段时间在 Studio 的保存也算成助手的；撤回一轮只能在主干上回退，连带撤掉
中间的人工修改；文件一落盘就进了主干，没有「已确认」的边界。方案 C7～C9、C12、C14、C15 给了做法：
git worktree 副本、`merge-tree --write-tree` 合成、按实体检测冲突、在合成树上跑门禁、经 ChangeTx 写 squash 提交。

## Decision

1. **副本（C7）**：每个项目一个 git worktree，路径固定 `<数据目录>/changesets/<项目>/assistant`，分支固定
   `onto/assistant`。Claude 的续接记录按工作目录存，路径不能变；副本删了重建时接着原分支，没确认的提交还在。
   - `resource/` 用 sparse-checkout（`/*`、`!/resource/`）排除，再挂一条指向主线工作区 `resource/` 的相对链接：
     资料各副本共享、上传后助手立刻能读，写入由内核守卫拦。副本自己的排除规则（`core.excludesFile`，按 worktree
     配置）排除这条链接，`sparse.expectFilesOutsideOfPatterns=true` 让经链接读得到的资料不被当成稀疏外文件，
     `git add -A`、`git commit -a` 都带不进去。
   - `.claude` 是指向主线工作区 `.claude` 的链接（链到链），装配一切换两边同时生效；`CLAUDE.md`、`README.md`
     同主线，指向 `.claude/` 里。`.ontology-state/`、`.onto/run/` 每个副本一份。
   - 建副本、撤回之后把主线上的验收回执（资料分析验收、脚本验证）带进副本；合入时把副本里新出的回执带回主线。
     回执绑定内容指纹，带过去不会误判。
   - 一轮开始前（会话宿主 `_deliver`）建或修副本并跟上主线：副本相对基准没改动就直接重置到主线最新；有提交、
     没有未提交文件、主线前进了且合得上，就把主线合进副本分支（详设 8.2「没有冲突时自动跟上主线」）。建不了
     副本（不是 git 仓库、git 出错）时这一轮退回主线工作区并记日志。
   - 启动恢复登记 `changeset_copies`（order 88，在执行记录改判之前）：清 worktree 的残留锁、清掉丢了的登记、
     重建坏了或丢了的副本、补链接；有修复或修不了才进 O-35 报告（同 `agent_commit_hooks`）。
   - 删除项目时一并清掉变更集记录与副本目录。agent 用户（E10）对 `changesets/` 与 `workspaces/` 一样可写。
2. **变更集记录**：数据目录 `changeset-state/<项目>.json`（不进 git、不在 agent 可写的目录），编号 `cs-<n>`。
   同一时刻最多一个没关闭的助手变更集。状态 `open`（助手在跑）⇄ `pending_confirm`（这一轮结束）→ `merged` /
   `discarded`；合入时实体冲突进 `conflict`。助手把改动又改回去时空变更集悄悄关掉（`discarded`）。
   副本里有改动就开变更集：一轮结束时看一次（带门禁检查、出卡片），跑的过程中每 5 秒最多看一次（只更新
   写入范围，见 ADR-0047）。改动的描述用副本的快照树（拷一份索引 `add -A` 后 `write-tree`），不碰副本的真实索引。
3. **确认生效（C8，`kernel/changesets/service.merge`）**：
   1. 助手还在跑（会话宿主登记的数据源说在跑）→ `409 changeset_in_progress`；已关闭 → `409 changeset_closed`；
   2. 副本里没提交的改动先收成一个提交；`git merge-tree --write-tree <主线> <副本分支>` 叠到最新主线；
   3. 按实体检测冲突（C9）：主线与变更集都改到的条目，两边各自改到了章节、字段或关系、且改成的不一样，就是
      冲突；不是条目的文件看 git 三方合并有没有文本冲突。有冲突 → 变更集记 `conflict`、`409 merge_conflict`
      （`entities`、`inbox_item_id=changeset_conflict:<cs>`）；CS-07 选好每一处保留哪一边后重试合入；
   4. 门禁：主线树与合成树各导出 `ontology/`、`research/` 到临时目录，用本项目锁定版本的 `ontology_checker.py`
      （装配目录里哈希核对过的）各跑一遍，按 `candidate_revision._minus` 相减，只看新增问题；新增错误 →
      `422 gate_failed`（`findings` 只列错误），新增警告只记下；
   5. 经写入事务写一个 squash 提交：把主线到合成树之间改动的文件写进主线工作区再提交。trailer 写在这个合入提交上：
      `Onto-Source: agent`（自主构建是 `autobuild`；构建方式由来源表达，不新增 trailer 键）、`Onto-Author`
      （项目所有者）、`Onto-Changeset`、`Onto-Packs`、`Onto-Node`（变更集里最近一个节点）；
   6. 副本重置到合入后的主线；记 `merged`、专项审计 `changeset.merge`；项目流 `changeset.updated`、
      `scope.updated`、`workspace.changed`（来源 assistant，带 changeset_id）、`flow.updated`；对话里这个变更集的
      卡片全部更新为已确认，加一条 `notice`。
4. **撤回（`discard`）**：副本分支与副本重置到主线（`reset --hard` + `clean -fd`，链接与工具状态在排除规则里
   不受影响），主线不动；专项审计 `changeset.discard`。
5. **实体级差异（C9，`kernel/changesets/entity_diff`）**：条目按元模型拆成章节、表格行（第一列为键）、关系
   （关系章节里的链接）。解析放进元模型脚本 `onto_metamodel.parse_document`（契约库按项目锁定版本加载），
   后端不再写一份。CS-03 的 `sections[].lines` 按章节给：表格章节逐行给删 / 增，正文按行给统一差异。
6. **产物变更卡（C12）**：基点是变更集基点，在副本里算；一轮有改动就放一张新卡（`rc-<cs>-<n>`），同一变更集之前的
   卡更新到最新内容，按钮作用于同一个变更集。`state`、`changeset_id`、`checks`（相对基准新增的问题）、
   `lock_note`、`next_version_label` 给真实值；S-05 的 `open_changeset` 是这条对话里没确认的变更集。
   助手在副本里的提交不再发 `workspace.changed`（主线没变），合入时由写入事务发。这一轮改到的插件产物在这张卡
   之后各出一张产物卡，路径按这一轮在副本里的改动、内容从副本读（ADR-0055 补充第 2 条）。
7. **版本编号（C14）**：主线按 `--first-parent` 计数（B2 已改），合入是一个 squash 提交，主线版本只 +1；
   侧枝合并（`--no-ff`）也只计一次。
8. **Studio 复核草稿（C15）**：放进副本的 `.ontology-state/studio-review/`，助手才看得到。

### 给 P3（B5b 自主构建）的接口

都在 `app/kernel/changesets/service.py`，调用方在事件循环外（线程里）调；对话卡片用 `card_records` /
`notice_record` 生成后在事件循环里追加（会话订阅队列不是线程安全的，参照 `routers/changesets.after_close`）。

| 用途 | 函数 | 说明 |
|---|---|---|
| 一片的副本 | `copy_for_turn(manager, project_id, root)` | 每片开始前调；返回助手工作目录 |
| 每片先占一个变更集 | `open_changeset(manager, project_id, source="autobuild", build_mode="L4", run_id=..., slice_id=..., purpose=...)` | 已有没关闭的就返回它 |
| 观察一片的改动 | `observe(manager, project_id, running=..., conversation_id=..., exec_record=..., checks=True)` | 往当前变更集里记改动，返回记录 |
| 自动合入 | `merge(manager, project_id, cs_id, actor=..., source="autobuild", needs_review=True)` | 门禁不过抛 `gate_failed`，冲突抛 `merge_conflict`（进待处理）；`needs_review` 记在变更集上 |
| 撤回已合入的一片 | `revert_merged(manager, project_id, cs_id, actor=..., source="revert")` | 在主线写反向提交；合入后同一实体又被改过时 `409 merge_conflict`，不撤 |
| 撤回未合入的一片 | `discard(manager, project_id, cs_id, actor=...)` | 副本重置 |
| 会话状态 | `register_activity_provider(fn)` | 已由 `app/wiring.py` 接会话池；自主构建的 run_id 由运行器写进变更集 |

## Alternatives considered

- **真合并提交（两个父提交）**：保留助手在副本里的逐次提交历史，`--first-parent` 计数也只 +1。没选：方案写的是
  squash；副本里的提交常是半成品（「fix: address rule check issues」），进主线历史只会让「版本历史」页变乱；
  合入提交带 `Onto-Changeset`，要追溯可以按变更集记录。
- **副本放在工作区里（`.onto/run/assistant`）**：工作区要整树导出、克隆、备份，嵌一个 worktree 进去会被一起打包；
  路径也不够稳定（项目改目录时）。
- **`resource/` 整份检出到副本**：大资料重复占盘，上传后的资料要等跟上主线才看得到。
- **按行三方合并判冲突**：本体文档靠 `[[链接]]` 互相引用，按行合并可能「合并成功」却悄悄断链（详设 8.2）；
  按实体判更保守，正常情况下写入范围只读已保证不会冲突。
- **合入时只跑提交检查（pre_commit_check）**：它只看暂存的文件；合成树上全量跑一次再相减，才能发现「这一轮让
  别的条目的引用断了」这类新问题。

## Consequences

- 助手的改动在确认之前不进主线：流程进度、发布、版本列表都只看主线，「进度不再因为文件落盘就打勾」有了基础。
- 每个项目多一个副本目录（不含资料），大小约等于工作区去掉 `resource/`。
- 合入要跑两遍规则检查（主线那一遍按树缓存），大项目确认生效会慢几秒；检查脚本跑不起来时 `503`。
- 副本里的 git 配置（稀疏规则、排除文件）用 `extensions.worktreeConfig`，主线仓库的 git 版本要 ≥ 2.38
  （`merge-tree --write-tree`）；镜像是 Debian 的 git，满足。
- 建不了副本时退回主线工作区：功能不丢，只是没有隔离，日志里有记录。

## 审查修复（2026-11-02）

独立审查查出的问题按下面改（用例 `tests/test_b4b_review_kernel.py` 等）：

- **受保护路径**：资料区与锁文件（`resource/`、`.onto/`）不进快照、不进收拢提交（`git add -A` 后恢复成 HEAD），合入时一律取
  主线的版本；每轮开始核对稀疏规则与资料条目的「不检出」标记，被改过就重设。
- **副本不在副本分支上**：收住改动接回副本分支，分叉合不上留 `refs/onto/rescued/<时间>`；目录真坏了挪到
  `assistant.broken-<时间>`（只留最近 3 份），不删。
- **合入**：全程持项目锁，锁内重读变更集；git 报文本冲突的路径一律算冲突；合成结果和主线一样时不写提交
  （`merged.commit` 为 null，不能 `revert_merged`）；写入意图标成整片回滚，崩溃后启动恢复回到合入前；启动时按
  `Onto-Changeset` 补记已合入、没记下的变更集（`startup_repair`）。
- **撤回已合入的变更集**：派生文件（`index.md`、脚本描述符）不比对、由写入事务重新生成；其余文件后来有任何改动就不撤。
- **先占的变更集**（`open_changeset`）没改过之前不当成空的关掉；`open_changeset`、`revert_merged` 签名不变。
- **回执**每轮收尾带回主线，两边同步只拿新的。
