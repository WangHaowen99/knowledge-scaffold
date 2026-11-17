# 能力包（capability）与节点包（node）

**结论先说**：给助手加命令、子代理、skill、脚本、模板或提示规则时用能力包；要在建模流程里多一个节点时用节点包。两种包的文件都按原相对路径复制到项目的 `.claude/`，下一条消息生效。节点包比能力包严：只能带命令、子代理、脚本、模板、参考、文档和流程声明。

名字规则、取值列表、受保护目录、图标集等常量都见 protocol.md，本文不重抄。

## 1. 选包类型

| 开发者要做的事 | 用哪种 | 骨架 |
|---|---|---|
| 加命令、子代理、skill、脚本、模板、参考、提示规则 | `kind: capability` | `examples/capability/sample-notes/` |
| 让内置页显示自己的数据（产物登记、视图） | `kind: capability` | 同上，再加第 8、9 节的内容 |
| 在流程里加一步：校验后复核、发布前确认、脚本统计 | `kind: node` | `examples/capability/sample-review/` |
| 既要流程节点，又要 skill、页面或产物登记 | 拆成一个节点包和一个能力包 | 上面两个都用 |

`pack_tool.py init <名字> --kind capability`（或 `--kind node`）复制骨架，把 `sample-notes`、`sample_notes` 这类字样全部换成新名字。改完名字后要改的：`label`、`description`、命令正文、脚本逻辑、节点的 `title` 和挂载位置。`checksums.sha256` 由 `pack_tool.py build` 生成，不手写。

## 2. 清单

```yaml
name: sample-notes
version: 0.1.0
kind: capability
category: capability
label: 纪要术语整理
description: 从会议纪要提取术语，按模板写成术语表。
publisher: {kind: tenant, label: ""}
kernel_api: ">=1.0 <2.0"
engines: [claude, jiuwen]
changelog: 首个版本。
contributes:
  commands: [sample-notes-run]
  agents: [sample-notes-agent]
  skills: [sample-notes-terms]
  scripts: [sample_notes_terms.py]
  templates: [sample-notes-glossary.md]
```

| 字段 | 要点 |
|---|---|
| `name` | 格式和保留名见 protocol.md；已被别的包占用时私有上传不通过 |
| `version` | semver；同一版本不能重复入库，改了内容就升版本 |
| `kernel_api` | 必填，写成范围 |
| `engines` | 缺省同时支持两个引擎 |
| `publisher` | 只用于显示，写 `official` 也不算官方包 |
| `category` | `kind: node` 时固定归为 `capability` |

## 3. 各项一览

所有落点都相对项目根目录。`.claude` 是指向装配目录的软链，每次装配重建，产物不能写在这里，写 `ext/<包名>/` 或 `reports/`。

| 项 | `contributes` 写法 | 包内文件 | 装配后落点 | 助手怎么用 | 静态检查挡什么 |
|---|---|---|---|---|---|
| 命令 | `commands: [sample-notes-run]` | `commands/<名>.md` | `.claude/commands/<名>.md` | 输入 `/<名> 参数` | 缺文件；没带 `<包名>-` 前缀；与 core 或别的包重名 |
| 子代理 | `agents: [sample-notes-agent]` | `agents/<名>.md` | `.claude/agents/<名>.md` | 命令里写明读这个文件并派出 | 缺文件；与 core 重名 |
| skill | `skills: [sample-notes-terms]` | `skills/<名>/SKILL.md` | `.claude/skills/<名>/SKILL.md` | 按 `description` 选用，或命令里写路径要求读 | 缺目录；与 core 重名 |
| 脚本 | `scripts: [sample_notes_terms.py]` | `scripts/<名>` | `.claude/scripts/<名>` | `python3 .claude/scripts/<名>` | 缺文件；语法错误；用了守卫保留名；与 core 重名 |
| 模板 | `templates: [sample-notes-glossary.md]` | `templates/<名>` | `.claude/templates/<名>` | 命令或子代理里写路径 | 只查名字格式，缺文件不报 |
| 参考 | `reference: [sample-notes-guide.md]` | `reference/<名>` | `.claude/reference/<名>` | 同模板 | 同模板 |
| 文档 | `docs: [SAMPLE-NOTES-GUIDE.md]` | 包内相对路径 | `.claude/<同一路径>` | 同模板 | 路径越界 |
| 常驻规则 | `rules: [sample-notes-rules.md]` | 包内相对路径 | 拼进工作区 `CLAUDE.md` | 每轮都在上下文里 | 计入提示词预算 |
| 提示规则 | `prompts: [...]` | 写在清单里 | 不落文件 | 用户消息命中时注入 | 写错的正则不报错，运行时跳过并记日志 |

**文件名都带包名前缀。** 静态检查只拦命令重名；模板、参考、文档撞路径时上传和安装都能过，到启用后到的那个包才返回 409 `pack_incompatible`。

**有文件没声明**只给 warn，文件照样会被复制，但清单不能反映包内容，发布前要补全。

命令、子代理、skill、规则合计有体积上限，见 protocol.md。

## 4. 命令、子代理、skill

命令是一段带 frontmatter 的 Markdown，`$ARGUMENTS` 是用户输入的占位，样板见 `sample-notes/commands/sample-notes-run.md`。`description`、`argument-hint` 显示给用户，`allowed-tools` 可选。参数为空时怎么办要写进正文，否则助手会自己猜。

子代理不用预注册。命令里这样写，两个引擎都能执行（jiuwen 引擎用 general-purpose 子代理承接）：

```markdown
读 `.claude/agents/sample-notes-agent.md`，作为子代理（Task）的 prompt 派出，把纪要路径和候选术语一起交给它。
```

子代理文件可以没有 frontmatter，也可以写 `name`、`description`、`tools`。文件末尾写清要返回什么，只返回结论和文件路径，全文留在产物里。

skill 的 `SKILL.md` 必须有 `name` 和 `description`，`description` 写清何时使用。要保证被读到时，在命令或子代理里写出路径，`sample-notes-agent.md` 就是这样做的。

## 5. 脚本

在命令里按 `python3 .claude/scripts/<名>` 调用，运行环境是助手所在的工作区，当前目录是项目根目录。

- 只用标准库；用第三方包时要确认运行环境里有。
- 输出给助手看的内容要限量，例如只输出前 20 条和总数。`sample_notes_terms.py` 是样板。
- 想写报告时把路径作为参数传入，写到 `ext/<包名>/` 或 `reports/`。
- 脚本名不能和内核守卫同名，规则见 protocol.md。

节点里的脚本运行方式不同，见第 11 节。

## 6. 提示规则（prompts）

会话宿主在两轮之间用 `match` 里的正则匹配用户消息，命中就把 `text` 注入给助手。两个引擎都拿得到。

```yaml
# manifest.yaml 的 contributes 追加（提示规则）
prompts:
  - id: sample-notes-hint
    match: ["会议纪要"]
    text: >-
      用户提到了会议纪要。需要整理术语时，用 /sample-notes-run 加纪要路径。
```

`match` 里任一条命中就注入，相同文本去重。匹配面要窄，`text` 要短：命中一次就占一次上下文。每轮都要遵守的约定才写 `rules`，只在特定说法下需要的写 `prompts`。

## 7. 页面

页面只能选内置页，id 和可用图标见 protocol.md。包里不能带自己的前端代码，`ui/` 之外出现前端文件直接 fail，租户包的 html 也不能带脚本。

```yaml
# manifest.yaml 的 contributes 追加（页面）
pages:
  - id: scenarios
    page_api: ">=1.0 <2.0"
    nav: {title: 术语表, icon: book, order: 3}
    data: {artifacts: [sample-notes.term]}
    actions:
      - {id: run, label: 整理纪要, command: /sample-notes-run, hint: 带上纪要路径}
```

- `data` 决定页面读什么：`artifacts`、`reports`、`nodes`。
- `actions` 的 `command` 是点按钮时发给助手的命令，`hint` 是提示语。
- `badge: inbox` 显示待处理数。
- 同一个页面 id 先到先得：两个启用的包都选 `scenarios` 时，后到的不显示，用之前要确认项目里没有别的包占用。
- 子页写 `subpages: [{parent, id, title}]`，`parent` 和 `id` 都只能是内置值；现在没有内置子页可选，见 `protocol.md`。

## 8. 产物登记

产物登记告诉平台某类文件的结构，内置页才能列出、编辑、校验它。文件放 `ext/<包名>/` 下。

```yaml
# artifacts/sample-notes-term.yaml
type: sample-notes.term
title: 术语
path: ext/sample-notes/terms/{key}.md
format: md-frontmatter
key: filename
schema:
  version: "1"
  fields:
    - {name: title, type: text, label: 术语, required: true}
    - name: state
      type: enum
      label: 状态
      required: true
      enum:
        - {value: confirmed, label: 已确认, tone: green}
        - {value: pending, label: 待确认, tone: gray}
    - {name: object, type: ref, label: 关联对象, required: false}
sections: [释义, 来源]
refs:
  - {field: object, kind: object}
status: {field: state, map: {confirmed: 已确认, pending: 待确认}}
index: ext/sample-notes/index/terms.jsonl
```

清单里声明：

```yaml
# manifest.yaml 的 contributes 追加（产物与视图）
artifacts: [artifacts/sample-notes-term.yaml]
views: [views/sample-notes-term.yaml]
```

| 规则 | 说明 |
|---|---|
| `type` | 写成 `<包名>.<名字>`，不能和五元组重名 |
| `path` | 只有一个 `*` 或 `{key}`，租户包必须在 `ext/<包名>/` 下 |
| `format`、字段 `type` | 取值见 protocol.md |
| `refs` | `kind` 只能是五元组之一 |
| `index`、`extras` | 只能是 `ext/<包名>/` 下的 `.jsonl` |
| `schema` | 可以内联，也可以写成包内文件路径 |

装配时这些 yaml 也会复制到 `.claude/` 对应路径。

## 9. 视图与扩展项

视图挂在产物类型上，决定列表、详情、卡片显示哪些字段。

```yaml
# views/sample-notes-term.yaml
artifact: sample-notes.term
list: {columns: [title, state, object], filters: [state]}
detail: {layout: sections, side: [state, object]}
card: {title: title, chips: [state]}
```

`artifact` 必须是本包登记的类型；`card` 的 `chips`、`fields` 各最多 4 个。

扩展项写在 `contributes` 下，声明了任意一项才会检查。

```yaml
# manifest.yaml 的 contributes 追加（扩展项）
entries:
  - {id: quick-run, kind: quick_action, label: 整理纪要, command: /sample-notes-run}
gates:
  - id: term-short
    label: 术语名不超过 20 字
    artifact: sample-notes.term
    on: [save]
    level: error
    rules:
      - {field: title, max_length: 20}
config:
  - {key: max_terms, label: 每次最多收录几条, type: int, default: 20, min: 1, max: 100}
metrics:
  - {name: terms_added, help: 收录的术语数, type: counter}
alerts:
  - {id: empty-rate, name: 空结果比例, condition: "empty_rate > 20%", window: 1h, level: warning, runbook: 检查纪要格式}
mcp_tools:
  - {name: terms, title: 列出术语, artifact: sample-notes.term, mode: list}
  - {name: term, title: 读取术语, artifact: sample-notes.term, mode: get}
```

| 项 | 作用 | 静态检查挡什么 | 私有包 |
|---|---|---|---|
| `entries` | `quick_action` 显示在输入框旁；`extra_action` 显示在插件页页头，包要有页面 | `command` 不是本包命令 | 生效 |
| `gates` | 在 `save`、`commit`、`publish` 时查本包产物；`rules` 每条有 `field` 加 `required`、`in`、`max_length`、`ref_exists` 之一 | `artifact` 不是本包类型；`min_count`、`ratio` 没配 `on: [publish]` | 生效 |
| `config` | 登记配置项，全名 `<包名>.<key>`；`type: enum` 要写 `choices` | `key` 格式、类型不认识 | 不生效 |
| `metrics` | 登记指标，全名 `onto_pack_<包名下划线化>__<name>` | 名字格式、含 `__`、类型不认识 | 不生效 |
| `alerts` | 登记告警，要有 `id`、`condition`；`auto_action` 只能是 `breaker` | 缺字段、级别不认识 | 不生效 |
| `mcp_tools` | 给外部调用方读本包产物，全名 `<包名>__<name>`；`mode` 取 `list` 或 `get` | `artifact` 不是本包类型 | 生效 |

`config`、`metrics`、`alerts` 是全局注册项，只有管理员发布成公开包后才生效。

## 10. 节点包：结构与清单

节点包的 `contributes` 只能写 `commands`、`agents`、`scripts`、`templates`、`reference`、`nodes`、`flow`、`readme`、`docs`，不能带 `skills/`、`ui/`、`settings.json`，至少声明一个节点。

清单和能力包的差别是 `kind` 与 `contributes`，完整写法见 `sample-review/manifest.yaml`：

```yaml
# 节点包 manifest.yaml 的差别部分
kind: node
contributes:
  commands: [sample-review-run]
  scripts: [sample_review_stats.py]
  flow: flow.yaml
  nodes: [sample-review.check, sample-review.stats]
```

`flow.yaml` 顶层字段：

| 字段 | 写法 | 说明 |
|---|---|---|
| `requires_flow` | `core>=0.3` | 必须以 `core` 开头；仓库里要有满足它的 core 版本。挂在主线节点上时不要写得比 0.3 低，0.3 起才有 `core.validate`（`core.object`、`core.operation` 在旧版 core 里按别名认成建模节点） |
| `probes` | 见下文 | 探针名不能和已有的重复，用 `<包名下划线化>_` 开头 |
| `commands` | `sample-review-run: {node: sample-review.check, requires: []}` | 把命令关联到节点，运行时节点才显示进行中。键写命令全名；目标节点必须存在 |
| `nodes` | 见下文 | 节点列表 |
| `hooks` | `{id: <包名>.<名>, title, node, when: before, command: /本包命令}` | 节点钩子：这个节点的命令运行前后提示助手多做一步 |

探针判断节点是否完成，写法有三种：

```yaml
probes:
  sample_review_check: {files: [reports], glob: sample-review-check.md}  # 目录里有匹配的文件
  sample_review_todo: {pending: [reports], glob: sample-review-*.md}     # 文件里还有 [ ]
  sample_review_tag: {tags: "release/*"}                                 # 有匹配的 git 标签
```

`files` 探针不计隐藏文件和 `index.md`。节点的 `done` 里可以用本包或 core 的探针，`!名字` 表示取反。core 的探针名见 protocol.md。

节点字段：

| 字段 | 说明 |
|---|---|
| `id` | `<包名>.<名字>` |
| `title` | 必填，面板上显示的名字，用动宾结构 |
| `kind` | `agent`、`script`、`human` |
| `placement` | `step`、`branch`、`gate`、`on_demand`；`spine` 只给 core |
| `section` | 面板分组，取值见 protocol.md |
| `after`、`before` | 挂载锚点；不是 `on_demand` 的节点至少写一个 |
| `display_after` | 只影响显示顺序 |
| `blocks` | 被它挡住的节点，发布关卡写 `[core.publish]` |
| `done` | 完成条件，探针名列表 |
| `stale_on` | 这些类别的内容改了，节点变成过期；类别见 protocol.md |
| `produces` | `[{path, kind}]`，节点产出；不能碰受保护目录 |
| `writes` | 节点会改但不算产出的路径 |
| `optional` | `true` 表示可以不做，下游不等它 |
| `progress` | `false` 表示不计入进度 |
| `visible_if` | 内核条件名，不成立时节点不在这个项目的流程里；现在只有 `activated`（本包声明的 `activation` 触发了，或这一轮开着）。内核探针写 `{kernel: session_open, session: <这一轮的状态文件>}` |
| `preempt` | 只能和 `visible_if: activated` 一起用：节点出现且没做完时，下一步建议先推它。写字符串时当说明行 |

## 11. 节点包：三类节点

| `kind` | 必填 | 怎么跑 |
|---|---|---|
| `agent` | `entry.command`，且是本包命令 | 用户在面板点动作或输入命令，助手按命令干活 |
| `script` | `script.name`（本包脚本）、`script.output` | 面板启动，在沙箱里跑，标准输出写进 `script.output` |
| `human` | `decision.title`、至少 2 个带 `id` 的 `options`、`decision.record` | 面板弹决策卡，回答写进 `record` 文件 |

`script.output` 和 `decision.record` 必须落在本节点的 `produces` 或 `writes` 里。

`agent` 和 `script` 节点的完整写法见 `sample-review/flow.yaml`。样例里没有的人工关卡这样写，`sample_review_approve` 要在 `probes` 里声明：

```yaml
# flow.yaml 的 nodes 追加（human 关卡）
- id: sample-review.approve
  title: 确认命名结论
  kind: human
  placement: gate
  section: pre_publish
  after: sample-review.check
  blocks: [core.publish]
  done: [sample_review_approve]
  produces:
    - {path: reports/sample-review-approve.md, kind: report}
  decision:
    title: 命名复核的结论是否接受
    background: 复核报告在 reports/sample-review-check.md
    options:
      - {id: accept, label: 接受, consequence: 进入发布}
      - {id: rework, label: 退回, consequence: 回到建模修改}
    record: reports/sample-review-approve.md
```

脚本节点的运行环境和第 5 节不同：

- 平台按 `script.inputs` 收集文件，打包成 `inputs/<相对路径>`，在沙箱里执行 `python3 -u <脚本> inputs [script.args...]`。
- 不联网，只能用标准库或沙箱里已有的包。
- 限制：输入最多 200 个文件，打包后不超过 96KB；运行不超过 20 秒；标准输出不超过 1500 字符，超了算失败。
- `inputs` 不能读 `.git`、`.claude`、`.onto`、`.ontology-state`。
- `inputs` 是相对工作区的 glob：`*` 不跨目录，`**` 递归。对象和操作可能放在子目录里，要写全：`ontology/object/**/*.md`、`ontology/operation/**/*.md`。
- 脚本不写文件，结果全打到标准输出，平台写进 `script.output` 并提交。
- 沙箱不可用时返回 503，不会回退到本机运行。

## 12. 节点怎样进主流程

启用节点包时，平台把包的 `flow.yaml` 和 core 的流程组装成一份，写到 `.claude/flow.yaml`（JSON 写法），节点带 `pack` 字段。

| 字段 | 效果 |
|---|---|
| `after: core.validate` | 加一条边。上游确认或过期后，本节点变成可开始；上游是没做完的可选节点时继续往上找 |
| `after: [trace.trace, core.validate]` | 取第一个存在的锚点，用于上游包可能没启用的情况 |
| `before: core.publish` | 只加一条边。没写 `after` 时节点一开始就可开始 |
| `blocks: [core.publish]` | 本节点没确认时目标节点被挡住，并进入发布关卡 |

**范围限定 `confine`（`flow.yaml` 顶层，可选）。** 写 `confine: {scope_file: <包的状态目录>/scope.json}`：范围文件合入主线、`status` 是 `active` 时，基线提交（`baseline.commit`）里有、`editable` 里没有的本体条目助手不能写也不能删，没有命令在跑时也一样；新增条目不限，网页上人工编辑不限。只认主线上的范围文件，副本里改它扩大不了范围；范围文件只能在它所属节点的命令运行时写。停用包就解除。一次装配只能有一个包声明，范围文件要落在本包某个节点的 `produces` 里。领域包扩展（`domain-extend`）是现成的例子。

**触发 `activation`（`flow.yaml` 顶层，可选）。** 节点不该一直在流程里、要等某件事发生才出现时用：

```yaml
activation:
  event: materials_added         # 内核给的事件，现在只有这一个：用户在平台上传了资料
  requires: [my_has_entries]     # 事件发生时要满足的探针，只能写本包声明的
  session: my-pack/scope.json    # 这一轮的状态文件，要在本包节点的 produces 里
confine: {scope_file: my-pack/scope.json, lock_on_activation: true}  # 可选：触发后、范围确认前已有条目全部只读
nodes:
  - id: my-pack.scope
    visible_if: activated        # 只在触发后、这一轮开着时出现
    preempt: 上传了新资料，先圈定范围   # 可选：下一步先推它
```

状态文件是 JSON：`status` 是 `active` 算这一轮开着；`covers`（确认时的主线提交）包含触发时的提交算这次触发处理过；`baseline.commit` 之后发布过算这一轮结束。
探针写 `{kernel: session_open, session: <路径>}` 判断这一轮开没开着，节点完成判定带上它，上一轮留下的文件才不会让新一轮的节点直接算完成。启用插件之前上传的资料不触发。

**`autobuild: false`** 让自主构建跳过这个节点，用于要用户先给输入才能做的节点。

**动作的条件 `when`**（`actions` 里每一项可选）：探针名列表，`!名字` 取反，都成立时这个动作才出现在建议条和面板上，如 `{label: 追加分析, command: …, when: [research_report]}`。用到的探针要在本包或 core 的 `probes` 里声明。

**`section: required` 不挡发布。** 上面的 `sample-review.check` 挂在 `core.validate` 之后，和场景模拟并列；没做完时发布照样可以进行。要挡发布必须写 `blocks`。

锚点用主线节点 `core.research`、`core.object`、`core.operation`、`core.validate`、`core.publish`，或别的包的节点 id。`core.model`、`core.consistency` 这类旧名字不写进新包。

| `placement` | 用途 | 常配 |
|---|---|---|
| `step` | 流程里的一步 | `section: required`、`after` |
| `branch` | 旁支，可以不做 | `section: optional`、`optional: true`、`after` |
| `gate` | 关卡 | `section: pre_publish`、`after`、`blocks` |
| `on_demand` | 随时可跑 | `section: on_demand`、`progress: false`，不写 `after` |

这些情况上传和安装都能过，启用时才返回 422 `flow_assembly_failed`：

- 锚点不存在，或节点之间成环。
- 两个节点的 `produces` 重叠。通配互相匹配也算重叠，所以样例用精确文件名。core 占了 `reports/*-rule.md` 和 `reports/*-semantic.md`。
- 探针名、`commands` 的键和已有的重复。
- 不止一个包声明了 `confine`，或范围文件不在声明它的包的 `produces` 里。

## 13. 启用后核对

| 看什么 | 位置 | 期望 |
|---|---|---|
| 命令、子代理、脚本、模板 | `.claude/commands/`、`agents/`、`scripts/`、`templates/` | 文件都在 |
| 节点 | `.claude/flow.yaml` 的 `nodes` | 有包的节点，`pack` 是包名 |
| 节点顺序 | 流程面板 | `sample-review.check` 在 `core.validate` 之后 |
| 完成判定 | 写好 `reports/sample-review-check.md` | `sample-review.check` 变成已确认，`sample-review.stats` 变成可开始 |
| 提示规则 | 发一条含「会议纪要」的消息 | 助手收到提示文本 |

## 14. 常见失败与修法

| 现象 | 检查项 | 原因 | 修法 |
|---|---|---|---|
| 命令要带包名前缀 | `names` | 租户包命令没写 `<包名>-` | 改文件名和清单 |
| 名字与 core 重名 | `names` | 命令、子代理、skill、脚本与 core 撞名 | 全部带包名前缀 |
| 包名不可用或版本已入库 | `names`、`private_name` | 保留名、名字被占用，或改了内容没升版本 | 换名字，或升 `version` |
| 缺少文件，或未声明（warn） | `declared` | 清单和实际文件对不上 | 对齐清单和文件 |
| 校验和不一致 | `checksums` | 改完没重新生成 | 重跑 `pack_tool.py build` |
| 前端文件不允许 | `frontend` | `ui/` 外有 `.js`、`.css`、`package.json` 等 | 删掉；页面选内置页 |
| 带了 `settings.json` | `hooks` | 想改工作区设置 | 删掉；需要拦截时用清单 `hooks` |
| 疑似凭据 | `secrets` | 正文或脚本里有密钥字面量 | 删掉，改成从环境变量读 |
| 语法错误 | `syntax` | 脚本编译不过 | 本地 `python3 -m py_compile` 修好 |
| 产物路径不合法 | `entity_types` | 不在 `ext/<包名>/` 下，或路径里有两个 `*` | 按第 8 节改 |
| 节点包内容超范围 | `node_pack` | 带了 `skills/`、`ui/`、页面、产物登记 | 拆成能力包 |
| 挂法或锚点不对 | `node_pack` | 写了 `spine`，或非 `on_demand` 节点没写 `after`、`before` | 改成 `step`，补锚点 |
| 探针未声明 | `node_pack` | `done` 里的名字拼错或没写 `probes` | 补 `probes` |
| `requires_flow` 不满足 | `node_pack` | 版本要求超过仓库里的 core | 放宽范围 |
| 写入范围不对 | `write_scope` | `produces`、`writes` 碰受保护目录，或 `script.output`、`decision.record` 没写进 `produces` | 改到 `reports/` 或 `ext/<包名>/`，补进 `produces` |
| 范围限定写错 | `confine` | 写了 `scope_file` 以外的键、路径带通配或在 `ontology/`、受保护目录下、不在本包节点的 `produces` 里 | 改成本包状态目录下的具体文件，并写进范围节点的 `produces` |
| 触发写错 | `activation` | 事件不认识、`requires` 不是本包声明的探针、`session` 不在本包节点的 `produces` 里、写了 `visible_if: activated` 没声明 `activation`、`preempt` 用在别的节点上 |
| 入口命令不属于本包 | `node_pack`、`node_entry` | `entry.command` 或钩子命令写了别的包 | 改成本包命令 |
| 启用返回 409，提示路径同时来自两个包 | `pack_incompatible` | 两个包落到同一路径 | 文件名带包名前缀 |
| 启用返回 422，提示同时声明输出 | `flow_assembly_failed` | `produces` 通配太宽 | 改成精确路径 |
| 启用返回 422，提示锚点不存在 | `flow_assembly_failed` | `after` 写错，或上游包没启用 | 改锚点，或写成列表兜底 |
| 启用后命令没有 | 无 | 当前对话还没到下一条消息 | 再发一条消息 |
| 节点一直未完成 | 无 | 探针 `glob` 和实际文件名对不上 | 对齐 `glob` 和命令里写的路径 |
| 命令跑了节点不显示进行中 | 无 | `flow.yaml` 的 `commands` 没写或键少了前缀 | 写命令全名 |
| 脚本节点报 503 | 无 | 沙箱不可用 | 联系运维恢复沙箱，本机不会代跑 |
| 脚本节点失败 | 无 | 输出超 1500 字符或超时 | 只输出摘要，减少输入 |
