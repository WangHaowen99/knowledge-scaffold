# BFO 建模规范（参考）

> 本文档是 **可选参考**，不是强制规则。建模人员在遇到结构性疑问（"这是状态还是过程？""这是属性还是角色？"）时可按需查阅。
> 项目自带的规则检查器与语义检查器不会强制按 BFO 校验；如需启用 BFO 风格的机检规则，参见末尾「与本项目工具集的衔接」。

---

## 0. 快速索引

| 我想…… | 跳到 |
|---|---|
| 知道 BFO 大致是什么 | §1 |
| 判断一个东西归入哪一类 | §2 决策树 |
| 查所有 BFO 类目的定义 | §3 类目录 |
| 查 BFO 常用关系 | §4 关系目录 |
| 看制造业建模该遵守哪些原则 | §5 |
| 把 BFO 范畴映射到本项目的 object / operation / action | §6 |
| 看常见错误怎么避免 | §7 反模式 |
| 建模收尾前自检 | §8 检查清单 |
| 让校验脚本支持 BFO 规则 | §9 |
| 查命令何时主动提示参考 BFO | §10 |

---

## 1. BFO 是什么

**BFO（Basic Formal Ontology）** 是一个上层（top-level）本体，由 Barry Smith 等人提出，现行版本为 **BFO 2020 / ISO/IEC 21838-2**。它不描述任何具体行业，而是为各领域本体提供一套统一的**最顶层范畴**与关系，使不同本体之间可以对齐。

制造业的事实标准衍生体系：
- **IOF（Industrial Ontology Foundry）** —— 基于 BFO + CCO 的工业领域本体集合。
- 子领域：IOF Core / Supply Chain / Maintenance / Product Lifecycle / Process Planning。

**BFO 的核心承诺**：
1. 世界里的实体只有两大类——**Continuant（持续者）** 与 **Occurrent（发生者）**。
2. 一切"属性、能力、信息"都依附于某个具体的实体，不能孤立存在。
3. 时间是建模的一等公民——"现在"是建模的默认问号。

---

## 2. 顶层判断决策树

遇到一个待建模的东西，依次问以下问题：

```
1) 它在时间里"展开"（有起止、有阶段）吗？
   ├─ 是 → Occurrent
   │   ├─ 持续一段时间 → Process（下单流程、加工过程）
   │   ├─ 瞬时 → Process Boundary（订单创建瞬间）
   │   └─ 是时间段本身 → Temporal Region
   └─ 否（在任何时刻都"整体存在"） → Continuant，进入 2

2) 它是物理实体本身，还是依附于实体的特征 / 信息？
   ├─ 是物理实体或场所 → Independent Continuant（IC）
   │   ├─ 有质量、占据空间 → Material Entity → Object / Object Aggregate
   │   └─ 不占据空间（场所、边界） → Immaterial Entity → Site / Spatial Region
   ├─ 是依附于一个特定载体的特征 → Specifically Dependent Continuant（SDC）
   │   ├─ 现实可观测的属性（颜色、重量、状态值） → Quality
   │   └─ 潜在的、要靠过程"实现"出来的
   │       ├─ 内在倾向（易碎、可加工） → Disposition
   │       ├─ 设计/选择赋予的内在功能（泵血、切削） → Function
   │       └─ 外部赋予、可终止（客户、操作员） → Role
   └─ 是可在多个载体间复制的信息 → Generically Dependent Continuant（GDC）→ Information Content Entity (ICE)
```

记忆口诀：**"先时间、再实体 vs 依附、再依附三选一"**。

---

## 3. 类目录（BFO 2020）

### 3.1 Continuant 分支

| 类 | 简称 | 定义要点 | 制造业例子 |
|---|---|---|---|
| **Independent Continuant** | IC | 不依附于其他实体而存在 | 机床、订单、原料 |
| ├ Material Entity | ME | 有质量、占空间的实体 | 设备本体、产品、毛坯 |
| │ ├ Object | | 边界自然、稳定独立的物体 | CNC-001、单件成品 |
| │ ├ Object Aggregate | | 由多个 Object 构成的集合 | 一批工件、一个产线设备群 |
| │ └ Fiat Object Part | | 由约定切分出来的"部分" | 工件的"上半部分" |
| └ Immaterial Entity | IE | 无质量、不占据物质空间 | 车间空间、工作位、轮廓边界 |
| **Specifically Dependent Continuant** | SDC | 依附于唯一一个载体 | |
| ├ Quality | | 实际显现的属性 | 直径=50mm、状态=running |
| └ Realizable Entity | RE | 潜在的，要由 Process 实现 | |
| &nbsp;&nbsp;├ Role | | 外部环境/约定赋予，可终止 | "供应商角色""审批人角色" |
| &nbsp;&nbsp;├ Disposition | | 实体内在的倾向，遇条件触发 | "易开裂""可被加工" |
| &nbsp;&nbsp;└ Function | | 设计/选择赋予的 Disposition | "心脏的泵血""车床的车削" |
| **Generically Dependent Continuant** | GDC | 可被多个载体并行承载 | |
| └ Information Content Entity | ICE | 关于某物的、可复制的信息 | 工单、BOM、CAD、合同 |

### 3.2 Occurrent 分支

| 类 | 简称 | 定义要点 | 制造业例子 |
|---|---|---|---|
| **Process** | P | 持续一段时间的发生 | 一次加工、一次审批、一次运输 |
| **Process Boundary** | PB | 瞬时事件、Process 的开始/结束 | "订单创建瞬间""停机瞬间" |
| **Temporal Region** | TR | 纯时间维度的区域 | 一天、2026-05 |
| ├ Zero-Dimensional TR | | 时刻 | 14:23:05 |
| └ One-Dimensional TR | | 时间段 | 班次 08:00–16:00 |
| **Spatiotemporal Region** | STR | 时空区域 | 某物在 t 时刻所处的时空轨迹 |
| **History** | | 某 Material Entity 一生的全部 Process | 一台设备的全生命历史 |

---

## 4. 关系目录（含 RO 常用项）

> RO（Relation Ontology）补足 BFO 的核心关系，IOF 大量沿用。

### 4.1 部分论
| 关系 | 适用 | 含义 |
|---|---|---|
| `part_of` / `has_part` | Continuant↔Continuant 或 Occurrent↔Occurrent | 部分关系（同范畴内） |
| `has_continuant_part_at t` | IC↔IC | 时间索引化的部分关系 |
| `has_occurrent_part` | Process↔Process | 子过程 |

### 4.2 依附 / 承载
| 关系 | 适用 | 含义 |
|---|---|---|
| `inheres_in` / `bearer_of` | SDC↔IC | "重量依附于这块钢" |
| `concretizes` / `is_concretized_in` | GDC↔SDC/IC | "这份订单 ICE 具现化为数据库行" |

### 4.3 实现 / 参与
| 关系 | 适用 | 含义 |
|---|---|---|
| `realizes` / `realized_in` | Process↔Realizable Entity | "这次车削 realize 了车床的车削 function" |
| `participates_in` / `has_participant at t` | IC↔Process | 物料/设备/人参与过程 |

### 4.4 时间 / 空间
| 关系 | 适用 | 含义 |
|---|---|---|
| `exists_at` | 实体↔TR | 实体存在于某时刻/时段 |
| `occurs_in` | Process↔TR/STR | 过程发生于某时空 |
| `precedes` / `preceded_by` | Process↔Process | 先后顺序 |
| `located_in at t` | IC↔Site | 时间索引化的位置 |
| `occupies_spatial_region` | IC↔SR | 空间占据 |

### 4.5 信息 / 指向
| 关系 | 适用 | 含义 |
|---|---|---|
| `is_about` | ICE↔任何实体 | "这条记录关于订单 X" |
| `prescribes` / `is_prescribed_by` | Plan(ICE)↔Process | "工艺计划规定了实际加工过程" |
| `denotes` | ICE↔实体 | "序列号指代某具体工件" |

---

## 5. 面向制造业的 10 条核心建模原则

### 5.1 设计/规范 与 实例 严格分层
- BOM、CAD、工艺路线、工单 → **ICE**（GDC）
- 实际产品、实际加工 → **Material Entity / Process**
- 通过 `prescribes` / `is_about` 关联。
- **反模式**：把"图纸"和"产品"建成同一对象，导致版本与追溯混乱。

### 5.2 设备 ≠ 能力 ≠ 功能 ≠ 角色
| 维度 | BFO 范畴 |
|---|---|
| 机床本身 | Material Entity / Object |
| "能加工铝合金"的内在能力 | Disposition |
| "设计用于车削"的设计用途 | Function |
| "本班次承担粗加工"的临时身份 | Role |
| 实际一次加工 | Process（realizes Function/Disposition/Role） |

### 5.3 状态变化拆三件套
状态不是 enum 字段，而是：
- **状态值**（idle / running / down）→ Quality
- **状态转移**（start / stop）→ Process
- **转移瞬间** → Process Boundary

并且必须 **time-indexed**：`has_quality_at`，而不是"对象当前 status = running"。

### 5.4 批 / 件 / 序列号三层粒度
- 批次 → Object Aggregate
- 单件 → Material Entity / Object
- 批号 / 序列号 → ICE（denotes 某 Object 或 Aggregate）
- 追溯 = 同一 Object 在多个 Process 中以 `participates_in` 出现，**不要在 ID 字段里拼字符串**。

### 5.5 工艺路线 = Process 的部分论
- 工序 → Process
- 工艺路线 → Process 的 `has_occurrent_part` 序列
- 工序先后 → `precedes`
- 工艺**计划**（Plan/ICE） ↔ 实际**执行**（Process）通过 `prescribes` 关联。

### 5.6 检测、缺陷、故障回到 Disposition 框架
- 质量参数 → Quality
- 检测过程 → Process，realize 设备的 measuring function
- 缺陷 → 越界 Quality 值 或 Disposition（如"易开裂"）
- 故障 → realize 设备的 failure disposition
- 维护 → Process，结果是改变 Quality 或消除 Disposition

### 5.7 物料流 = 参与关系，而非属性赋值
- 同一块钢在毛坯 / 半成品 / 成品阶段 → 同一 Material Entity 在不同 Process 中 `participates_in`，其 Quality 随时间变化。
- 涉及消失/诞生的（化学反应、装配/拆解）→ 用 Process 的 input/output（RO/CCO），不要让"原料对象"突然变成"产品对象"。

### 5.8 物理位置 vs 业务归属
- 车间 / 仓位 / 工位 → Site（Immaterial Entity）
- 物料位置 → `located_in Site`（time-indexed）
- "归属于某产线"这种**管理归属** → Role，而非物理位置
- 库存数量 → Object Aggregate 的成员数，不是某对象的属性

### 5.9 信息系统对象走 ICE 中间层
MES / ERP / WMS 里能看见的工单、报工、检验单、入库单 → 全是 ICE：
- `is_about` 现实中的 Process / Material Entity / Quality
- `is_concretized_in` 数据库行 / 打印件 / 屏幕显示
- 同一份工单在 ERP、打印、扫描件中是**同一个 ICE 的不同 concretization**

→ 解释"系统数据与现场不一致"的本体论根源就在这里。

### 5.10 时间索引 + 历史是默认能力
- 任何"现在……"建模时都默认 **time-indexed**。
- 物理对象的全部生命过程 = 它的 **History**。
- 数字孪生 = 一个持续更新的 ICE，`is_about` 物理设备的 History。
- 追溯不是后加功能，是建模结构本身的产物。

---

## 6. 与本项目模板的对应关系

> 本项目的 `ontology/` 目录已经预设了 object / operation / action 三类文档，本节给出与 BFO 范畴的对照，方便在不改模板的前提下"按 BFO 思考"。

### 6.1 BFO 范畴 → 本项目文档类型

| BFO 范畴 | 本项目落地位置 | 备注 |
|---|---|---|
| Material Entity / Object | `ontology/object/` | 主对象 |
| Object Aggregate | `ontology/object/` | 命名建议带"批次/集合/列表" |
| Site / Spatial Region | `ontology/object/` | 仓位、车间作为对象 |
| Quality | object 的**属性**（属性清单一行） | 不需单独建对象 |
| Role | 一般做成 object，命名带"角色"；或作为属性 | 若有自己的生命周期、独立属性，独立建对象更清晰 |
| Disposition / Function | object 的**属性** 或 `ontology/knowledge/` 业务规则 | 通常不单独建对象 |
| ICE（工单 / BOM / 检验单 / 合同） | `ontology/object/` | 命名建议保留"单 / 表 / 记录"后缀以示信息体 |
| Process（业务流程 / 子流程） | `ontology/operation/` | 主体 |
| Process Boundary（瞬时事件） | `ontology/operation/` 的判断节点或外部副作用说明 | |
| Plan（工艺计划、规则规范） | `ontology/knowledge/` 或独立 object | 与"实际执行的 operation"分开 |
| 一个外部触发动作 | `ontology/operation/` 中的独立触发逻辑或外部副作用步骤 | |

### 6.2 模板字段的 BFO 解读

**`object-template.md`**
- "对象描述" → 本体类目（IC / ICE / 视情况）
- "对象属性清单" → 该 IC 的 Quality 集合（可能也包括与之关联的 ICE 字段）
- "关联对象" → BFO 关系的简化表述；如需严格 BFO，可在关系名里使用 `part_of / inheres_in / participates_in` 等

**`operation-template.md`**
- 整个文档对应一个 **Process**（或一个 Process 类）
- "输入 / 输出" 是 Process 的 input/output（CCO 风格）
- "支撑数据的对象" 通常是 `has_participant` 的实体
- "逻辑描述" 中的子流程调用 = `has_occurrent_part` 或 `precedes`

### 6.3 命名建议（避免范畴混淆）

| 想表达 | 命名提示 |
|---|---|
| Object Aggregate | 加"批次 / 集合 / 列表 / 组" |
| ICE（信息体） | 保留"单 / 表 / 记录 / 凭证 / 报告" |
| Role | 加"角色 / 身份" |
| Site | 加"车间 / 仓位 / 工位 / 区域" |
| Process | operation 文件名用动词或动名词："下单""审核订单""车削加工" |
| Plan / 规则 | 进 `ontology/knowledge/`，不要混进 operation |

---

## 7. 常见反模式

| 反模式 | 症状 | 正确做法 |
|---|---|---|
| **Continuant/Occurrent 混淆** | 把"加工"当对象、属性叫"是否完成" | 加工是 Process（operation），完成是 Process 边界 |
| **设计/实例混淆** | 工单和产品共用一个对象 | 工单 = ICE，产品 = Material Entity，两个对象 + `is_about` |
| **状态机塞进字段** | 对象有 `status: enum`，没有过程文档 | 拆 Quality + 状态转移 Process + 边界 |
| **属性当对象** | "颜色"独立建对象 | Quality 留在 object 属性里 |
| **对象当属性** | "供应商"只写成订单的字符串字段 | 供应商应是独立 object（含 Role 语义） |
| **Role 当作 Person 本体** | "审批人"建成对象，与"用户"语义重复 | 审批人是用户在某流程中的 Role，可只在 operation 里以 Role 出现 |
| **缺时间索引** | 位置/状态/归属直接写"当前 X = Y" | 加 `at_time` / 配合 history 类对象 |
| **追溯靠拼 ID** | 在 ID 字段里拼"批号-序号-工位" | 用 `participates_in` 串联 Process，ID 仅为 ICE 中的 denotation |
| **信息层与现实层未分** | "订单"既指数据库行也指现实交易 | 现实交易 = Process；数据库行 = ICE 的 concretization |
| **Function vs Disposition 混淆** | 把人为设计的功能写成内在倾向 | 设计赋予的 = Function；天然属性 = Disposition |

---

## 8. 建模收尾自检清单

逐条问自己，不通过则回到对应原则：

- [ ] 每个对象都能明确归入 IC / SDC / GDC 之一？
- [ ] 每个 operation 都对应一个 Process（有起止、有参与者）？
- [ ] "设计/规范"类（BOM、工艺、合同）有没有作为 ICE 与"实际执行"分开？
- [ ] 出现"状态"的地方，是否拆成了 Quality + Process + Boundary？
- [ ] 出现"批/件/号"的地方，是否分清了 Aggregate / Object / ICE 三层？
- [ ] 工艺/流程链条是否以 Process 的 `has_part` / `precedes` 表达，而不是塞进一个长文本？
- [ ] 设备的"能做什么"是否区分了 Function / Disposition / Role？
- [ ] "位置 / 状态 / 归属" 是否有时间索引（或在 operation 中说明随时间变化）？
- [ ] 信息系统对象（工单等）有没有标注"它关于什么"（`is_about`）？
- [ ] 追溯需求是否通过参与关系自然成立，不依赖 ID 字符串拼接？

---

## 9. 与工具集校验层的衔接

> 当前 `ontology_checker.py` 与 `semantic-check-agent.md` **不强制 BFO**。本节给出可引入 BFO 风格检查时的切入点，并把两条已规范成型的可机检规则——**属性时序分离**、**状态转移闭环**——以完整规范的形式写在 §9.4 与 §9.5。

### 9.1 适合作为规则检查的 BFO 项
- **属性时序分离**：见 §9.4 完整规范。
- **状态转移闭环**：见 §9.5 完整规范。
- **设计/实例分层**：对名称含"单 / 表 / 记录 / 凭证 / 报告"的对象，提示其应有 `is_about` 类语义说明，并应与对应"实际过程 / 实体"对象分开。
- **追溯结构**：检查跨 operation 是否通过对象参与（输入 / 输出 / 支撑）建立链条，而非只在字段里携带上游 ID 字符串。

### 9.2 适合作为语义检查的 BFO 项
- Continuant / Occurrent 混淆（如 operation 文档读起来像对象描述）。
- Function vs Disposition vs Role 命名误用。
- 对象属性是否其实应建模为独立对象（如"供应商"只是字符串字段）。

### 9.3 不建议机检的项
- BFO 类目的精确归类——业务专家直觉常常优于机械判断。
- 是否启用 BFO 本身——保持为本文档式的"参考选项"。

### 9.4 属性时序分离

**适用范畴**：所有 `ontology/object/*.md` 的对象属性清单。

**判定原则** —— 区分**静态属性**（归属于存在体 / Continuant）与**动态属性**（归属于时序体 / 应通过 time-indexed Quality 表达）：

| 维度 | 静态属性 | 动态属性 |
|---|---|---|
| 是否随时间变化 | 不变 / 极少变 | 随时间持续变 |
| 更新频率 | 创建时设定，几乎不再更新 | 周期性 / 事件驱动更新 |
| 历史需求 | 不需要保留 | 需要保留变化记录 |
| 时间戳依赖 | 不依赖时间戳即有意义 | 必须有时间戳才有意义 |
| 典型例子 | 编号、型号、最大速度、额定载重 | 当前位置、运行速度、温度、当前状态 |

**核心规则**：
1. 一个 Object（BFO 中的 Independent Continuant）**不应携带动态属性**。
2. 若发现 Object 含动态属性，应拆出独立**时序对象**：命名建议 `{实体名}状态` / `{实体名}快照` / `{实体名}监测记录`，把动态属性迁移过去，并显式说明该对象需带时间戳维度。
3. **逻辑（Process）本身不需要再为之另建"状态对象"**——Process 在 BFO 中即时序展开物。

**校验步骤**：
1. 对每个 `ontology/object/*.md`，逐条扫描"对象属性清单"。
2. 按上表判断每条属性是静态还是动态。
3. 若 Object 上发现动态属性 → 报"属性时序未分离"，并给出拆分建议：`保留属性 / 迁出属性 / 建议的新时序对象名`。
4. 反向检查：是否有 operation 文档已被错误地伴随建出"XX 状态"对象——若有，标"可能多余"。

**防错**：
- 不要遗漏**隐式**动态属性：命名里没有"当前 / 实时"字样但语义上属于动态的（如"位置""温度""状态"），需按业务语义判断。
- "最大速度"（设计参数）是静态，"当前速度"（实时观测）是动态——同源属性的不同形态需分离。
- 此规则为**硬规则**：Object + 动态属性 = 必须拆分，无例外。

**BFO 解释**：动态属性本质是 **time-indexed Quality**。如果直接挂在 Continuant 上而不带时间索引，本体上无法回答"该值在哪个时刻成立"。拆出独立时序对象，是把时间索引固化到结构里的最直接做法。

### 9.5 状态转移闭环

**适用范畴**：`ontology/object/`（提供合法状态集）+ `ontology/operation/`（提供转移定义）。

**前提**：仅当对象含状态枚举型属性时执行。识别信号：
- 属性名含"状态 / 阶段 / 步骤"；或
- 枚举值数量 ≥ 3 且语义上表示流程阶段。

**核心规则**：每个状态枚举必须形成"可达 + 可出"的闭环；任何 operation 中提到的状态转移都必须能在转移矩阵中找到位置。

**校验步骤**：

1. **提取合法状态集**：从对象属性枚举得到 `States = {S1, S2, ...}`。
2. **构建转移矩阵**：扫描所有 `ontology/operation/*.md`（及启用时的 action）的"逻辑描述"，识别形如「当前状态 = A，触发 E 后状态变为 B」的语句，登记：

   | 当前状态 | 触发事件 / 条件 | 目标状态 | 来源文档 |
   |---|---|---|---|
   | A | E | B | `ontology/operation/X.md` |

3. **死锁 / 孤岛 / 不可达检测**：
   - **死锁**（有入无出）：可被到达，但无任何出向转移。
   - **孤岛**（有出无入）：定义了出向但无路径可到达。
   - **不可达**：枚举值中定义，但无任何转移能到达。

4. **幽灵事件 / 遗漏转移 / 冲突转移检测**：
   - **幽灵事件**：某状态下不应触发的事件却被定义了处理（如"已完成"出现"审核通过"分支）。
   - **遗漏转移**：某状态在某个常见事件下没有显式处理（既无转移也无显式拦截）。
   - **冲突转移**：同一 (当前状态, 事件) 出现多个目标状态且无优先级。

5. **与 Action 的闭环验证**（若启用 action 阶段）：
   - 每个触发状态变更的 Action 是否定义了目标状态？
   - Action 是异步时，状态变更何时生效（触发时 / 完成后）是否明确？
   - 是否存在 Action 回滚导致的状态倒退，且该回退路径已纳入转移矩阵？

**报告项**：每个问题输出 `{对象} : {状态} : {问题类型} : {来源文档列表}`。

**防错**：
- 状态枚举必须显式存在于对象属性中，不允许校验器"推断"状态集。
- 转移必须来源于文档原文（operation 的逻辑描述 / action 的描述），不允许脑补补全。
- **终态合法**：某些状态本就是终态（已关闭 / 已完成），有入无出不算死锁——需在对象文档中显式标注"终态"。
- **防御性事件合法**：有些"幽灵事件"是业务上有意为之的防御性处理——需在 operation 描述中显式说明并标注"防御性"。
- 异步语义不能假设瞬时——Action 完成后才生效的状态变更要在矩阵中体现两次（触发时进入"中间态" / 完成时进入"终态"），否则会被误报。

**BFO 解释**：
- 状态值（A、B、…）= time-indexed Quality；
- 转移事件 = Process Boundary；
- 转移过程 = Process（即对应的 operation / action）；
- 完整状态机 = "Quality 集合 + Process 集合 + Process Boundary 集合" 的三层组合。
- 转移闭环校验本质是验证这三层在文档中**齐全且对齐**。

---

## 10. 提醒触发约定（供命令/agent 引用）

> 本节是命令端"何时主动提示用户参考 BFO"的单一事实源。新增触发点请改本节，不要把触发逻辑散落到各命令。

### 10.1 触发时机

| 时机 | 命令 | 出现位置 |
|---|---|---|
| 对象提取完成、等待用户确认时 | `/ontology-extract-object` | 列出对象数量、请求 **确认** 之前 |
| 逻辑提取完成、等待用户确认时 | `/ontology-extract-operation` | 列出逻辑数量、请求 **确认** 之前 |
| 校验完成、写收尾小结时 | `/ontology-validate` B6 末尾 | "校验全部完成" 文案之前 |
| 整体视图报告生成后 | `/ontology-overview` Step 5 | 收尾小结末尾 |

> 每个时机**最多出现一次**，避免连续阶段重复打断。

### 10.2 触发条件（命中任意一项即提示）

**对象名 / 逻辑名 关键词**（区分大小写不敏感，子串匹配）：

- 状态机型：`状态 / 转换 / 变更 / 流转`
- 批次/序列：`批次 / 批号 / 序列 / 序号 / 流水`
- 信息体（ICE）：`工单 / 单据 / 表 / 记录 / 凭证 / 报告 / 合同 / BOM`
- 计划/规范（Plan）：`计划 / 规范 / 方案 / 工艺 / 规则`
- 设备/能力：`设备 / 装置 / 机床 / 产线`
- 角色/身份：`角色 / 身份 / 操作员 / 审批人`
- 物理位置：`仓位 / 工位 / 车间 / 班次`

**校验报告 issue category 关键词**（仅用于 `/ontology-validate` B6 时机）：

- `规则冲突` / `状态相关` / `时序` / `关联无支撑` / `关系应转Operation`

### 10.3 标准措辞（命令/agent 直接复用）

**短形式**（无具体匹配项时使用）：

```
💡 可选参考：本次涉及范畴较多，可参阅 `.claude/reference/BFO_MODELING_GUIDE.md`（§6 范畴映射 / §7 反模式）核对建模结构。无需中断当前流程。
```

**带匹配项的形式**（命中 10.2 关键词时使用）：

```
💡 可选参考：检测到与 BFO 范畴敏感场景相关的命名 —— {matched_keywords，最多列 5 个}。
建议在确认前快速对照 `.claude/reference/BFO_MODELING_GUIDE.md` §6 与 §7 核对范畴归属与命名建议；如需要调整对象/逻辑结构，可在确认前直接说明，照常修改后再继续。
```

**校验报告触发形式**（命中 issue category 时使用）：

```
💡 可选参考：本次报告包含 {category 列表} 类问题，BFO 视角下常与"状态未拆三件套""设计/实例混淆""时序未索引"相关。
可参阅 `.claude/reference/BFO_MODELING_GUIDE.md` §5.3 / §5.1 / §5.10 排查归因。
```

### 10.4 约束

- 提示**永远不阻塞**用户的当前回复；用户忽略即继续。
- 不要把提示包装成必须回答的问题（不要加 `回复 是/否`）。
- 检测不到关键词或 issue 时**不出现**该提示——避免噪声。
- 若用户已在当前会话中回复过"不需要 BFO 提示"或"知道了"之类含义，本会话内不再触发。
- 未来如果把某条 §5 原则升级为强制规则（即从"软提示"变为"机检规则"），同步：先从 §10.2 移除对应触发关键词或条目，再按文末「变更约定」更新 `VALIDATION_BOUNDARIES.md` 与相关模板。

---

## 11. 延伸资料

- BFO 2020 官方仓库：`https://github.com/BFO-ontology/BFO-2020`
- ISO/IEC 21838-2:2021 (BFO)
- IOF（Industrial Ontology Foundry）：`https://www.industrialontologies.org/`
- CCO（Common Core Ontologies）：`https://github.com/CommonCoreOntology/CommonCoreOntologies`
- RO（Relation Ontology）：`https://github.com/oborel/obo-relations`
- Barry Smith 课程讲义：`http://ontology.buffalo.edu/smith/`

---

## 变更约定

- 本文档为参考性内容，更新无需走 phase commit 流程；可单独 `docs: update BFO modeling guide` 提交。
- 若把其中某条 "原则" 升级为强制规则，需同步：
  1. 在 `.claude/VALIDATION_BOUNDARIES.md` 增条目并标注归属（规则/语义）。
  2. 在对应模板（`object-template.md` / `operation-template.md`）加字段或说明。
  3. 在本指南顶部加 "⚠️ 已纳入强制检查" 标记。
