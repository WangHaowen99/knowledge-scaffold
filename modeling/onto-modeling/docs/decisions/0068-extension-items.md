# 0068 扩展项补齐：入口、门禁、视图选择、配置、指标与告警、只读工具、节点钩子

## Context

详设 5.6 列了 12 类扩展项。B3a～B5a 做了流程节点、页面入口、提示规则、产物格式、视图声明、hooks、包自带编辑器；
界面入口、门禁、配置项只在 PL-12 里原样列出，没人消费；视图选择、指标与告警规则、只读 MCP 工具（除通用的
`artifact_list/get`）、节点钩子没有。证据源只留接口（ADR-0056），数据源连接向导、证据侧栏、`evidence.*` 工具不出现。

## Decision

声明都写在包清单 `contributes`（节点钩子写在包的 flow.yaml），静态检查多一项 `extensions`（有这几类声明时才出现），
逻辑在 `services/packs/extensions.py`：

1. **界面入口** `entries: [{id, kind: quick_action|extra_action, label, command, where}]`：命令要是本包的；PL-12 只给
   快捷项、附加操作，`source_wizard`（数据源连接向导）随证据源留空、不出现。
2. **声明式门禁** `gates: [{id, label, artifact, on, level, rules, min_count, ratio}]`，`on` 取 save / commit / publish，
   缺省 save（B4c 的行为）：
   - save：网页保存插件产物（AR-05/06）时查，同 B4c；
   - commit：写入事务提交前查（新登记点 `kernel/writepath.register_gate_checker`，详设 4.3 第 3 步、㉖），网页、Studio、
     MCP 与变更集合入都经过它；回退、撤回、插件变更、导入不查；有错误回 `422 gate_failed`，写入回滚；
   - publish：发布前检查（PB-01）的门禁链里每道包门禁一项（`source.kind="pack"`），按发布的那个提交导出产物目录再查，
     支持逐项规则与 `min_count`、`ratio: {field, in, at_least}`（如「推演通过率不低于 80%」）；`level: warning` 只警告。
   规则只管本包登记的产物类型（不能新增实体类型，也不碰本体条目）。
3. **视图选择** `view_choices: [er_view | schema_browser | evidence_sidebar]`：PL-12 多 `view_choices`；依赖证据源的两种
   本期不出现。前端按它显示内核视图在 I1 接。
4. **配置项** `config: [{key, label, type, default, min, max, choices}]`：启动核对后与发布新版本后，各包 stable 版本的配置项
   登记进配置注册表（键 `<包名>.<key>`，来源 `pack`，生效 next_message），SY-14 能看到；PL-12 `config[].key` 同一写法。
   按项目改值没做。
5. **指标与告警规则** `metrics: [{name, help, type}]`、`alerts: [{id, name, condition, window, level, runbook, auto_action}]`：
   指标全名 `onto_pack_<包名，连字符换下划线>__<指标名>`，`/metrics` 每个指标名一个族、HELP 与 TYPE 各一行；告警规则接 O-08～O-10（运维新建的 `source=kernel`
   可改可删；包声明的 `source=<包>@<版本>` 只能停用、删除回 403）。**规则只登记、不评估**：`state` 恒为 ok、`firing` 恒为 0。
6. **只读 MCP 工具** `mcp_tools: [{name, title, artifact, mode: list|get}]`：只能指本包的产物类型；本项目生效中的包的工具
   出现在 MCP 读工具集（名字 `<包名>__<工具名>`），调用时固定类型走 `artifact_list` / `artifact_get` 同一套上限。没有写工具。
7. **节点钩子** flow.yaml `hooks: [{id: <包>.<名>, title, node, when: before|after, command}]`：组装时检查编号、宿主节点存在、
   命令在本次装配里；FL-01 宿主节点多 `hooks`，动作里多一项（之前的排在前、之后的排在后）；用户发宿主节点的命令时，
   宿主在两轮之间提示助手先做或接着做那一步（和提示规则同一个出口，两个引擎都能拿到）。不新增节点。

## Alternatives considered

- **门禁只接发布**：详设 ㉖ 写的是提交和合入时执行；只在发布时查，问题会在主线里留很久。
- **合入门禁改 `kernel/changesets/gates.py`**：那是 B4b 的模块，合入本来就经写入事务提交，在事务里查一处覆盖所有写入方。
- **告警规则先不接 O-08**：没有消费方，声明等于没有；评估器另做，规则形状不用再变。
- **节点钩子做成隐藏节点**：会进进度、影响完成判定，违背「不新增节点」。

## Consequences

- 声明门禁 on 含 commit 的包，网页保存与助手合入都会被它挡住；规则写错会挡住写入，静态检查只查格式。
- 告警评估器、配置项按项目取值、前端显示快捷项与视图选择都是后续项（`_plan/gaps.md` B5c）。

## 补充（B5c 审查修复，审查第 8、9 条）

- 包名里只有小写字母、数字、连字符，工具名、指标名里不许写 `__`，用 `__` 把包名和名字隔开，生成的全名不会和内置工具
  （没有 `__`）或别的包撞名；上传检查再按包仓库里别的包的 stable 版本查一遍重名，列出时也去重（先到先得）。
- `/metrics` 按指标名合成一个族：已记录样本的按实际类型出一行 TYPE，只有声明的按声明类型；不按「有项目启用」筛
  （/metrics 是平台级的，每次抓取都扫项目锁文件不划算，没有样本的 HELP / TYPE 不影响抓取）。
