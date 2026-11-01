# 0042 从 core 拆出可选包；锁文件升到 2；插件运行时接到治理平面的四个登记点

## Context

方案 D7 后半、详设 5.3 的划分表与第 14 章第 3、15、16、17 项。P1（0024）把整个工具箱当成一个 core 包，
所有项目都带着场景走查、代码生成、图示、反馈演进、联网调研这些可选能力；P2 要按项目启用、停用，停用后
已有产物保留。拆包前要先解开三处耦合（详设 5.3）：命令里写死的 `.claude/...` 路径、脚本之间按模块名 import、
后端直接 import 工具箱脚本（已在 B3 收成共享契约库）。

另有 B3 合并时留下的四个登记点（`_plan/gaps.md`「B3 合并」）：就绪检查的 `assembly` 项、O-29 熔断的包查询、
O-42 版本清单里的包与锁文件格式、提交 trailer 的 `Onto-Packs`。

## Decision

1. **划分**（镜像里放在 `assets/packs/<名>/`，启动时和 core 一样入库，版本号是清单版本加内容摘要）：

   | 包 | 内容 | 新建项目缺省 |
   |---|---|---|
   | core 0.2.0 | 主入口、资料分析、抽取、校验、查询、增量更新（按需节点）、业务知识的存放与加载（`knowledge_helper`）、共享契约库、`script_verify`、`report_reconcile` | 必装 |
   | trace 场景走查 | trace 4 个命令、`/ontology-mock`、两个子代理、`trace_inputs`、`trace_index`、场景与示例数据产物 | 开 |
   | knowledge 知识沉淀 | `/knowledge-record`、`/knowledge-review`、`/knowledge-nominate`，knowledge-conflict-agent，沉淀守则，纠错信号提示规则，知识沉淀页 | 开 |
   | codegen 代码生成与求解 | codegen-scan、codegen、solver-evaluator，两个子代理，评估器模板，扫描报告与求解契约产物 | 开 |
   | diagram 图示 | flowchart、validate-diagrams，mermaid-flowchart，drawio 摘要脚本，流程图产物 | 开 |
   | evo 反馈演进 | `/ontology-evolution`、`check_eval_generalization.py`（B1 留下的） | 关 |
   | websearch 联网调研 | deep-research 技能 | 关 |
   | migrate 迁移复用 | extract-migrated-objects 技能 | 关 |

   `script_verify`、`report_reconcile`、`jiuwenbox_client` 按方案第 3 章的勘误留在 core（预提交守卫与发布检查要用）；
   没人引用的 `generate_overview.py`、`split_drawio.py`、`summary_to_drawio.py` 删掉。系统逆向建模包在 P3。
2. **官方包保留现有命令名**（方案第 6 章第 7 项）。前端插件页定的命令名与后端不同的，以后端为准，映射见
   `_plan/contract-changes.md` B4c；页面要而后端没有的（`/knowledge-review`）在对应包里补。
3. **流程节点跟包走**：可选包用自己的 `flow.yaml` 往主流程上挂节点（详设 6.8 的编号：`trace.trace`、`trace.mock`、
   `codegen.codegen`、`codegen.solver`、`diagram.flowchart`、`evo.evolution`、`migrate.reuse`）。`after` 可以写成列表，
   取第一个存在的锚点（代码生成挂在场景验证之后，没启用场景走查就挂在质量检查之后）；`display_after` 只管显示顺序；
   包可以补命令前置与探针，重名就拒绝；`/ontology` 的下一步建议里指向没启用的包的命令在组装时去掉。
   工作区规则加一节「可选能力」：命令文档提到可选命令时先看 `.claude/commands/<名>.md` 在不在。
4. **知识沉淀只拆沉淀这一段**（第 14 章第 15 项）：存放和加载留在 core，停用后已有条目照常加载、不再新增。
   纠错信号改成知识沉淀包声明的提示规则（0040）；原来的 `knowledge_signal_probe` hook 在装配目录里没有沉淀守则时
   不出声，会话宿主接上提示规则注入后可以去掉它。
5. **锁文件版本 2**：`lock_version` 写进锁文件。存量项目的锁（版本 1）在 core 升到拆分后的版本（≥0.2.0）时，
   一次性补上全部拆出去的包（都启用，能力不变），然后升到 2；之后用户停用的包不会被补回来。新建项目按
   P-02 的 `capabilities`（没写的取清单 `default_on`）与 `industry_packs`、`system_packs` 写锁文件。
6. **四个登记点**（`services/packs/governance.py`，`api/v1.install` 时登记）：
   - **就绪检查 `assembly`**（关键项）：fail = 包仓库里没有 stable core，或 stable core 的内容和校验和对不上，
     或试装配 stable core 出错（新项目建不起来）；warn = 上次启动核对有项目装配失败（这些项目保持原版本还能用）；
     其余 ok；镜像里没有工具箱（测试里的最小部署）也算 ok。不逐项目装配，就绪检查要快。
   - **O-29 包查询**：包与版本在包仓库里就返回 O-25 的一项（通道、灰度比例、熔断状态、使用的项目数；
     `error_rate_pct` 在 O-03 有数据前为 null），否则 404。
   - **O-42 版本清单**：各包 stable 版本，加一行 `packs.lock` 格式版本。
   - **`Onto-Packs` trailer**：本项目生效中的装配里的包与版本（`core@0.2.0+…, trace@0.9.1+…`，按名字排，最多 20 个），
     没有装配目录时不带；所有经写入事务的提交都带。

## Alternatives considered

- **整个知识能力拆出去**：没启用的项目里 core 的 15 处知识加载与行业包分层都会落空（详设 5.3 的理由）。
- **存量项目按新建口径只补 default_on 的包**：会让升级后的老项目突然少了反馈演进、联网调研等能力；
  升级不能改变已有项目的行为，所以全补、由用户自己停。
- **拆包时改命令名（带包前缀）**：所有已有对话记录、文档、前端都要跟着改，收益只有命名整齐。

## Consequences

- 节点编号从 `core.trace`、`core.codegen` 等改成包前缀，FL-01 的 `nodes[].id`、`commands` 跟着变（契约补充）。
- 只启用 core 的项目里，`/ontology` 不会推荐没启用的命令；用户点到时 S-20 返回 `pack_not_enabled`。
- 测试里引用 `workspace-toolkit/scripts/...` 的路径改到各包目录；Onto-Packs 进 trailer 后，按整份 trailer 断言的用例
  一并改了。
