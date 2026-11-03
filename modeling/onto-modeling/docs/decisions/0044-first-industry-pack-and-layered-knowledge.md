# 0044 第一个行业包（零售连锁，合成内容）与分层的业务知识

## Context

详设 5.5、第 13 章 P2 验收「第一个行业包」，契约 P-02（新建项目选行业）、PackCard `default_on`（F1 补充）、
K-01～K-08。工具箱已有项目级的业务知识（`ontology/knowledge/*.md`，`knowledge_helper load` 在命令里按范围加载），
行业包要把它往上提一层：跨项目共享的知识、概念别名、参考对象、声明式校验规则、示例项目。行业包只有数据，
不带代码（H0，马上生效）。内容必须是合成的，不能出现任何真实客户场景。

## Decision

1. **内容**：`assets/packs/industry-retail/`，行业取前端 mock 用的「零售连锁」，主线示例是「门店补货单编制」。
   按 5.5 的结构：`knowledge/`（与项目知识同一格式，15 条）、`concepts.yaml`（21 个概念与别名）、`reference/`
   （挂在对象抽取、操作抽取、规则指标三个决策点上的写法提示）、`library/`（6 个参考对象、3 个参考操作，只作候选
   提示，不自动复制）、`standards.yaml`、`rules.yaml`（7 条声明式规则）、`examples/retail-restock-mini/`。
   全部是通用说法和整数示例值，不出现企业、品牌、系统产品、标准编号、地名。
2. **装配**：`kind: knowledge` 的包在装配目录里各占 `industry/<包名>/`，不和能力包的文件混在一起；YAML 另存一份
   JSON（`pack.json`、`concepts.json`、`checks.json`、`standards.json`），工作区脚本只用标准库就能读。
3. **分层加载**（`knowledge_helper load`）：优先级从低到高是行业知识、项目知识，slug 相同时项目知识覆盖行业条目。
   行业条目的范围写 `concept:<概念名>`，加载时用请求范围里的条目名去匹配概念名与别名（等于或包含）；全局条目全部加载，
   按概念命中的最多取 budget 条。加载出来的条目注明「来源：零售连锁（行业包）」。
4. **校验**（`ontology_checker` + `industry_rules.py`）：额外加载已启用行业包的 `checks.json`。规则只有四类——
   `require_relations_to`、`require_fields`、`require_sections`、`prefer_terms`——按 `match.concept` 与条目名匹配，
   **只给警告或建议**，不阻断提交：行业规则是参考口径，项目有权不同。
5. **接口**（`services/knowledge_layers.py`、`routers/knowledge.py`）：
   - K-01 多一个只读的行业层（`label` 是行业包显示名），`overridden` 是覆盖了行业条目的项目条目数；
   - K-02 的 `overrides` 指向被覆盖的行业条目；行业条目改、删返回 `403 artifact_readonly`（`reason=industry`），
     要改口径就在项目里写同名条目；删掉项目条目后 `restored_industry_entry` 指回行业条目；
   - K-06 只做确定性检查（同名重复、覆盖了口径不同的行业条目、适用范围引用了不存在的条目），结果存工作区
     `.onto/run/`；需要语义判断的冲突由 `/knowledge-review` 在对话里复核；
   - K-07、K-08 要启用知识沉淀包和对应行业包：自动脱敏（手机号、邮箱、证件号、网址、单位名称、内部编号、金额，
     只判有无、给替换后的文字，不回显命中原文），提交后存数据目录 `knowledge/nominations/`，状态 `pending_review`，
     等管理员审核（AD-09、AD-10）。
6. **新建项目选择**（P-02）：`industry_packs`、`system_packs` 按名字启用，不存在或类别不对返回 `404 pack_not_found`；
   `capabilities` 没写到的能力包取清单 `default_on`。行业包不进「缺省启用」：只在向导里选了才写进锁文件。

## Alternatives considered

- **行业知识复制进项目的 `ontology/knowledge/`**：项目里就分不清哪些是自己的口径，行业包升级也没法更新；
  只读分层加载、同 slug 覆盖，升级时项目覆盖的条目保持不变。
- **行业规则判错误**：行业通用口径和具体项目常有出入，判错误会挡住合理的建模；先给警告，项目确认后可以写
  知识条目说明为什么不同。
- **概念匹配用模型做语义判断**：加载在每条命令里都跑，要快且确定；名称与别名匹配够用，漏掉的由全局条目兜底。

## Consequences

- 详设 5.5 的「`/ontology 示例` 列出行业示例项目」「在产物或提交里记录用了哪条行业知识」「管理员审核提名」
  还没做（P4 系统知识包与版本归因一起），见 `_plan/gaps.md` B4c。
- 行业包是纯数据包，启用、停用马上生效（不等下一条消息）；装配目录哈希随之变化。
- 提名记录在数据目录里，不进项目 git；审核接口落地前只能落盘不能处理。

## 审查修复（2026-11-02）

见 ADR-0045「审查修复」：K-08 服务端重新做脱敏检查，还有命中项回 `422 nomination_not_sanitized`；新建项目条目与
行业条目同名时沿用行业条目的 slug，才能覆盖；清单的 `knowledge_dir` 等路径字段不能出包。
