# 0083 按项目数据出现的节点：内核条件

## Context

**目标**：系统逆向建模包的取证节点 `rev.evidence` 只在项目授权了数据源时出现在流程里；授权的源有一份完整快照就算完成
（ADR-0082）。原来节点只要包启用、装配进流程就一直在，完成判定只能用工作区里的探针（`flow_state.py`）。

**基本约束**

1. 授权存在数据目录（`DATA_DIR/evidence/grants/<项目>.json`），不在工作区：智能体能改工作区，不能让它自己给自己授权。
   工作区里的脚本（`flow_state.py`、`state_helper.py`）读不到授权。
2. 授权随时会变，不经过装配：装配目录按锁文件生成、可以多项目共用，不能把某个项目的授权写进去；`flow.lock` 只由锁文件决定（ADR-0065）。
3. 节点状态的完成判定签名（`state.done_signature`）按探针定义算，定义变了就把已确认的节点标「需复核」。
4. 包代码不在内核里跑：条件只能由内核实现，包只能引用。
5. core 的探针脚本按项目锁定版本加载；改它要发 core 新版本，老项目用不上。

## Decision

内核给一张固定的条件表（`services/flow/conditions.py`），条件名对包是接口，判断是内核代码。现在两条：
`evidence_sources`（项目有授权的数据源）、`evidence_snapshot`（授权的源里有 `complete: true` 的快照）。三处可以引用：

1. **节点 `visible_if: <条件>`**：`model.load_for` 读出装配好的流程后去掉条件不成立的节点，连线、命令归属、钩子一起去掉，
   挂在它后面的节点改挂到它的上游；返回的流程多 `hidden_nodes`，`flow_hash` 换成去掉之后的摘要（节点集合变了，推给前端的
   摘要也要变）。所有读流程的地方（FL-01、项目进度、待处理、自主构建、FL-05）都经 `load_for`，口径一致；FL-05 对不出现的节点回
   `404 node_not_found`。依据：约束 1、2——授权只能内核读、随时变，只能在读流程时按项目算，不能在装配时定。
2. **探针 `{kernel: <条件>}`**：`model.flow_state` 返回的模块包一层，`evaluate`（以及不带探针结果调用的 `node_hits`、
   `progress`、`legacy_stage`）补上内核探针的结果。探针定义不变，完成判定签名就不随结果变（约束 3）；core 的
   `flow_state.py` 不用改（约束 5），它不认识这类探针，在工作区里恒为否。
3. **提示规则 `visible_if: <条件>`**：条件不成立时不注入（`packs/registry.prompt_rules`），用于「有系统快照时」的提醒。

工作区一侧：装配目录的 `flow.yaml` 里还有被去掉的节点。后端写 `.onto/run/flow-state.json` 时把它们记成 `skipped`
（`flags.hidden`），`state_helper.py suggest` 按这份状态算，不会把它们当下一步；`overlay.disabled_nodes` 也不把它们列成已停用。

不认识的条件按不成立处理：新版包引用了旧内核没有的条件时，节点宁可不出现。

## Alternatives considered

- **只给 rev 写死**：改动最少，但条件判断要散在流程、进度、待处理、提示几处各写一份；通用写法只多一张表，
  以后数据库、代码连接器的节点直接用。
- **在 core 的 `flow_state.py` 加探针类型**：工作区也能算完成，但读不到授权（约束 1），还要发 core 新版本（约束 5）。
- **把内核结果改写成工作区认识的探针**（比如换成列出完整快照目录的 `files` 探针）：不用包一层，但探针定义随快照变，
  完成判定签名跟着变，已确认的节点会被误标「需复核」（约束 3）。
- **授权变了就重新装配**：装配目录按锁文件共用，按项目授权装配会让同一锁文件出多份装配，`flow.lock` 也要跟着变（约束 2）。

## Consequences

- 节点出现与否不进 `flow.lock`：锁文件相同的两个项目，流程可以因授权不同而不同；FL-01 的 `flow_hash` 反映的是本项目看到的节点集合。
- 前端收到 `flow.updated` 时只合并已有节点（`mergeFlow`），节点出现或消失要重新取一次 FL-01；授权接口（EV-11）改完授权后要触发
  流程刷新（`project_events.schedule_flow_refresh`）。
- 工作区里的 `state_helper.py detect-stage / check-prereq` 不受影响；`/rev-evidence` 在工作区里照常能用，没授权时命令自己会提示先采集。
- 条件每次读流程都算：读一个授权文件，`evidence_snapshot` 再读几份快照清单，开销可以忽略。
