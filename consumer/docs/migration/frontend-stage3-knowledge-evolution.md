# Consumer 前端阶段 3：知识包、图谱与 Evolution 验收记录

## 1. 范围

- 分支：`codex/frontend-knowledge-evolution`
- 基线：阶段 2 `codex/frontend-chat-observability@9493bbe`
- 日期：2026-07-15
- 目标：保留 Meta-wise 知识包工作台的信息架构，补齐版本导入冲突、数据源隔离、当前会话运行态图谱以及 Operation / Skill Evolution 完整生命周期，并同步删除对应历史实现。
- 上一阶段：[阶段 2：聊天、Trace 与观测](./frontend-stage2-chat-observability.md)
- 总体方案：[Consumer 前端收敛与能力加强计划](./frontend-convergence-and-enhancement-plan.md#阶段-3加强知识包图谱和-evolution)

## 2. 知识包文件与版本边界

当前工作台继续提供知识包列表、目录树、文件查看、版本切换、ZIP 下载、启用/禁用、版本激活、历史版本删除和整包删除。文件内容的写入边界进一步收紧：

- 当前激活版本中，只有 `objects/` 和 `operations/` 直属目录下的 Markdown 文件允许在线编辑和上传；保存后同时刷新文件内容、目录元数据和知识包状态。
- 历史版本的内容始终只读，不显示编辑或上传入口；管理员仍可单独执行“设为激活版本”或删除非激活历史版本，这些属于版本管理操作，不改变只读内容边界。
- `assets/` 只支持浏览和随整包下载；scripts、mappings 及其他不符合编辑约束的文件保持查看模式。
- 下载明确携带当前查看版本，历史版本可以独立打包下载。
- 文件读取绑定 `Workspace + 知识包 + 文件路径 + 查看版本`，切换任一作用域都会中止旧请求并拒绝迟到响应，旧文件内容不会回显到新选择。
- 知识包列表、目录树、导入、启停、激活、删除和下载均检查当前 Workspace 请求作用域；Workspace 切换会清理选择、导入上下文、操作状态和旧目录。

## 3. ZIP 导入与三种冲突语义

ZIP 导入改为可测试的显式状态机，覆盖文件选择、安全确认、上传解析、冲突、成功、失败、重试、返回冲突决策和取消。首次上传前必须确认文件来源和上传责任；仅接受非空 `.zip` 文件。

服务端返回版本冲突后，界面只开放与冲突类型相符的覆盖操作，并提供创建新版本路径：

| 决策 | 适用场景 | 成功后的版本语义 |
|---|---|---|
| 覆盖匹配版本 `overwrite_matching` | 上传包声明的版本已存在 | 替换该同名版本内容，并立即把该匹配版本设为激活版本 |
| 覆盖激活版本 `overwrite_active` | 上传包未声明版本，需覆盖知识包当前版本 | 原位替换当前激活版本内容；目标版本继续保持激活 |
| 创建新版本 `create_version` | 需要保留现有版本 | 校验新版本号后写入新版本，并立即把新版本设为激活版本 |

三条路径完成后，目标版本都会成为知识包的当前激活版本。创建新版本在写入前仍是独立版本快照；导入成功并激活后，工作台按“激活版本可编辑、其他历史版本只读”的统一边界展示。

失败处理保留所选文件和上一次请求决策，可直接重试；冲突决策失败时还可以返回当前冲突对应的处理选项重新选择。确认或冲突阶段关闭会清空文件、冲突和重试上下文。上传解析阶段的按钮明确为“停止等待”：浏览器会中止等待，但同步服务端可能仍在解析或写入，结果也可能已经生效；页面会在 2.5 秒后自动刷新知识包、目录和激活版本进行一次对账，并持续提示以当前列表为准。旧的 `window.confirm` 导入流程及未使用的免责声明 helper 已删除。

## 4. 数据源密钥与作用域隔离

共享连接和知识包直连两种模式保持在同一工作台中，并统一以下约束：

- 服务端返回的数据只读取 `has_<secret>` 标记，不把任何密钥值复制到公开配置或表单草稿。
- 密钥输入使用密码字段；已有密钥时显示“已配置，留空保留原值”，空值不会进入保存 payload，`has_<secret>` 标记也不会被回传。
- 新建直连配置时，必填密钥必须实际输入；编辑已有配置时，服务端的已配置标记可满足必填校验。
- 共享连接与直连配置的加载、保存和解除配置均绑定 `Workspace + ontology`；请求同时使用 AbortController、scope key 和递增 sequence，旧 Workspace、旧知识包或旧请求的响应不会覆盖当前状态。
- 切换数据源模式或作用域会重建公开草稿，成功和失败均显示明确反馈，避免密钥与配置在知识包之间串线。

## 5. 当前会话运行态图谱

本体结构图继续按当前工作台选中的知识包和版本读取静态节点、边与来源文件，同时接入当前会话 canonical conversation / `agent.v2` Trace：

- `deriveGraphRuntimeState` 是无 React 依赖的纯转换，将 envelope、canonical message、专业化 block 和嵌套子 Agent 工具事件投影到已有节点与边。
- 匹配只读取工具输入、结构化输出和明确的资源字段，并在轻量规范化后做精确标识匹配；普通自然语言和图数据库实例 id 不会造成误命中。
- Trace 只投影到相同 ontology；当前会话元数据指向其他知识包时不显示命中。静态图仍以工作台当前查看版本为准。
- 运行中工具标记为 `live`；最新完成 run 或最近窗口内命中标记为 `recent`；更早 run 标记为 `history`。节点和关系使用相同优先级合并多次命中。
- 明确关系与推断关系、Operation 与业务对象、运行中/最近/历史命中均有独立图例；节点宽高和布局比例固定，不随文本或命中状态缩放。
- “重置视图”恢复滚动原点并通过 live region 给出反馈；全屏可通过按钮或 Escape 退出。
- 有来源文件的节点支持鼠标、Enter 和空格打开对应文件，SVG、节点和关系列表保留可访问名称。

## 6. Operation Evolution 生命周期

Operation Evolution 保留建议、证据、暂存补丁和管理员审批语义，并补齐生产入口中的管理操作：

- 建议按 `ontology / operation` 聚合，详情关联原始证据；支持认可、拒绝、编辑建议、重新扫描以及按当前 ontology 导出建议与证据。
- 暂存补丁展示原文与候选内容；管理员可应用补丁、丢弃并拒绝源建议，或只丢弃补丁并让建议回到待积累状态。
- 应用补丁后统一切回激活版本并触发知识包刷新，已打开的文件和图谱不会继续使用旧 revision。
- 列表、详情、选择项和写操作均绑定 `Workspace + ontology + operation`。切换 Workspace、知识包或 Operation 时会中止旧请求，迟到成功/失败信息不会污染当前选择。
- 历史版本下 Operation / Skill Evolution 的写操作全部禁用，并明确提示进化写入会影响激活版本；用户必须返回或激活当前查看版本后才能继续。
- Operation 详情、证据和暂存 Diff 绑定当前 `selectedKey`，Skill Diff 和 drift 绑定当前 `task_id`；选择切换时同步清空旧详情，状态不匹配时不展示也不允许提交。
- 加载、空数据、错误和每个写操作都有独立状态；无管理员权限时暂存补丁保持只读。

## 7. Skill Evolution 生命周期

Skill Evolution 按后端真实状态契约展示并管理任务：

- `submitted`、`running`、`downloading` 为活动状态；存在活动任务时每 5 秒轮询，切换 Workspace 或 ontology 会立即清空任务、Diff、选择和反馈并中止旧请求。
- `completed` 表示任务完成、等待审批，它可以读取 Diff，但尚不是可删除终态。
- `approved`、`rejected`、`no_change`、`failed`、`interrupted`、`cancelled` 为可删除终态；只有管理员显示删除入口，删除任务不会回滚已经批准的知识包版本。
- 活动任务可由管理员或任务发起人取消；任务按 ontology 分组和折叠，并显示进行中、待审批和终态数量。
- Diff 同时提供行级增删、行内词级标记和长段未变化内容折叠；文件级折叠可独立展开。
- `completed` 任务支持“创建新版本”和“覆盖激活版本”两种批准模式。新版本名可留空由服务端生成，也可输入通过格式校验的名称；创建成功后新版本立即激活。
- Diff 未完成或发生 drift 时批准按钮锁定；拒绝路径始终可用。服务端仍在批准时执行 drift 校验，所有审批、拒绝、取消和删除都显示成功或失败反馈。

## 8. 同阶段删除范围

能力迁入当前工作台后，同一变更删除以下历史页面和专属工具：

- `src/components/OntologyManagement.jsx`
- `src/components/OntologySelector.jsx`
- `src/components/OperationEvolution.jsx`
- `src/utils/ontologyCommand.js`
- `src/utils/ontologyImportDisclaimer.js`
- `src/utils/ontologyImportDisclaimer.test.js`

历史 `ConfigurationCenter` 同步移除已删除的 Ontology 页面入口和引用。`src/index.css` 删除旧知识、Ontology、Operation Evolution 及相关重复 selector，共净删除 1916 行样式；新的导入、图谱和 Evolution 样式随 Knowledge lazy chunk 按需加载。入口基线由 13 个批准的不可达生产模块降到 8 个，未批准孤儿模块保持 0。

## 9. 自动验收

### 9.1 前端

```bash
cd consumer/frontend
npm run lint
npm test
npm run build
npm run check:entry
```

- ESLint：通过，0 error / 0 warning。
- Node tests：142 项通过。
- Vite build：通过。
- 入口可达性检查：通过；批准的不可达生产模块 8 个，未批准孤儿模块 0 个。
- 新增回归覆盖导入安全确认、三种冲突请求、版本号校验、失败重试与取消；密钥不回显、空密钥保留和数据源 scope；Trace 节点/边命中、运行态分级及误命中保护；Evolution scope、任务分组、状态权限、审批 payload、行/词 Diff、未变化折叠和 Operation 证据导出范围。

### 9.2 后端

- 阶段 3 受影响后端回归：173 项通过。
- 回归范围覆盖知识包与版本路由、ZIP 导入冲突、知识包数据源、Operation Evolution 和 Skill Evolution 的现有服务端契约。

## 10. Bundle 对比

| 资源 | 阶段 2 | 阶段 3 | 变化 |
|---|---:|---:|---:|
| 入口 JS | 516.38 kB | 524.56 kB | +1.58% |
| 入口 JS gzip | 161.77 kB | 164.46 kB | +1.66% |
| 全局 CSS | 164.83 kB | 137.95 kB | -16.31% |
| 全局 CSS gzip | 29.93 kB | 25.47 kB | -14.90% |
| Knowledge lazy JS | 28.31 kB | 72.09 kB | +154.64% |
| Knowledge lazy JS gzip | — | 23.52 kB | — |
| Knowledge lazy CSS | — | 10.71 kB / gzip 2.53 kB | 新增拆分 |
| Config lazy JS | 61.84 kB | 61.84 kB / gzip 16.76 kB | JS 不变 |
| Observability lazy JS | 40.97 kB / gzip 12.90 kB | 40.97 kB / gzip 12.90 kB | 不变 |
| Observability lazy CSS | 17.62 kB / gzip 3.62 kB | 17.62 kB / gzip 3.62 kB | 不变 |

Knowledge chunk 的增长来自导入状态机、数据源草稿、运行态图谱以及 Operation / Skill Evolution 的完整交互，仍只在进入知识工作台时加载。全局 CSS 与新拆出的 Knowledge CSS 合计 148.66 kB，较阶段 2 的 164.83 kB 下降 9.81%；gzip 合计 28.00 kB，下降 6.45%。入口 JS 增长 1.58%，低于单阶段 10% 门槛。

## 11. 浏览器验收

已在当前分支本地服务完成以下非破坏性检查：

- 1440、1024、768 三档宽度下知识包文件、数据源、图谱和 Evolution 页面布局可用，无新增页面级横向溢出。
- 切换历史版本后显示只读提示，编辑和上传入口关闭；返回激活版本后恢复允许范围内的编辑能力。
- 已配置数据源的密钥不回显，密钥字段留空时保持“保留原值”语义。
- 当前会话 Trace 可命中对应图谱节点与关系，图例状态正确；全屏、Escape 退出和重置视图可用。
- Operation 和 Skill Evolution 的无数据场景使用正式 empty state。
- 图谱全屏具备 `dialog` / `aria-modal` 语义，进入后焦点受限在全屏区域，Escape 退出后焦点恢复到“全屏查看”；重置视图通过 live region 返回“本体图视图已重置”。
- 完整刷新后浏览器控制台未出现新增 error 或 warning。
- 深色主题的状态色全部使用语义 token，主题切换与深色模式由自动测试覆盖；本阶段浏览器视觉复核使用浅色主题，深色视觉回归纳入阶段 4 的个人外观入口验收。

本轮浏览器验收没有上传会改变知识包内容的真实冲突 ZIP，也没有执行 Evolution 的批准、拒绝、应用补丁、取消或删除等真实破坏性写入。三种导入决策和 Evolution 写操作由纯逻辑测试、请求契约测试与后端回归覆盖；浏览器验收只核对现有数据、只读边界、非破坏性交互和空状态。

## 12. 阶段结论

阶段 3 的代码范围、自动门禁、核心响应式与非破坏性浏览器验收已完成：知识包内容和版本管理边界明确，导入冲突具备三条可恢复路径，数据源密钥与 Workspace 作用域隔离，图谱运行态来自当前会话 Trace，Operation / Skill Evolution 覆盖完整服务端生命周期，对应历史页面、helper 和专属 CSS 已删除。

阶段 3 没有遗留代码阻塞项；下一阶段按[总计划的阶段 4](./frontend-convergence-and-enhancement-plan.md#阶段-4加强配置管理和权限)继续配置、管理和权限收敛，并完成可见的深色外观切换与视觉复核。
