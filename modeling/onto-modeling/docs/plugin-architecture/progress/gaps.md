# 差距记录

做不到「完全一致」的地方（RULES.md「遇到问题」）：差距、原因、什么时候补。

## 2026-11-01 · B1 评测移出

1. **`check_eval_generalization.py` 待 B4 迁到反馈演进包。** 包机制还没有（D1～D7 在 B3），脚本先留在 core 工具箱，已改为读反馈入口写入的 `feedback/*.json`。`/ontology-evolution` 命令同理，随反馈演进包一起迁。
2. **仓库根目录的整栈部署脚本仍往 onto 的 `.env` 写 `ONTO_WEB_MODELING_URL`**（`deploy/deploy.sh`、`deploy/install-consumer-modeling*.sh`、`deploy/upgrade-consumer-modeling-legacy-interactive.sh`，`deploy/tests/test_workbench_deploy.py` 断言它）。onto 已不读这个变量，留着无害；属于整栈部署线，B1 没改，需要主控决定是否清理。
3. **Modeling 核心服务侧的配套配置没动**：`modeling/.env.example` 的 `MODELING_MCP_CLEANUP_TOKEN`、`modeling/deploy/cce/chart/templates/workloads.yaml` 给 onto 注入的 `ONTO_WEB_MODELING_*`（`modeling/deploy/cce/tests/test_contract.py` 断言）。那是评测侧（Modeling）的代码，按详设第 11 章划给评测侧处理。
4. **待处理汇总里的外部反馈（kind `feedback`）、所有者的反馈列表与状态标记（R-11、R-12）没做**：待处理（inbox）与反馈演进包都还没有，反馈先只落盘提交。
5. **SV-04（`GET /api/v1/projects/{id}/solver/receipts`）没做**：面板仍读旧接口 `.../package/solver-validations/latest`，B2 改 `/api/v1` 时一起改；回执记录仍用 epoch 秒存时间，X-04 响应转成 RFC 3339。
6. **旧前端里的评测界面、Modeling 关联界面还在**，调用的接口已删除（前端整体重写）。
7. **契约第 7 章对象形状尚未定稿**：`ReleaseRecord`、`ReleaseManifest`、`Receipt`、`Feedback` 先按 `contract-changes.md` B1 第 5 条返回。

## 2026-11-01 · F1 前端（设计系统、外壳、数据层、入口页、工作台对话）

视觉对照方法：Playwright 1440×900，设计稿 `file://…html#/<页面>` 与本地 mock 开发服务器同一状态各截一张，逐像素比（阈值 RGB 差 > 40），脚本在 `本机临时目录`。

1. **无主项目指定所有者（契约 3.4 AD-06、AD-07）没有界面**：设计稿没画入口，按任务要求不自行加。现在项目没有所有者时，打开项目显示「这个项目还没有所有者，管理员指定后才能打开」。需要主控决定在哪一页补入口（建议运维中心「用户、审计与回收站」）。
2. **维护模式横幅没有设计稿**：按设计系统自拟（顶栏下一条琥珀色 / 红色细条，写模式与运维填的提示）。需要设计确认。
3. **与真实后端未联调**：后端 v1 还在改造，F1 只对 mock 跑通；接口路径、请求体、错误码、事件名全部按契约写。I1 阶段联调。
4. **只差在原型演示控件上的页面**（其余像素一致）：
   - `login` 0.46%：右上角演示状态切换；对照时用脚本填了同样的用户名和密码（产品输入框默认空、带占位文字）。
   - `projects` 1.15%：设计稿默认展开第一张卡的菜单、演示切换；展开菜单后对照 0.38%（只剩自主构建卡的流光动画）。
   - `import` 四个状态 0.18%～0.24%：演示切换。初始「还没选文件」的状态设计稿没画，用了新建向导的拖放区样式。
   - `create` 第 2、3 步 0.05% / 0.00%；第 1 步 1.72%：设计稿在示例写法下有一整行演示切换，产品没有这行，下面的「好的目标写清楚三件事」上移 50px；第 4 步 3.71% → 引擎标题行保留 42px 后只剩演示切换。第 2 步设计稿第 5 个文件停在「上传中 72%」，产品上传完成。
   - `wb-l2` 0.05%：几乎一致（流程图卡顶部滚动位置差 1 行像素）；建模步骤抽屉 0.04%。
   - `chat-states` 首屏 0.06%，往下 700 / 1400 / 2100 / 2800 像素分别 0.13% / 0.05% / 0.12% / 0.00%：差在「功能点清单」按钮（原型专用，已去掉）和转圈动画。
5. **wb-l2 交互后的状态与设计稿脚本的差别**：
   - 回答决策卡后新起的一段带时间（设计稿那段没写时间）；
   - 确认生效后对话里多一条「这一轮已确认生效 · 版本 v0.13」提示（契约 CS-04 要求出 notice，设计稿只弹了 toast）；
   - 待确认、已生效两态的建议条末尾多「全部命令」（契约固定有，见 decisions-log）。
6. **还没接的弹窗和页面**（F2）：流程图编辑弹窗、待处理、资料分析 / 分段规划、增量更新、自主构建、Studio、资料、发布、插件页、项目设置、全局设置页先显示「后续版本接入」。对话里的产物卡双击会跳过去。
7. **对话页里设计稿没单独画、按设计系统补的交互**：@ 引用条目的搜索浮层、命令浮层（样式取 chat-states 第 2 项）、拖入文件时的整页遮罩（取第 17 项）、消息旁悬停出现的复制 / 沉淀按钮、顶栏运行计时与锁标记、上下文环点开的用量浮层（取第 10 项）。
8. **沉淀为业务知识**：作用范围只能选「全局」，按条目选范围的选择器没做（等知识沉淀页 F3 的条目选择组件）。
9. **「这一轮改了什么」逐条「对比」**：明细视图里的「对比」在说明页只做提示，没接 CS-03 的 `target` 参数；工作台里「查看改动」展开整份差异。
10. **新建向导第 4 步 L4 说明里的「先看看默认设置」**：项目还没建，没有可去的项目设置页，改成文字说明。
11. **生产包 570 KB（gzip 180 KB）**：主要是 Markdown 渲染（react-markdown + remark-gfm）。F2 加页面时按路由拆包。
12. **README、`.env.example` 没改**：新增的开发变量 `VITE_API_MOCK=1`（只在开发 / 测试构建生效）应写进 `modeling/onto-modeling/README.md`，那不在前端目录里，留给主控。

### 标注自查（`标注.json`）

- `login` 10 条、`projects` 17 条、`import` 15 条、`create` 23 条、`wb-l2` 14 条、`chat-states` 26 条：文字和出现条件都已实现，悬浮提示（wb-l2-013、014）走 `data-pop`。
- 有出入的：create-022 末尾的「先看看默认设置」链接（见上第 10 条）；projects-017 里「个人设置」链接指向的页面在 F4。

## 2026-11-01 · B2 · 非项目级路由

1. **auth-server 没有 ops 角色（E3/P1）**：AD-02 改成 `ops` 由 auth-server 拒绝（400）；契约只给 ops 的运维写接口过渡期 admin 也放行（decisions-log）。
2. **auth-server 能力不够的字段**：AD-03 `require_change` 忽略；AD-01 `new_7d`、`locked` 为 null；登录没有锁定与限频，A-01 不会返回 `account_locked`、`rate_limited`；A-05、A-06 吊销是按用户整体，当前设备也要重新登录。
3. **AD-04 `projects="trash"` 不支持**：删除项目已改为永久删除，回收站只剩旧版存量；只做了 `transfer`。
4. **A-05 不关用户事件流**：用户流（A-12）还没有。A-07 `last_login` 只在内存里。
5. **资产包上传 `POST /api/settings/assets/upload` 原路径照旧工作**（不在兼容表、无 Deprecation 头），去向 PL-13 属于 B3 包仓库；O-30/O-32/O-33 先按资产包过渡实现（contract-changes 第 19 条）。
6. **O-37 同步备份**，`task_id` 查不到（O-40 任务中心以后做）。
7. **O-13**：模型网关只查是否配置，没有真实探测；`runtime.last_exit` 为 null；O-14 立即探测没做。
8. **O-01**：告警、自主构建为空；`calls_today` 为 null；费用是 SDK 报告的美元数（B14 单价表以后做）。
9. **审计记录缺字段**：`audit.jsonl` 由鉴权中间件在处理之前写，没有响应状态、IP、链路 ID；契约 1.12 要求响应之后写 `{status, ip, trace_id}`。需要改 `services/audit.py` 与 `api/v1.py` 的鉴权中间件（主控的骨架），本次没动。
10. **SY-02 `compact_at_pct` 不保存**：服务端按窗口与最大输出推算（或 `.env`），页面填的值只校验范围。SY-12 不支持 bearer 鉴权、不列工具；SY-13 不做 MCP 握手。
11. **MCP token 没有 `onto_pat_` 前缀**（`services/onto_mcp/tokens.py` 签发的是裸 urlsafe 串），`evidence.read` scope（E13）与包贡献的只读工具（`pack_tools`）还没有。
12. **本部分契约里没做的接口**（没有旧接口、属于以后阶段）：A-08、A-12～A-14；SY-14～SY-18；AD-08～AD-10；O-02～O-12、O-14、O-16、O-18～O-29、O-31、O-34、O-38～O-47、O-49、O-51、O-54。C-02～C-04 由主控在 `main.py` 做（C-04 尚无）。

## 2026-11-01 · B2 · 项目级接口、发布与会话

1. **契约里依赖以后阶段能力的字段给空值**：分段、自主构建、变更集、写入范围锁（`lock` 恒为 null）、执行记录、页面命令任务（`command_task`/`reread_task`/`review_task` 恒为 null）、证据、包——见 `contract-changes.md` B2 各条。随 B3、B4 补。
2. **项目列表不算「可发布」**：P-01 的 `publishable`、`counts.publishable` 恒为空 / 0（门禁要逐项目渲染）。流程运行时与待处理汇总（P2）落地后由缓存提供。
3. **会话事件只换了信封和事件名**，第 4 章逐类载荷（`message.assistant` 拆出 `tool.activity`、卡片、问题对象、进展归并）等会话宿主改造（P2）。
4. **没做的项目级接口**（以后阶段的新接口）：P-15 项目事件流、R-05～R-12、ST-06/09/10、K-06～K-08、V-02～V-05、CS-*、S-14～S-20、FL/BP/IN/INC、RP-01/04/05/06、PL/AR、SC-*、EV-*、AB-*。
5. **审计仍在处理请求之前记录**，不带状态码、IP、链路 ID（E4，P1）；专项审计只补了发布（`publish.release`、`publish.download`）、导出迁移包、确认卡回复。
6. **运维管理面挂在主监听**，独立监听与 `ONTO_WEB_OPS_ON_MAIN` 随 E8（P1）。
7. **Claude 引擎的阶段提示与知识信号仍靠 hook**，统一到宿主注入是 P2 的 B4 完整版（Jiuwen 已接，见 ADR-0022）。
8. **启动自检只有残留锁与未提交改动两项**；装配目录、依赖可达、中断执行、历史自愈等随启动恢复注册表（B8，P1）与执行记录（B5）补上。`last_exit` 恒为 null。
9. **B1 遗留的处理**：B1 第 5 条（SV-04、回执时间）已补：`GET /api/v1/projects/{id}/solver/receipts` 读回执，接口层时间一律 RFC 3339（回执记录内部仍存 epoch 秒）；旧面板路径经兼容中间件改写到 SV-04。B1 第 7 条（第 7 章对象形状）：`ReleaseRecord` 已按 7.14 对齐，`Receipt`、`Feedback` 仍是 B1 的形状（7.4 的 `Feedback` 多出的 `state`、`summary`、`suspected` 等字段随反馈演进包补）。B1 第 6 条（旧前端里的评测界面）不变，等前端线替换。

## 2026-11-01 · B3a · 插件运行时与流程运行时

1. **没做的插件接口**：PL-04（申请平台安装，依赖管理员审核 AD-08）、PL-12（扩展项注册表，方案 D17，P2）、PL-19（平台安装提供方，H2，P3）。PL-01 的推荐、`offline` / `not_applicable` 状态，PL-18 退回时通知上传者，PL-17 的灰度放量与自动熔断（O-25～O-29）都还没有。
2. **事件没发**：项目事件流 P-15 还没有（B2 遗留），`assembly.updated`、`extensions.updated`、`flow.updated` 没有地方发；对话流的 `session.config_pending`、`session.reconnected` 已发。MCP 只读工具 `flow_get` 没加（onto_mcp 在 B3b 范围）。
3. **流程运行时只做到 P1**：FL-03～FL-05（跳过、恢复、开始节点）、8 种节点状态与按段的进度、完整的下一步优先级、`state_helper suggest`、`flow.lock` 与节点挂载都在 P2 / P3。`post_commit_hook.sh` 按提交前缀给的提示仍在（P2 随宿主注入退役）。
4. **受保护目录第 3、4 层不完整**：提交检查已拦暂存的 `resource/`、`.claude`、`.onto`；合入时的检查要等统一变更事务（C1）与变更集（C8）；第 4 层进程隔离（agent 改不了）要等密钥移出（E10）与按项目隔离（P4）。`git add -A && git commit` 写在一条命令里时提交检查看到的是旧暂存区，方案放在提交接口（C11，P2），这次没动。
5. **备份范围不含包仓库**：`services/backup.py`（运维面，B3b 范围）没加 `packs/`；异机恢复后锁定了上传版本的项目会退回 stable。需要 B3b 在备份范围里加 `data/packs/`（`assembled/` 可重建，不用备）。O-32 回滚前的包仓库备份已有（`pack-repo-backups/`）。
6. **写入路径还用镜像版本的契约**：`workspace/files.py` 的 `write_file` 同步脚本描述符仍用镜像版本的 `script_descriptor`（写入路径是 B3b 的范围）；B3b 的 ChangeTx 里改用 `contracts.for_project(root).script_descriptor`，锁文件提交也改走 ChangeTx。
7. **core 清单要跟上 B3b 新增的脚本**：`assets/workspace-toolkit/manifest.yaml` 的 `contributes.scripts` 要加 B3b 的 `onto_metamodel.py` 等新脚本（漏了只会在静态检查里给「未声明」警告，不影响装配）。
8. **不再用的装配目录不清理**：`assembled/` 只增不减（每个不同的锁组合一份）；P2 加引用计数清理。
9. **前端**：`LeftPane`、`ChatPane` 改读 `GET /flow`，记在 `frontend-progress.md`。

## 2026-11-01 · F2 前端（Studio、报告、流程图、待处理、自主构建、资料分析、建模蓝图）

视觉对照方法同 F1（Playwright 1440×900，阈值 RGB 差 > 40，脚本在 `本机临时目录`）。示例换成合成场景后，数据文字和设计稿不同属于预期差异，各页的差异比例与原因见前端线 F2 报告。

### 主控

1. ~~契约里还有 4 处旧示例~~：主控已改为合成场景（陈列上限、超量与审批提示、§9.2 超量处理 / 补货标准表、ERP · 商品品类）。
2. **`src/lib/solver-contract-model.js` 的三个行业专用字段**没改，原因见 decisions-log「F2 主控」。
3. **主入口包 647 KB（gzip 205 KB）仍超 500 KB 警告**；F2 页面都已拆包，主入口里主要是 F1 的 Markdown 渲染和 antd 依赖。另有 `elk-worker` 1.59 MB（流程图自动布局依赖），只在用到时加载。
4. **与真实后端未联调**：F2 的接口全部只对 mock 跑通，I1 阶段联调。

### Studio 与报告（F2-A）

5. **mock 的 S-05 `open_questions` 总是空的**：决策卡那句更具体的锁定说法（studio-006、studio-table-015）在 mock 下出不来，逻辑有单测覆盖。
6. **几个跳转去的页面还没做**（都是 F3）：「查看原文」去 resources / schema；「刷新后对比」去 versions（带 `path`、`from`、`to=WORKTREE`）。
7. **原版图谱自带的缩放和小地图还在**（属于画布，不要求一致）。
8. **普通条目没有删除入口**：设计稿只画了删流程图；ST-10 的 mock 有。
9. **Studio 的流程图预览是单独一份（`FlowPreview`）**，可以换成 F2-B 导出的 `FlowchartSvg`，之后合并。

### 流程图编辑弹窗（F2-B）

10. **真实 drawio 编辑器没验证过**：`vendor/drawio/webapp` 里只有 `.gitkeep`，开发服务器上 `/drawio` 返回 500，只验证了降级路径（只读预览），开发控制台因此有一条 HEAD 探测的 500。embed 方式沿用旧代码，联调阶段验证。
11. **保存后对话里的流程图卡只更新规模文字**，缩略图不会重新生成（缩略图由服务端卡片数据给）。

### 待处理与自主构建（F2-C）

12. **开始自主构建没有单独界面**（AB-01 读策略、AB-03 预览、AB-04 开始）：设计稿 l4-run 只画了运行中和结束两态。入口在建模蓝图（F2-D 已接 AB-03 / AB-04）、项目设置（F3）、⌘K（现在只跳到 l4-run）。AB-04 的 mock 只返回一个排队中的运行，不会起新对话。
13. **查看改动跳的 versions 页在 F3**，现在显示「后续版本接入」。
14. **对话里助手名旁有时间；运行中补充说明的气泡下写「这一步结束后送达」**：都来自 F1 的 Timeline，没动。
15. **mock 不模拟运行推进**（用时、token 不跳），只模拟暂停、续跑、停在安全点、转为陪伴、复核、回退；EP-16 预算提醒有处理，但 mock 不发。
16. **共享单车故障诊断用的是补货场景的运行数据**；它的项目卡写「待处理 0」（F1 fixture），进去后待处理是 6。
17. **待处理没有「忽略」入口**（设计稿没画），只在没有设计稿的事项类型的通用详情里出现。

### 资料分析与建模蓝图（F2-D）

18. **「从已有知识包导入」契约里没有接口**：现在发一条 S-20 命令，让助手列出已发布的包。
19. **mock 的局限**（`index.js`）：S-05 上下文用量固定 42%（设计稿是 24% 和 46%）；回答资料用途问题只给「已回答」，不会真的改用途；「打断」找不到正在跑的读取，提示「现在没有正在跑的步骤」。
20. **时间线里步骤文字不支持粗体**（设计稿「等你确认蓝图」是粗体）。

## 2026-11-01 · B3b 元模型、写入事务、治理平面、密钥隔离

1. **前端还有一份链接正则**（`frontend/src/components/CenterPane.jsx:208`）：前端线改读 C-05 `GET /api/v1/metamodel`。
2. **工具箱里没收拢的链接写法**：`field_semantics.py:67`、`:555` 是遮罩用的正则片段，`generate_overview.py` 已列入删除清单（方案第 3 章），都没改；`ontology_checker.py:629`、`:1428/1432` 是句式识别，不是取链接目标。
3. **契约库与 manifest**：`onto_metamodel.py` 是新增的工具箱脚本，合并后要登记进 `assets/workspace-toolkit/manifest.yaml`，并由契约库（`services/packs/contracts.py`）按锁定版本加载，替换 `app/kernel/metamodel.py` 里的按文件加载。
4. **agent 自己的提交还不带 trailer**（C11 agent 提交收口在 P2）；版本列表把没有 trailer 的提交记为助手。审计按轮记 `agent.commit`，trailer 取最新提交上已有的。
5. **MCP 写入在 agent 有暂存改动时仍返回 `PROJECT_EDITING_BUSY`**：ChangeTx 已经只提交本次路径，这道整项目忙的判断按 C10 在 P2 换成写入范围锁时删除。
6. **`workspace.changed` 还没有 SSE 订阅方**：项目事件流 P-15 不在本阶段；流程运行时（B3a）、产物索引（P2）可直接 `subscribe`。
7. **启动恢复注册表（B8）没做成统一注册表**：写入意图恢复、退出记录放进了启动自检（`startup_report.run`），就绪检查等启动恢复做完才 ready。
8. **运维面没做的接口**：告警规则与导出器（O-08～O-12）、插件运维（O-25～O-28、O-31、O-34，归包仓库）、恢复演练（O-38、O-39）、任务中心（O-40、O-41）、数据保留（O-43、O-44）。O-37 仍同步备份。
9. **指标与链路在进程内**：重启清零，没有 OTel 等外部导出器；守卫拒绝只统计 Jiuwen（Claude 的 hook 拒绝在 CLI 里，后端看不到）；合入、证据采集在 P2、P3 前恒为 0。
10. **agent 隔离的残余风险**：
    - CLI 进程本身仍有当前会话的模型 Key，agent 与 CLI 同一用户，可读 `/proc/<pid>/environ`；彻底拿走靠 P3 的模型网关代理（E16）；
    - 所有项目的 agent 是同一个系统用户，能读别的项目的工作区与 Jiuwen 工件区（`data/jiuwen/` 整个归 agent 组，含 Jiuwen 存档库）；按项目隔离在 P4；
    - Claude CLI 的 Bash 工具经 `CLAUDE_CODE_SHELL_PREFIX` 执行按 CLI 文档实现（hook、MCP 走前缀已在 CLI 代码里确认），还没用真实模型端到端确认一次；
    - 用户切换的测试只在 Linux 以 root 跑时执行（Linux 容器全量会跑到），macOS 本机跳过。
11. **发布 compose 没打开 `ONTO_WEB_OPS_ON_MAIN`**：没有反代的单端口部署，运维中心经业务端口访问会 404，需要在 `.env` 打开或加反代（deploy/README 已写），待主控决定默认值。
12. **配置注册表只覆盖后端读的变量**：沙箱网关进程、部署脚本里的变量（`ONTO_MODELING_*` 等）不在里面。
13. **装配目录对 agent 只读要装配器配合**：启动时 `agent_identity.prepare_filesystem` 把已有的 `data/assembled/` 设成 agent 只读；运行中新建的装配目录要在 `Assembler.build` 里调一次 `agent_identity.share_read_only(目录)`（否则后端 umask 007 建出的目录 agent 读不到，CLI 加载不到命令）。B3b 分支上没有装配器，合并时由主控接上（一行）。

## 2026-11-01 · B3 合并（B3b 合进交付分支）

B3b gaps 第 3、11、13 条与 B3a gaps 第 5、6、7 条已在合并后的衔接提交里处理。下面是合并时看到、本次没接的：

1. **B3b 给插件运行时留的登记点还没接**（都不影响现有测试，接上要补测试）：
   - 就绪检查的 `assembly` 项（`governance/health.register_check`）：注册前恒为 ok；「装配目录就绪」的判据（启动核对失败的项目数？stable core 能否装配？）要先定；
   - O-29 熔断的包查询（`ops_control.register_pack_lookup`）：要返回 O-25 的一项，而 O-25～O-28 还没做，现在一律 404 `pack_version_not_found`；
   - O-42 版本清单的包与 `packs.lock` 格式（`ops_control.register_version_provider`）：现在只有内核、元模型、引擎几项；
   - 提交 trailer 的 `Onto-Packs`（`writepath.register_trailer_provider`）：现在所有提交都不带；接上后 `test_writepath.py` 里按整份 trailer 相等断言的用例要跟着改，agent 提交带 `Onto-Packs` 属于 C11（P2）。
2. **元模型按项目锁定版本只收了 Studio 一处**：MCP 文档模型（`onto_mcp/documents.py`）、发布 frontmatter 的 `uses.objects`（`publish/ontology_package._used_objects`）、`package_mapping` 的关系端点、兼容中间件的条目 ID 仍用镜像版本（`metamodel.mm`），要穿项目根目录才能改；在 core 的元模型出现第二个版本之前结果相同。
3. **旧 core 版本没有 `onto_metamodel.py`**：B3a 期间入库、被项目锁定的 core（没有元模型脚本）在加载元模型时退回镜像版本并每次记一条警告；交付分支没发布过，存量环境里应当没有。

## 2026-11-01 · F3 前端（插件页与项目页）

视觉对照方法同 F1、F2（Playwright 1440×900，阈值 RGB 差 > 40，脚本在 `本机临时目录`）。数据文字与设计稿不同属于预期差异，各页的差异比例见 `frontend-progress.md` F3 一节。

### 共通

1. **与真实后端未联调**：F3 的接口全部只对 mock 跑通。B2、B3a 的实际口径里不少字段还是 null（R-01 被引用数、R-02/R-03 不返回任务、`Version.run`、SV-01 的 `problem_families[].label` 等），页面不会出错，只是相应的横幅、计数、进度条出不来；I1 联调。
2. **页面命令（S-20）的命令名和参数是前端定的**：各子任务的约定见 `contract-changes.md` F3 一节，后端实现 S-20 时要对齐（或在命令目录 S-18 里给出）。
3. **主入口包 695 KB（gzip 221 KB）**，比 F2 多约 48 KB：增量更新的卡片要随对话渲染，把 `plugin/parts.jsx`、`more.css` 和知识沉淀的作用范围选择带进了主包。其余 F3 页面都按路由拆包（最大的项目设置 54 KB）。
4. **保留但没人用的旧组件**：`src/components/ExcelView.jsx`（调的 `api.excelPreview` 本来就不存在）、`HtmlPreview.jsx`、`cm/MergeDiff.jsx`。资料区、版本对比都按设计稿另写了；删不删由主控在 I1 定。
5. **跨页 mock 只做到大致同一时刻**：工作台确认生效写死 v0.13，版本页顺延；`raw:changed` 下 Studio 说 v0.13 是助手改的，版本页里 v0.13 是自主构建；回退只在版本页生效，Studio 条目不跟着变。

### F3-A 知识沉淀与场景走查

6. 新建场景（AR-06）、手动重跑知识冲突检查（K-06 POST）没有界面入口（设计稿没画），mock 两个接口都有。
7. 交互走查在 mock 里只推进度、回最后一句话，不往对话里发 chat-states 第 20 项的走查卡。
8. ⌘K「补齐过期场景」没带数量（设计稿是「（3 处）」）。
9. 插件页左栏不显示 IN-01 `pack=trace` 角标（设计稿插件页的左栏没有角标）。

### F3-B 代码生成与求解

10. 两个合成脚本（`safety_stock_strictest.py`、`overage_compare.py`）不是 Studio 条目，选中时预览显示「读不到脚本原文」，「去 Studio 编辑」找不到条目；默认预览的 `build_replenish_order.py` 正常。
11. B2 后端的 SV-01 有三处显示不全：`option_labels`、`result_title`、`result_texts` 恒为 null（页面改读 `template_params`，不受影响，建议后端修）；`problem_families[].label` 等于 id；`evaluator.kind` 恒为 `script`。SV-03 的 `evaluator` 为 null，「评估器已解析：声明式 · N 条约束」退成「评估器：<文件名>」。
12. 没有删除求解契约的入口（设计稿也没画）；五份附属文件的 chip 在 1440 宽下折两行。
13. mock 的命令钩子拿不到开跑时的写入范围锁，求解契约跑完直接解锁，不进「修改待确认」。

### F3-C 增量更新

14. 真实后端下「已执行但还没确认生效」的清单找不到所在对话：IN-01 只列没执行的清单，现在靠引擎绑定的在线对话兜底；要稳需要变更清单列表接口，或 CS-01 能按来源筛选。
15. 从待处理执行清单（IN-03 `execute`）时，mock 只改状态，对话里不出执行过程（routes-inbox 没接 INC-04）。
16. mock 局限：执行中打断后变更集停在 open；确认生效后再点「修复场景 6」只把回归卡改成通过；上下文用量固定 42%（设计稿 18%）。
17. 清单弹窗编辑中按 Esc 会关掉弹窗，没保存的改写会丢（Modal 默认行为）。
18. 从别的路由切过来时，清单卡位置偏约 8px（工作台平滑滚到底和定位叠加）。

### F3-D 版本与发布

19. 数据来源「直接写锚点」只在提示条里说明，没有输入入口；P1 的 PB-02 锚点返回 404。「标注 Schema 资料」只跳到资料区，不带文件。
20. 对比只支持条目文件；场景这类插件产物在「改了什么」里列出来但不能对比。
21. ⌘K「标记一个版本…」只跳到版本页，不直接打开标记弹窗。
22. SV-04 在 `routes-publish.js` 和代码生成分组里各登记了一份，先登记的生效（mock 行为一致）。

### F3-E 资料区

23. drawio 编辑和「流程图已被助手修改」提示条没有实际验证：开发环境没部署 drawio 组件（同 F2 第 10 条）。
24. mock 下「在新窗口打开」打不开：F-04 地址开发服务器不提供。
25. 扫描件靠「R-06 没有文字块，且是 PDF」判断；建议 R-06 给一个能否抽出文字的标记，或返回 `preview_unavailable`（原因 `scanned`）。
26. R-02 修订资料按批次只发一次增量更新，服务端不知道一批什么时候传完；mock 里同一 `batch_id` 返回同一个任务。
27. 默认阶段多数文档显示「自动识别」（沿用 F2 资料地图的数据），设计稿只有表格带这个标记。

### F3-F 数据源、采集与快照、证据浏览器

28. 标注 schema-007（相邻记录里的「代码调用方」）没做：合成场景没有代码仓库数据源；后端返回代码类相邻记录时会用代码图标显示。
29. mock 的简化：分层摘要和 ER 只有少数地方随快照变；「本月采集」统计列表里所有已结束的任务；「已加载 7 个提供方」包含文件提供方。
30. 普通用户提交登记申请（EV-03）后，管理员那边的处理界面没有（AD-08～AD-10，同 F1 第 1 条）。
31. 访问审计弹窗、删除登记确认弹窗设计稿没画，按设计系统自拟；设计稿默认态高亮了一个字段，产品只在带字段锚点进来时才高亮。

### F3-G 项目设置

32. AB-04 的 mock 新开的运行一直停在排队中（AB-05 不认识新运行），工作台不显示运行条。
33. mock 里的待生效切换不会真的生效（没有「下一条消息」的钩子），只能撤销或刷新页面。
34. 升级只发 `next_message`，「只在下次自主构建时启用」（`next_autobuild`）设计稿没画入口；启用、升级前没有先调 PL-03 做兼容检查，检查不过时把失败项拼进提示。
35. 策略里的 `until_node`、`must_suspend_kinds` 没有界面（设计稿也没画）；「上次运行 #3」只是文字（AB-18 的摘要没有 `conversation_id`）。
36. 能力表「系统逆向建模」那行设计稿有个「⋯」菜单，产品只在有升级时把「停用」收进「⋯」。

### 标注自查（`标注.json`）

- knowledge 20 条、scenarios 18 条、scripts 23 条、incremental 17 条、versions 7 条、publish 11 条、resources 19 条、sources 18 条、snapshots 11 条、schema 9 条、proj-settings 31 条：除下面几条外都已实现。
- 有出入的：knowledge-007（每页 20 条，加「加载更多」）、knowledge-017 与 scenarios-004（「客户」换成「单位」「门店」）、scenarios-006（交互走查走选中的场景）、incremental-001（系统通知的版本号按 v0.12 口径）、incremental-016（mock 上下文固定 42%）、versions-002（图例按契约补了「回退」，「插件升级」写成「插件」）、resources-012（drawio 被助手修改的提示开发环境出不来）、sources-001 与 snapshots-002/008（「签发」写成「下发」）、schema-007（见第 28 条）、proj-settings-027/028（Token 创建后只留「我已保存，关闭」）。

## 2026-11-02 · B4a（会话宿主、执行记录、崩溃恢复、项目流）

字段级的空值和固定值见 `contract-changes.md` B4a 节，这里只记要后续阶段接手的事。

1. **留给 B4b**：
   - 统一下一步（`suggest`）的输入：节点与分段状态、写入范围锁。现在只按流程和执行记录算。
   - `scope.updated` 的数据源。登记前恒为 `state="none"` 的空锁。
   - 变更集钩子。`round_changes` 卡恒为 `merged`，`changeset_id`、S-05 `open_changeset` 为 null；agent 的提交直接进主线。
   - F3 要的「已执行未确认的变更清单要能找到所在对话」。
2. **合并 B4c 时接上**：
   - `workspace.changed.artifacts` 为空，要接产物登记；
   - `extensions.updated.changed` 恒为全部七类，要接扩展项注册表的比较函数；
   - S-20 的 `action` 由扩展项注册表登记的解析函数换成命令，没登记前一律 `409 pack_not_enabled`。
3. **等 P3**：`message.assistant` 的 `anchors`、`entry_links` 为空，要等证据快照。
4. **通知中心 A-13 没做**：用户流没有 `notification` 事件。
5. **`context.compacted.kept` 是固定值**：引擎不报告压缩时保留了什么。
6. **执行阶段没有「共几步」**：命令锁不带总步数，要等节点声明补上。
7. **项目流、用户流不持久**：重启即丢。契约允许，前端按 `stream.reset` 重拉快照。
8. **agent 提交钩子防不住有意绕过**：比如 `unset ONTO_RUN_ENGINE`、直接 `commit-tree`。要等 P4 按项目隔离 agent。
9. **Jiuwen worker 没在真实模型下跑过端到端**：现在只有假引擎和协议级测试，I1 联调时要跑。
10. **收尾时修了三处竞态**，详见 decisions-log 的 B4a 节，Linux 容器里才稳定复现：
    - 打断被一轮收尾改回「等待」；
    - 运行中改配置后，重载与记账的顺序颠倒；
    - 打断等待期间先到的 result 把工作过程收成「完成」，命令锁已放掉时执行记录也被记成「完成」，续跑建议就没了。
    第三处补了确定性用例，让假引擎在 `interrupt()` 返回前先出 result。

## 2026-11-02 · F4 前端（运维中心、插件中心、个人与系统设置）

视觉对照方法同 F1～F3（Playwright 1440×900，阈值 RGB 差 > 40，脚本在 `本机临时目录`）。各页差异比例见 `frontend-progress.md` F4 一节。

### 共通

1. **与真实后端未联调**：F4 的接口全部只对 mock 跑通。B2、B3a、B3b 没做的接口（O-08～O-12、O-14、O-18～O-29、O-38～O-47，AD-08～AD-10，SY-14～SY-18，X-06，A-08，PL-04、PL-19 等）页面给「暂时读不到这一块」或「这一版服务端还没有…」的说明，不会出错。
2. **设计稿没画入口的 5 组能力没有挂界面**（等用户决定）：链路检索 O-05、无主项目与指定所有者 AD-06/07、管理员审核申请 AD-08～AD-10、模型单价 SY-18、服务令牌 X-06。组件、mock、单测都写好，挂载方法见 `frontend-progress.md` F4 一节。F1 第 1 条（指定所有者）、F3 第 30 条（管理员处理登记申请）也等这一项。
3. **只有 PL-04 的 mock 把申请推进管理员审核队列**；EV-03（数据源登记申请）、K-08（提名回流）的 mock 还没接 `addAdminRequest`。
4. **左栏角标与计数只在进入页面时取一次**：运维中心「可观测 N」（O-08）、「降级」（O-13），插件中心各分类计数；在本页启停规则、立即探测后要刷新才变。
5. **`package.json` 有 9 个依赖已没有代码引用**：`antd`、`@ant-design/icons`、`@ant-design/x`、`@codemirror/merge`、`@codemirror/lang-markdown`、`@codemirror/language`、`highlight.js`、`mermaid`、`rehype-highlight`。删依赖要重新生成 `package-lock.json`，本阶段没动。
6. **mock 里两处 F1 fixture 和后端口径不一致**：`fixtures.packs()` 的 core 是 `1.4.2`，运维管控按 B3a 第 14 条写 `0.1.0+3f9a1c2d`；`fixtures.clientConfig` 的 `billing.currency` 是 CNY，B2 第 1 条单价表落地前是 USD。联调时以后端为准。

### F4-A 运维总览与可观测

7. B3b 的 O-08～O-12 还没做，告警、规则、导出器卡显示「没取到」；O-03 要有带包或节点标签的样本才有数据。
8. 后端链路的 labels 里没有 `user`，链路检索按用户筛要后端补（contract-changes F4 第 1 条）。
9. 按节点归因时后端 `version_or_source` 恒为 null，「来自」列显示「—」。
10. 会话池指标卡：后端给的是活跃数加单位「个」，显示成「7」配小字「上限 12 · 排队 1」，不是设计稿的「7 / 12」。

### F4-B 运维管控与健康

11. **mock 的只读拦截只看启动参数**：`?mock=maint:` 决定拦不拦业务写请求，维护向导里进入的维护不拦，刷新后横幅回到启动参数的状态。（`maint:readonly` 下运维面的写请求已放行，同 B3b 第 12 条。）
12. 人名依赖 AD-01：返回 `available=false` 时显示用户名。
13. 任务（O-40）、演练记录（O-39）只取第一页，没有筛选界面；灰度浮层只支持比例和按项目 / 按租户，不支持 `targets`（设计稿没画）。
14. 任务续采 / 重试返回 428 时只弹提示（已通知所有者），没有别的界面。
15. 标注 ops-ctl-017：B3b 的 `last_exit.code` 只给 null 或 0，显示「意外结束」，不推断「内存不足」。

### F4-C 用户、审计与回收站

16. AD-03 的「下次登录时要求修改」auth-server 不支持（B2 第 2 条），复选框停用并注明。
17. 「移入回收站」能不能用靠 O-52 `retention_days` 是否为 null 推断（contract-changes F4 第 5 条）。
18. 设计稿「（关卡）」这种节点类型契约没有字段，不显示；B2 下审计的 `pack`、`node`、`ip`、`trace_id` 为 null 时显示「—」、不出链路图标。
19. B2 后端的 `display_name` 就是用户名，审计里的操作者显示名靠 AD-01 列表映射。
20. `platform-data.js` 里每人只有 1 个项目（设计稿删除弹窗是 3 个）；无主项目 2 个、回收站 4 条在 `admin-data.js` 里合成。

### F4-D 插件中心与插件详情

21. **推荐**：B3a 后端恒为空，整条推荐带不出现；mock 只给主示例项目推荐。
22. **PL-04、PL-19 后端没做**（B3a 第 1、13 条）：mock 照契约，管理员安装直接装好（真实环境是 O-40 任务、几分钟），页面不跟进任务进度。
23. B3a 的 PL-01 不出 `offline`、`not_applicable`、`platform_available`；租户包「本项目不适用」只在 mock 里有（合成的 `property-compliance` 只对启用了物业服务行业包的项目适用）。
24. B3a 的 PL-02：`flow_insertions` 只有 `{id, title}`（title 暂取 ID），主线图按 ID 猜插入位置、胶囊上是 ID；`project_stats` 为 null，后两张统计卡显示「—」。
25. 卡片说明用新建向导同一份包目录（`fixtures.packs()`），比设计稿短（联网调研的说明是「离线部署已禁用」）；提供方与证据源页（EV-10）同一批，用 Oracle、达梦代替设计稿的界面录制、CLI-Anything。标注 plugins-007～015、019、020、028、029 的文字因此不同。
26. 「只在下次自主构建时启用新版本」（PL-07 `effective=next_autobuild`）后端在 P3 前按下一条消息处理（B3a 第 7 条）。

### F4-E 插件上传与审核

27. 以租户身份上传（PL-13 的 `tenant_label`）没有界面：设计稿没画，`services.uploadPack` 也不传这个字段。
28. 静态检查跑完没有用户流事件，有检查中的上传时每 2.5 秒轮询 PL-14。
29. 已发布上传的通道和灰度从 PL-15 拿不到，只能看审核记录（contract-changes F4 第 16 条）。
30. 灰度项目数和「指定项目」依赖 O-01：运维面单独监听、浏览器访问不到时只显示「约 10% 的项目」，也不能指定项目。
31. 灰度放量和自动熔断后端还没执行（B3a 第 12 条）；退回不通知上传者（B3a 第 13 条），toast 写明「不会自动通知上传者」。
32. 「暂不发布」不落状态，刷新后仍是「检查完成」；上传列表只取第一页（50 条）。

### F4-F 个人设置

33. **系统通知和标签页标题闪烁没有真正弹出**：外壳收到用户流通知时不读 A-08，也不弹通知、不闪标题。判断函数 `systemNotifyAllowed(prefs, kind, permission)` 与对应表 `KIND_PREF` 已在 `src/pages/me/meModel.js`，接线点在 `src/shell/Shell.jsx` 的 `ShellG`。
34. 深色主题能选能存，但没有样式；应用启动时读偏好切主题也没做。
35. A-08 后端没做（B2 第 12 条），读到 404 时按默认显示并提示改动暂时存不上。

### F4-G 系统设置

36. 后端还没有 SY-15～SY-18 和 X-06，页面给说明（`sys:p1`）。
37. 全局 MCP 的 kb-search 在项目设置 P-14 的 mock 里没有（那个文件归 F3-G）。
38. 「当前 2 个运行中」是 mock 的固定值，不跟着自主构建运行变。

### 标注自查（`标注.json`）

- ops 4 条、ops-obs 9 条、ops-ctl 40 条、ops-users 23 条、plugins 40 条、plugin-detail 11 条、plugin-upload 14 条、me 19 条、sys 34 条：除下面几条外都已实现。
- 有出入的：
  - ops-obs-009（「客户平台」改「外部平台」）；
  - ops-ctl-005（显示 `GET /api/health/ready`，不是 `/readyz`）、006（探测地址用 `gateway.example.com`）、017（见第 15 条）；
  - ops-users-006（「签发方」改「颁发方」）、015（错误留档每页 10 条）、016/017（`retention_days` 为 null 时换说法）、021（「管理员或运维可以恢复」）；
  - plugins-004/005（推荐理由是合成场景）、007～015/019/020/028/029（见第 25 条）；
  - plugin-upload-010（缺命令文件判失败、未声明给警告，按后端口径）、013（默认不指定项目），检查项标题用后端固定标签；
  - me-012（预览示例换成合成场景）、014（「已保存」只在保存后出现）、016/019（管理员摘要按 A-09 数据）；
  - sys-006（模型名举例用合成的）、011（覆盖 / 新增行数按数据算）、019（会话池改为「只读 · 运维可在运维管控里调整」，契约 #56）、022（网关端口按后端默认 8110）。

## 2026-11-02 · B4c（扩展项、插件产物、拆可选包、配额、第一个行业包）

1. **事件只到发送口，没接项目流**：`extensions.updated`、`assembly.updated`、`artifacts.updated`、`knowledge.updated` 经 `kernel/project_events.emit` 发出，合并前只留进程内最近 200 条。合并时由会话宿主一侧 `project_events.register_sink(...)` 接到项目流；`packs/service.emit_switch_events` 与 B4a 自己发的 `assembly.updated` / `extensions.updated` 只留一处（见报告「对接点」）。
2. **S-20 的 `action` 解析、提交后产物通知、提示规则注入都只给了函数**：`packs/registry.resolve_action`、`kernel/artifacts/service.after_commit`、`packs/registry.prompt_hints` 要会话宿主（B4a）在合并时调用。提示规则接上前，知识沉淀的纠错信号仍靠 `knowledge_signal_probe` hook（只有 Claude 引擎执行；装配目录里没有沉淀守则时不出声），接上后可以去掉这个 hook。
3. **PL-04（申请平台安装）没做**：针对的是平台还没装的提供方（H2，系统逆向建模在 P3），现在包仓库里没有「平台可装但未装」的包，管理员通知（A-13）也没有。PL-19 同理。
4. **详设 5.5 没做完的部分**：`/ontology 示例` 列出行业示例项目（包里有 `examples/retail-restock-mini/`，P-02 的 `example` 仍只认内置示例）；提名审核（AD-09、AD-10）与审核后发行业包新版本；「用到了哪条行业知识」只在工作区规则里要求助手写进数据来源或提交说明，没有结构化记录与「行业包升级后列出受影响的产物」（P4 版本归因）。
5. **页面命令参数里依赖 B4b 的部分**：`/ontology-update {change_list_id, fix_scenarios}` 要变更清单；`/ontology-evidence-impact` 在系统逆向建模包（P3）前不存在，S-20 返回 `pack_not_enabled`。前端要把 `/ontology-trace-refresh`、`/ontology-trace-walk` 改成后端命令名（contract-changes B4c 映射表）。
6. **配额只拦对话入口**：P-02、上传、引擎启动不查；`snapshot_git_mb` 能设不检查（证据快照 P3）；`check_collection`、`autobuild_budget_cap` 等证据采集（EV-14）、自主构建（AB-02/03）落地时调用。用量入账有约 1 秒滞后。
7. **左栏角标、写入范围锁**：`registry.register_badge_provider`（待处理按包计数）与 `artifacts.service.register_lock_provider`（写入范围锁）等 B4b 登记，之前角标恒为 0、产物不显示锁定。
8. **SC-01/02 的沙箱验证没有失败记录与耗时**：`script_verify` 只留通过的记录，`status` 只有 `passed|not_run`，`duration_ms` 为 null；要显示失败输出得让 `script_verify` 记失败结果（core 脚本改动，留到 codegen 页联调时定）。
9. **行业规则只有四类、按条目名匹配概念**：`require_relations_to`、`require_fields`、`require_sections`、`prefer_terms`；概念匹配是名称与别名的等于 / 包含，不做语义判断。
10. **存量项目升级会多一次锁文件提交**：锁文件版本 1 → 2 时补上拆出去的包，主线版本 +1（同 B3a 首次写锁文件）。
11. （补第 2 条）扩展项比较函数也只给了函数：`packs/registry.extension_differ()` 对上主线 `services/project_events.register_extension_differ` 的签名；合并后主线发 `assembly.updated` / `extensions.updated`，B4c 的 `packs/service.emit_switch_events` 里这两条要去掉，避免重复。


## 2026-11-02 · B4 合并（B4c 合进交付分支）

1. **B4a 第 2 条已接**（原文不改，在这里标注）：`workspace.changed.artifacts` 接上产物登记（`after_commit`）；`extensions.updated.changed`
   接上扩展项注册表的比较函数；S-20 的 `action` 由注册表登记的解析函数换成命令。登记都在 `app/wiring.py`（ADR-0045）。
   B4c 第 1、2、11 条（事件接项目流、三个函数要宿主调用、扩展项比较）同样已接；提示规则注入接上后 `knowledge_signal_probe` 已删。
2. **前端命令名对齐留到联调**，已记录、这次不碰 `frontend/`：`/ontology-trace-refresh` → `/ontology-trace-batch {keys}`，
   `/ontology-trace-walk` → `/ontology-trace-interactive {scenario}`（contract-changes B4c 映射表、本文件 B4c 第 5 条）；节点编号带包前缀
   （contract-changes B4c「流程节点」第 1 条）；场景产物在 `ontology/testcase/<逻辑>.md`，不是 `ext/trace/scenarios/`（contract-changes B4c 第 10 条）。
3. **合并后新发现**：
   - agent 删掉产物文件时没有 `artifacts.updated` 的 `deleted`：`after_commit` 只看提交后还在的行，`workspace.changed.paths` 里有这个路径；
   - 纯数据包（H0）当场切换时 `assembly.updated` 先后两条（待生效、已生效），前端按最后一条为准；
   - 产物映射挂在每次主线提交上，提交接口同步刷新产物索引，场景多的项目提交会慢一些；
   - 知识信号的提示文本换成知识沉淀包里的那段，不再列出命中了哪些词；
   - Jiuwen worker 进程按内核传的数据目录另建一个工作区管理器读装配与包仓库（只读）；
   - 默认的并发会话上限（每人 3 个）现在对续跑、重试、Studio 转复核也生效。
4. **审查修复**（B4c 只读审查确认的 12 条，用例在 `tests/test_pack_review_fixes.py`，修复前都失败）：
   1. 流程图标准保存丢泳道、批注、第 2 页：`flowchart.render` 改成在原文件上只改第一页里内核认得的节点、连线与流程元数据，
      其他单元格、其他页按 id 保留；没动的节点坐标样式不变，容器里的节点换算回相对坐标；删节点连带删挂在上面的单元格；
      高级编辑器保存以提交的 XML 为底。
   2. 场景保存删节内自由内容：`parsers._render_section` 只替换登记了的字段、分段对应的列表项，段落、表格、小标题原样保留。
   3. 网页新建场景不符合模板：产物登记加 `heading`（场景是「测试场景：{key}」），新节把登记了标签的字段写成列表项；
      `trace_inputs.parse_source` 能解析。
   4. 删项目绕开 token 上限：`purge_history` 先 `quota.service().sync()` 再删历史。
   5. Studio 转助手复核不过配额：配额检查放进 `SessionManager.send`（所有发起一轮的入口），HTTP 闸门保留作提前拦。
   6. 提名不重新脱敏：K-08 服务端重查，有命中 `422 nomination_not_sanitized`。
   7. 同名条目覆盖不了行业条目：新建时标题或 slug 与行业条目相同就沿用行业条目的 slug。
   8. 目录符号链接读到工作区外：产物索引读文件前 `resolve()` 确认仍在项目根目录内（`index.inside`）。
   9. core 回滚到拆分前后跟随项目装配失败：拆出的 7 个包与行业包依赖改 `core >=0.2`（各升补丁版本）；`follow()` 逐包试装配计划，
      配不上不跟；PL-05 只给能装配的回滚目标。
   10. 官方身份由清单自声明：改按入库来源（`PackRepository.is_builtin`），清单 `publisher` 缺省租户、只用于显示；清单里指向包内
       文件的字段（`flow`、`readme`、`knowledge_dir`、`artifacts`、`views`、`docs`）拒绝绝对路径和 `..`。
   11. 产物保存的版本校验与写入不在同一把锁里：保存、新建、删除的读版本、校验、算内容、提交都放进项目锁。
   12. 并发会话上限先查后起：并发检查和绑定会话在会话池的同一把锁里，`sending>0` 的会话计入占用。

## 2026-11-02 · B5a（包的界面、证据源只留接口）

分支 `feat/onto-modeling-plugin-p3-ext`（起点 af5659d3），ADR 0055、0056；契约见 contract-changes B5a 节。

### 和 B4b 合并时要接的

1. **产物卡的出卡点**在会话宿主 `services/agent_sessions.py::_emit_workspace_changes`：发完 `workspace_changes`（「这一轮改了什么」）
   之后调 `project_events.round_cards(项目, changes, 本轮起点, 终点)`，每张卡 `session.broadcast({"type": "card", "payload": 卡})`（3 行）。
   变更集上线后一轮的改动先进变更集副本，这 3 行要挪到「一轮结束、得到这一轮改了哪些路径」的新位置；`changes` 要是这一轮相对
   起点的增删改路径，`base` 是起点提交（一个文件多项的格式靠它按项比较）。生成函数 `kernel/artifacts/cards.round_cards` 与登记
   （`app/wiring.py::_round_artifact_cards`）不用动。
2. **卡片与编辑器的只读**用写入范围锁：卡上的 `locked` 取 `kernel/artifacts/service.register_lock_provider` 登记的锁（B4b 登记前恒为 false）；
   iframe 编辑器的只读状态取前端项目上下文的 `scope`（CS-06），保存时以 AR-05 的 423 为准。B4b 登记锁之后不用再改。
3. **`agent_sessions.py`、`project_events.py`、`wiring.py`、`api/v1.py`、`routers/packs.py`** 是这次改过、B4b 也可能改的文件；改动都是追加，
   冲突时两边都留。

### 没做或做不到的

4. **通用详情只读**：字段、分段、表格预览；详设 5.9.5「按格式定义自动生成编辑表单」没做，要编辑的产物走包自带编辑器或插件页面。
5. **只做了「编辑器」**：任务说明里的「弹窗」用同一套声明与加载（`contributes.editors` + `detail.editor`），没有单列一类；以后要让
   页面按钮打开包的弹窗，再给页面声明加一个引用编辑器的字段。
6. **PL-02 插件详情**没有列出包带的编辑器（`ManifestSummary.contributes.editors` 有，详情页没展示）；插件上传页的检查表按后端标签显示，
   新的 `ui` 项会自动出现。
7. **PL-21 的票据在后端进程内存里**：多进程部署要换成签名票据或共享存储；后端重启后已打开的 iframe 后续静态请求 404，重开即可。
8. **iframe 里的能力**：`sandbox` 只给 `allow-scripts`，包的编辑器不能弹新窗口、下载文件、全屏、用剪贴板读；CSP 只允许同源脚本、
   不允许内联脚本。包作者说明写在 README「包自带界面」一条。
9. **EV-29 的 `file` 锚点也回 501**（任务说明：EV-* 一律 501）：对话、资料、Studio 里出处角标悬停取原文暂时没有，浮层直接收起。
   证据源实现前要不要先单独实现 `file` 锚点（资料分析报告里的出处），待主控定。
10. **前端 mock 仍模拟证据源可用**（数据源、快照、证据浏览器、EV-01 有数据），用于和设计稿对照；真实后端下这些入口都不出现。
11. **打开产物时每次取一遍 AR-01**（`ArtifactOpener`），没有放进项目上下文缓存；扩展项变化后自然是新的。
12. **设计稿没有 iframe 编辑器弹窗**：版式自定（`styles/packs.css`，980×720，标题行带「插件 · 编辑器名」与只读标记）。
13. **端到端脚本在 `本机临时目录`**（按要求不进仓库）：`e2e.mjs`（Playwright 1.63，20 项检查）、`prep.py`（打包样例、往对话历史放一张产物卡，
    模拟一轮结束）。没有真实模型，产物卡是用宿主同一个生成函数放进历史的，不是真跑一轮出来的。
14. **开发模式**：包目录在前端工程外，`vite.config.js` 的 `server.fs.allow` 放开了 `../assets/packs`；第一次打开包编辑器时依赖已在启动时扫到，
    不会整页刷新（已验证）。

## 2026-11-02 · B4b（变更集、写入范围、节点状态、统一下一步、待处理）

字段级的空值和固定值见 `contract-changes.md` B4b 节，这里只记要后续接手的事。

1. **建模蓝图与分段没做**：BP-01～BP-03 没有实现（前端调用会 404）；FL-01 的 `slices`、`slice_stages` 为空，没有
   `per_slice` 节点。要先让资料分析（`core.research`）产出分段规划（蓝图文件格式、命令文档），再按详设 6.8 把建模节点
   重排成 `core.model`（按段：对象、操作、规则、写入即查、场景），写入范围才能「落到当前分段」收窄。B4b 之前的 P1
   节点集合（资料分析、对象、操作、规则与指标、质量检查、增量更新）保持不变。
2. **自主构建（P3，B5b）的接口已留**（ADR-0046 表格）：`open_changeset(source="autobuild", build_mode="L4", run_id, slice_id)`、
   `observe`、`merge(source="autobuild", needs_review=True)`、`revert_merged`、`discard`；节点状态
   `flow/state.mark_needs_review(root, nodes, "autobuild", run_id=, slice_id=)`；待处理的 `review`（autobuild）与
   `suspension` 来源、IN-04 `accept_high_confidence`、`ScopeLock.state=autobuild_slice`、`Suggestions.mode=autobuild_controls`
   （`flow_state.suggest` 已认 `context.autobuild`）等 B5b 接上。挂起问题进待处理要在 `inbox.collect` 加一个来源。
3. **节点包（P3，B5c）**：启用节点包时生成 `flow.lock` 作为变更集——锁文件现在仍按 B3a 直接作为一次主线提交合入（`Onto-Source: plugin`），
   B5c 要改成：写 `.onto/flow.lock` 后经写入事务提交（导入、插件变更「校验通过即合入，可以撤回」，接口上不可见），撤回用
   `revert_merged` 同一套反向提交；节点包的 `produces` / `writes` / `stale_on` 声明守卫、只读与过期自动跟上。节点 `backfill`、
   `disabled` 标记在 B5c 前恒为 false。
4. **变更点清单的格式**：清单由助手现有的影响分析条目映射（条件取理由、取值取变更说明、锚点为空、把握按改动类型），
   不是契约 7.10 的「条件 / 取值 / 范围 / 出处锚点」原样产出；要改 `update-impact-agent` 的输出格式。INC-05 的回归场景、
   `impact_summary.scenarios` 要接场景走查包的索引（哪些场景引用了受影响的条目），现在为空。
5. **格式迁移的处理**：待处理能列出格式待迁移的插件产物，但没有包提供迁移命令，`migrate` 回 `pack_not_enabled`。
6. **待处理现算**：每次 IN-01 都重算（含一次流程计算、扫描 reports/ 与 feedback/）；项目大时可能要按事件缓存。外部反馈没有
   「已处理」状态（R-11、R-12 没做），只能忽略。
7. **门禁只有规则检查**：合入时跑 `ontology_checker`（含示例数据）新增错误；包声明的门禁规则（`gates`）只在插件产物保存时
   查，合入时没并进来。规则检查要两遍，大项目确认生效会慢几秒。
8. **变更集期间的主线同步**：一轮开始前副本有改动、主线前进了且合得上才把主线合进副本；副本里有没提交的文件时不合，助手
   这一轮看到的是旧主线（合入时照样按实体检测冲突）。
9. **助手副本不进备份**：副本分支在工作区的 `.git` 里，恢复后启动核对按分支重建；没提交的改动会丢。
10. **真实模型没跑过**：副本、守卫按节点收窄、`state_helper suggest`、`/ontology-update change_list_id=…` 都只用假引擎与
    直接调用验证过，I1 联调时要用真实模型跑一遍陪伴建模（对象提取 → 确认生效 → 操作提取）与增量更新。
11. **决策卡的 `affected`** 由会话宿主给（B4a 现在恒为空，AskUserQuestion 不带改动预览）；写入范围已经认它，等引擎侧能
    给出改动预览时自然生效。
12. **升级后第一轮 Claude 续接**：助手的工作目录从项目工作区换成副本，Claude CLI 的续接记录按工作目录存，升级前的对话
    第一次在新版本上发消息时 CLI 续接不上（B4a 的回放兜底接手），之后在副本路径上照常续接。
13. **克隆、迁移导入**不带源项目的副本登记与副本分支（没确认的改动留在源项目）；导出的迁移包里仍有 `.git/worktrees`，
    导入时去掉。

### B5a 审查修复后（2026-11-02）

15. 独立审查的 7 条都已修，提交见 README 进度记录；用例在 `backend/tests/test_b5a_review_fixes.py` 与 `frontend/tests/packUi.test.js`（「审查第 N 条」），
    端到端加了「越权命令被拒」「iframe 导航后桥接断开」（自己重新加载、导航到不带头的外站、导航到带 `Allow-CSP-From` 的外站三种）。
16. **残余通道（ADR-0055 第 5 条）**：包页面离开时可以把已经读到的数据写进目标地址带出去；包页面在自己加载完之前就跳走、由外站页面抢先握手时，
    平台分辨不出来——Chromium 里有 CSP 嵌入强制兜着（外站页面加载不了或跑不了脚本），别的浏览器没有。都要靠包的审核、灰度、退回流程兜。
17. **服务端的命令限频与票据都在进程内存里**：多进程部署要换共享存储；限频只按「项目 + 包」，不区分用户（单人所有权下项目只有一个用户）。
18. 前端 `requiredCsp` 用 `window.location.origin` 拼来源：平台经反向代理换了对外地址时以浏览器看到的为准，没问题；但包静态资源如果以后挪到单独的域名，
    要跟着改。

## 2026-11-02 · B5 合并（B5a 合进交付分支）

B5a 第 1、2 条（出卡点、写入范围锁）已接，B4b 的锁登记早已在 `app/wiring.py`；做法见 decisions-log「B5 合并」。

1. **产物卡在确认生效前双击打开的是主线上的产物**：AR-03 读主线，助手这一轮改的还在副本里——修改的看到改之前的版本，
   新增的读不到（AR-03 回 `404 artifact_not_found`，编辑器与通用详情只能报错）。确认前看改动要走「这一轮改了什么」卡的查看改动
   （CS-03 认 `<类型>/<键>`）。I1 时定做法：卡片带 `changeset_id`、待确认时双击打开 CS-03 的差异，或 AR-03 加按变更集读副本。
2. **前后端写入范围的路径匹配口径不一样**：后端 `ScopeLock.paths` 是 fnmatch（`*` 跨目录，`[x]` 是转义成字面量的字符），
   前端 `state/readonly.js` 的 `globToRegExp` 是 glob（`*` 不跨目录，方括号按字面）。节点声明的 `目录/*` 与普通文件名两边一致；
   路径里带 `[ ] * ?`、或模式要跨目录时前端会判成可编辑，保存仍以 423 为准。I1 联调时统一（前端改按 fnmatch）。
3. **官方包编辑器（流程图）的只读仍只看锁里的产物**，没有按路径：助手在跑流程图节点、还没改到这张图时编辑器可编辑，保存回
   423 后提示并重新拉锁。上传包 iframe 编辑器已按路径（`editorReadonly`）。
4. **一轮开始多一次副本快照**（临时索引上 `git add -A` + `write-tree`），副本大时发消息会慢一点；取不到时退回上一张卡时的树。
5. **端到端脚本**：B5a 的脚本复制到 `本机临时目录`（改了路径，加了「2b 变更集与包界面」8 项），要用新的数据目录跑；
   复用数据目录时第一项（样例包已入库）必然失败，最后一项（停用图示包后打不开编辑器）也超时过一次。没有真实模型，副本里的那一轮
   由 `prep.py changeset` 按宿主同一个函数（`agent_sessions._round_artifact_cards`）模拟。

## 2026-11-02 · B4b 审查修复

1. **守卫拦 git 内部设置是按命令文本认的**：`git sparse-checkout`、`git worktree`、改 skip-worktree、改稀疏 / 排除文件 / 钩子路径相关配置。
   拼接、变量里的写法照样防不住（同 B4a 第 8 条），由合入时受保护路径取主线兜底。
2. **接回副本分支时两边分叉且合不上**：只留 `refs/onto/rescued/<时间>` 引用并记日志，界面上看不到；要人工合回。
3. **合入期间网页保存要等**：确认生效全程持项目锁（含门禁两遍规则检查），大项目可能等几秒。
4. **崩溃恢复只回滚整批写入**：合入写到一半崩溃，主线回到合入前，变更集仍待确认；重新点确认生效即可。
5. **state_helper suggest 比对时不排除资料区**：稀疏规则被关的那一轮里会误报有改动，下一轮开始重设稀疏规则后恢复。

## 2026-11-02 · B5c（节点包、扩展项补齐、插件运维登记点）

分支 `feat/onto-modeling-plugin-p3-nodes`（起点 b297f3b8），ADR 0065～0068；契约见 contract-changes B5c，取舍见 decisions-log B5c。

### 合并时要处理的

1. **变更集模块缺一个「记已关闭的变更集、不动 current」的函数**：插件变更集现在直接用 `kernel/changesets/store.create/update`
   写（任务要求不改变更集模块）。`store.create` 会把 `current` 指到新记录，所以助手的变更集没关闭时，不改流程的切换只提交、
   不记变更集（`changeset_id=null`，撤回只能靠 PL-10 / PL-09 / V-06）。主控合并时在 `store.py` 加一个只追加已关闭记录的函数
   （比如 `record_closed(root, **fields)`），`services/packs/changes.begin` 改用它即可，那时这类切换也能记成变更集。
2. **审查第 17 条（撤回时派生的 `index.md` 误判冲突）**：插件变更集的提交只改 `.onto/packs.lock` 与 `.onto/flow.lock`，不触发
   `index.md` 派生，`revert_merged` 的用例没碰到这个缺陷。
3. **写入事务多了门禁登记点**（`kernel/writepath/tx.py` 的 `register_gate_checker`，在 `_derive` 之后、提交之前）：B4b 审查修复
   如果动了 `ChangeTx.commit`，合并时两边都留。
4. **`routers/sessions.py` 的 S-11**：会话里找不到问题时交给 `routers/flow.answer_human_node`（人工节点）；B5b 动这个路由时保留。
5. **`project_events.EXTENSION_KINDS` 多了 `view_choices`**，`agent_sessions.py`、会话宿主没有改动。

### 没做或做不到的

6. **人工节点的卡不进待处理**：待处理（`services/flow/inbox.py`）不在本阶段改动范围；卡在对话里，节点在建模步骤抽屉里显示可开始。
   人工节点的问题文件（数据目录 `flow-human/<项目>.json`）删除项目时不清理（删除流程在会话宿主里，B5b 范围），残留无害。
7. **脚本节点的产出受执行器输出上限（1500 字符）限制**：适合检查结论；大产出要等沙箱支持回传文件。没有真实 jiuwenbox 下的
   端到端验证（用例用本机执行器模拟网关），V 阶段在有沙箱的环境里跑一遍。
8. **外部节点**：证据源只留接口，`available_providers()` 恒为空，外部节点一律不能启用。
9. **O-26 按租户灰度、PL-17 的 `auto_breaker`（失败率超阈值自动熔断）、O-25 的 `error_rate_pct`**：属于 P4（版本归因与灰度）；
   按租户回 `501 rollout_not_available`，错误率恒为 null，自动熔断没有触发方。
10. **O-28「已锁定它的项目进待处理，提示升级或回滚」没做**（待处理不在本阶段范围）；锁定版本的项目照常用被禁用的版本。
11. **告警规则只登记、不评估**：O-08 的 `state` 恒为 ok、`firing` 恒为 0；没有告警评估器，也没有 `auto_action=breaker` 的触发。
    包声明的指标只在 /metrics 给出 HELP 与 TYPE，取值要包的脚本或内核按同名记录（现在没有上报通道）。
12. **配置项只进平台的配置注册表**：取 stable 版本的声明、值是包里的默认值；设计稿「在项目设置里改，从下一条消息开始生效」的
    按项目取值没有落点（项目设置没有这块、契约没有接口），助手也还读不到这些值。
13. **前端没有消费的扩展项**：PL-12 的 `entries`（快捷项、附加操作）、`view_choices`，FL-01 节点的 `hooks`，前端都还不显示
    （节点钩子已经进了节点动作，抽屉里能点）。I1 接：快捷项放对话输入框的「全部命令」旁，附加操作放对应页面的操作区，视图选择
    决定 Studio 是否给 ER 视图。
14. **节点钩子靠提示让助手执行**：用户发宿主节点的命令时宿主在两轮之间提示；助手不照做时没有强制，钩子没有自己的完成判定。
15. **已停用节点只在 FL-01 列出**：项目列表、`state_helper` 都不知道它们（它们不在组装后的流程里）。
16. **macOS 本机**：`test_script_verify.py::test_pre_commit_blocks_unverified_scripts` 失败（bash 3.2 经钩子调 python3，已知的环境性失败）。

## 2026-11-02 · B5c 合并（B5c 合进交付分支）

B5c 第 1 条（记已关闭的变更集）、第 6 条前半（人工节点的卡进待处理）已做；第 2～5 条合并后核对仍成立（第 3 条的门禁登记点在
B4b 改过的 `ChangeTx.commit` 里，`_derive` 补记意图之后、提交之前）。做法见 decisions-log「B5c 合并」。

1. **助手的修改没确认时记下的插件变更集撤不了**：`revert_merged` 要在主线写反向提交，回退类写入在助手变更集没关闭时一律 423；
   PL-10 退回「按生效中的版本改回」（多一个提交，不复用那个变更集）。助手那组确认或撤回后就能撤。
2. **崩在锁文件提交中途**（写入意图补提交）：补的提交不带 `Onto-Changeset`（非整批回滚的意图不记变更集编号），启动核对找不到
   提交，那条插件变更集留在已撤回，PL-10 只能按生效中的版本改回。崩在提交之后、改记录之前的能按 trailer 补记。
3. **人工节点的卡在待处理里按 E 只写方向不选选项**：后端回 `400 answer_invalid`（人工节点必须选一个选项），前端照常提示错误；
   卡上没有「其他，我来说」，I1 文案审计时看要不要在这类卡上隐藏 E 的提示。
4. **熔断期间**待处理不列这个节点的过期与需复核；熔断解除后重新出现，之前的忽略记录照旧按指纹认。
5. **人工节点的问题文件**删除项目时仍不清理（B5c 第 6 条后半，删除流程在会话宿主里，B5b 范围）。

## 2026-11-02 · B4d（建模蓝图、分段、按详设 6.8 重排主流程）

字段级的口径见 `contract-changes.md` B4d 节，取舍见 decisions-log 同名节。分支 `feat/onto-modeling-plugin-p2-blueprint`，起点 170ff9f3。

### 合回交付分支时要处理（B5c 已先合回）

1. **`hooks` 键**：B5c 的节点钩子用 flow.yaml 的 `hooks`（`{id, title, node, when, command}`）；B4d 往分段里加步骤、改显示名用的是
   `slice_steps`、`node_titles`，两者分开，不要合成一个键（B4d 起初也叫 `hooks`，已改名以免和 B5c 的校验互相拒绝）。
2. **`services/flow/view.py`**：两边都改了 `build` 与 `_actions`。B5c 先算熔断（`overlay.released`）再 `state.compute`、之后补可补做；
   B4d 把节点状态抽成 `node_states(root, flow, hits)`（蓝图、分段汇总、`compute(sliced=…)`、`_apply_slices`），待处理也用它。
   合并后 `node_states` 里要按拿掉熔断节点的流程调 `compute`，`_apply_slices` 放在 `mark_broken` / `mark_backfill` 之前；`_actions`
   两个新参数（B5c 的 `hooks` 位置参数、B4d 的 `blueprint_draft` 关键字参数）都留；`core.research` 的「去确认」排在钩子动作前面。
3. **`flow_state._candidates`**：两边都改了第 4 级（B5c 的可补做排除与第 5 级「补做」，B4d 的蓝图与按段的下一步），合并后
   `required` 同时排除可补做节点，B4d 的分支照旧。
4. **`services/flow/model.progress`**：B5c 减可补做的分母，B4d 按段覆盖建模节点的完成判定，两段都留。
5. **`services/flow/inbox.py`**：B5c 加人工节点的决策项、不列熔断节点；B4d 加蓝图问题的决策项、`_node_items` 改用 `view.node_states`
   （过期项的指纹对建模节点用 `stale_key`）。合并后 `_node_items` 用 `node_states` 时也要不列熔断节点。
6. **`routers/flow.py`**：B5c 在 FL-05 里按节点类型分流（人工节点出卡、熔断回 409），B4d 在 FL-03～05 里处理 `slice_id` / `step`；
   建模节点是 agent 节点，B4d 的分支放在 B5c 的类型分流之后。
7. **发布门禁**：B5c 的包门禁在 `services/publish`，B4d 的流程关卡在 `routers/publish.check_view` 末尾追加，顺序是平台 → 包 → 流程。

### 没做完、做不到的

8. **第 9 章其余建议没做**：3（产出即查与收敛：修复两轮上限、修复前先看影响范围）、5（读取、编号、出处交给确定性工具：
   `source_extract` 强制使用、编号脚本分配、证据锚点必带）、7（场景前移：抽取操作时就生成验收场景）。分段流程用到的只有「写入即查」
   ——由确认生效时的门禁承担（ADR-0051），没有单独的命令。
9. **资料分析小结卡与 RP**：资料地图（R-07）、核验（R-09）没做，小结卡的数字只取验收回执；`report_id` 为 null（资料分析报告在
   `research/`，RP-02 只读 `reports/`），卡片上「看报告」点不了。
10. **证据提示**：`Blueprint.evidence_hint` 恒为 null（证据源只留接口）。
11. **「按章节拆分」**：靠助手重读资料、重写草稿（`/ontology-research phase=blueprint`），没有单独的接口；前端按契约用 S-20 发命令即可。
12. **条目归属**：一段「做完」按「规划的条目在主线上都有了」或「这一段这一步的变更集确认生效过」判断。模型改了名字（规划叫 A、
   实际建成 B）时，B 会在一轮结束时自动记进这一段，A 留在规划里；靠确认生效的记录照样算做完，全局一致性会报「规划了没建」。
13. **规则「按资料出现」**：按段模式下这一段没规划规则就跳过；全量模式下不会自动判「资料里没有规则」，规则一步保持可开始（可选、不挡）。
14. **守卫与决策卡**：决策卡改动预览扩出段外时，用户一侧的锁加上了，守卫一侧的 protect 不会放行（引擎侧的决策卡还不带
   `affected`，同 B4b 第 11 条）。
15. **protect 在 acquire 时算一次**：命令跑的过程中别的段又新建了条目（网页保存），守卫这一侧不拦它们；用户一侧的锁不包含它们，
   不会冲突（新建的只在主线上）。
16. **全局一致性的口径检查**按字面找非统一叫法（`strip_links` 之后的正文），统一叫法本身包含别的叫法时不报；会有少量误报，
   命令要求逐条让用户确认或改。
17. **进度 x/N 的分子分母随节点重排变了**：对象、操作合成「建模」（只有对象没有操作时不算完成），多了全局一致性与发布。已确认的
   步骤在「全部」那一段里保持已确认，不往回退。
18. **发布节点**：发布之后改了内容就显示过期（`stale_on` 条目类别），下一步不会推荐「去发布」之外的动作（面板节点没有命令）。
19. **真实模型没跑过**：资料分析第 6 步写草稿、建模命令按 `SLICE_MODE.md` 只做一段、全局一致性命令都只用假引擎与脚本验证过；
   V 阶段要用真实模型走一遍「资料分析 → 确认蓝图 → 第一段 → 第二段 → 全局一致性 → 发布」。

### 留给 B5b（自主构建）

20. 接口见 ADR-0050 的表格：`register_autobuild_preview`（登记后 BP-03 `next=L4` 返回预览，「确认后怎么建」卡出现）、
   `run_slices`（按段执行的分段列表与段说明）、段的命令带 `slice=`（执行记录、变更集 `slice_steps`、确认生效记段的步骤都自动接上）。
   自动合入一段时 `merge(..., needs_review=True)`：`wiring._confirm_nodes` 把这几段这几步记为确认过并带需复核，分段格子显示 `auto`。
21. `ScopeLock.state=autobuild_slice` 还没出（自主构建的一段按陪伴同样的口径锁这一段），B5b 接上运行状态时在 `scope_lock` 里区分。

### 前端 I1 要对齐

22. 节点编号变了：`core.object` / `core.operation` / `core.rule_metric` / `trace.mock` 不再出现（mock 数据里的 `core.rules`、`core.incremental`、
   `trace.regression`、`solver.*`、`evolution.feedback` 也要换成真实编号）；`core.validate` 的标题是「校验」或「校验与走查」。
23. 建议条 `send` 动作带 `args.slice`、`text` 为空串：前端走 S-20 时用 `command` + `args`，不要把空 `text` 当消息正文。
24. 建模步骤抽屉：没有蓝图时 `slices` 是一段「全部」，「调整分段」打开的蓝图弹窗会是 `blueprint_not_found`（见报告「需要改设计」）。
25. `core.publish` 的动作 `target` 是页面 id `publish`（`nodeActionKind` 认得）；`core.research` 的「去确认」`target` 是 `wb-blueprint`。
26. 「确认后怎么建」卡在 B5b 之前不出；`build_choice.options` 出现时两项都有。
27. BP-03 可能回 CS-04 的错误（门禁不过、合入冲突），前端按确认生效同样的提示处理。
28. P-01 `slices.cells` 的取值见 contract-changes B4d 第 15 条。

## 2026-11-02 · B5c 审查修复

交付分支 2642d9ef..e12e6651，用例在 `backend/tests/test_b5c_review_fixes.py`；做法见 decisions-log「B5c 审查修复」。

1. **已入库的旧版本不会重新检查**：审查第 1、10 条的静态检查只对之后上传的包生效；包仓库里已有的上传包靠运行时那道检查
   （脚本输出、决策记录不在写入范围里回 403，写入事务拒绝 `.git/`，装配时拒绝包外的节点文件）。
2. **旧网关**：脚本验证网关如果还是旧镜像（执行器不回报 `stdout_truncated`），脚本节点输出到上限长度时一律当截断处理（422），
   正好 1500 字符的合法输出也会被拒；网关随主服务同一镜像发布，升级后不存在。
3. **跟随通道推迟的提示**：助手修改没确认、跟随会改流程节点时只记日志，界面上没有「有一次升级在等你确认」的提示；
   确认或撤回之后下一条消息前自动跟上。
4. **灰度清零只在 stable 指针动的时候发生**：beta 版本被禁用（O-28）时分桶项目自己就不再拿它（`beta_for` 查禁用），放量记录不改。
5. **包的 MCP 工具名、指标名改了格式**（`<包>__<名>`、`onto_pack_<包>__<名>`）：B5c 起声明过工具或指标的包只有测试里的合成包，
   官方包都没声明，不需要迁移；契约补充见 contract-changes「B5c 审查修复」。

## 2026-11-02 · UI 文案（前端文案精简与设计调整，I1 时后端要做的）

分支 `feat/onto-modeling-plugin-ui-copy`，只改了前端（含图示包随平台编译的 `assets/packs/diagram/ui/`）。取舍见 decisions-log「UI 文案与设计调整（执行）」。

1. **停止下发说明槽位（口径 ⑤）**：字段清单见 contract-changes「UI 文案」第 1～19 条。前端已不显示，后端去掉即可；另把后端自己生成的对话通知文案（如自主构建「做完并合入」）、O-20 维护步骤的 detail 按口径 ③ 改成「生效」「自主构建」，不出现「合入」「主线」「L4」。
2. **证据源相关数据本期不下发**（前端已统一过滤，`state/evidence.js`：项目内看有没有数据源子页，项目外看 EV-01 读不读得到）：
   - FL-01 的「系统取证」等系统逆向建模包节点、`kind="external"` 的节点；FL-02 指向 `sources` / `snapshots` / `schema` 的建议（如资料阶段的「连接业务系统」）；
   - AB-03 预览的 `production`、`autobuild_start` 卡 chips 里的「生产库」一格；BP-01 `evidence_hint`；P-10 `evidence_sources`；ST-06 `snapshot_basis`；IN-01 `evidence_conflict`；
   - P-14 内置的 `evidence` 服务器、M-01 `pack_tools` 里的 `evidence.*`；PL-01 的提供方分类、系统逆向建模包与推荐它的条目；PB-01 系统逆向建模包的门禁（证据出处检查）；
   - O-02 `collections` 卡；运维管控数据保留表的「证据快照」行、任务表的「证据采集」、插件运维与版本清单里的提供方；SY-15 照常返回 `not_enforced`（前端另按 EV-01 再过滤）。
3. **接口不存在的分区**（R17 分区随接口出现）：真实后端 SY-17（自主构建限制）回 404，系统设置「配额与自主构建」里不画那张卡；SY-15 在，配额分区照常出现。后端补上 SY-17 后卡片自动出现，不用改前端。
4. **前端新认、待后端补的字段**：contract-changes「UI 文案」第 20～22 条（脚本节点 `last_run`、编辑器 `pack_label`、`entries[].where` 的取值）；R3 的蓝图卡 `build_options`（第 3 条）；PB-06 `source_version_label`（第 11 条）。
5. **求解契约缺 `decision_model`**（scripts-49）：前端不再报「未填写 Decision Model ID」，提交时补成 `<solver_key>_model`；SV-02 / SV-03 在缺这个字段时也应能自己推出来。
6. **端到端脚本**（`本机临时目录`，`bash run.sh <日志>` 每次新数据目录）：B5a 原有 33 项 + 新增 3 项（说明页不进生产构建、「更多」菜单没有入口、证据源不可用时配额页不出证据项）全过。两处偶发与文案改动无关：原脚本打开流程图编辑器后立刻读标题，偶尔读到占位「流程图」（副本里改成等标题换成产物名再断言，断言不变）；最后一项「停用图示包后打不开流程图编辑器」修脚本后 5 遍里有 1 遍等「插件已停用」超时（停用后装配切换的时机），其余 4 遍通过，V 阶段再观察。

## 2026-11-02 · B4d 合并（B4d 合进交付分支）

merge 4e6ed10f + 衔接 ee03c022；B4d 节第 1～7 条（和 B5c 要接的点）已接，做法见 decisions-log「B4d 合并」。

1. **前端 I1**：建模步骤抽屉的「调整分段」在 FL-01 `blueprint.state === "none"` 时改为 S-20 发 `/ontology-research phase=blueprint`（按钮文字不变）；
   蓝图卡底部按 `confirm_options` 出「确认，一段一段来」「确认，一路建完…」两个按钮（调 BP-03），不再有「确认后怎么建」卡（设计调整 R3）。
2. **资料分析小结卡的「看报告」**（主控定，不改设计）：I1 补 R-07 资料地图、R-09 核验，并让报告页收录 `research/` 下的报告（B4d 第 9 条，已记）。
3. **B5c 的节点包用例**改挂在 0.3.0 的节点上（资料分析之后、建模之前；挡住建模），原来挂在对象提取与操作提取之间的写法由 `aliases` 兼容
   （去掉 `before`，挂在建模之后）——这类老包的节点在新流程里的位置变了（从两步之间变成建模之后），没有更好的对应。
4. **节点钩子挂在建模上**：0.2 挂在对象提取上的钩子现在挂在建模（对象、操作两条命令都会提示），按命令分步的钩子没做。

## 2026-11-02 · B5b（自主构建）

分支 `feat/onto-modeling-plugin-p3-autobuild`（起点 c76c236d），ADR-0060～0063；字段级口径见 contract-changes「B5b」，取舍见 decisions-log 同名节。

### 没做完、做不到的

1. **真实模型没跑过**：运行器、策略代答、补充说明、门禁修复、重启续跑都只用假引擎验证。V 阶段要用真实模型跑一个 5 段的项目，
   重点看三件事：助手会不会按注入的约定在决策卡 header 里写「置信度 0.xx」与出处锚点、label 里标「（推荐）」「（保守）」（没写时一律按
   低于阈值处理，台账里会全是假设）；收到「挂起：……」的答案后会不会跳过这一项接着做；`/ontology-extract-* slice=<段>` 每一步做完会不会
   释放命令锁（不释放的会被说三次「继续」后记成部分完成）。
2. **场景推演通过数没有数据来源**：AB-05 `scenarios`、AB-16 `metrics.scenarios`、`slices[].scenarios` 恒为 0/0；场景走查包的结果格式定了再接。
3. **通知不落盘**：用户流推 `notification`（完成、新增挂起、停下），通知中心 A-13 没做，离线时收不到。
4. **运行器、会话池里「正在跑的运行」、开始前预览都在进程内**：多进程部署要换共享存储与调度（同命令限频、票据）；服务重启后预览失效
   （`410 preview_expired`，前端会重新预览）。
5. **生产库授权、夜间优先只存不用**（`not_enforced`）：证据源只留接口；夜间优先没有调度器。
6. **费用估算**：没配单价时 Jiuwen 记 0、Claude 用 SDK 报的美元费用（单价表不是美元时也记 0），都标估算；不做换汇。SY-18 设计稿没有画入口
   （前端组件已写好、未挂载，F4 遗留），管理员现在只能经接口配单价。
7. **按段撤回之后蓝图里留着这一段运行时新记进来的条目名**（规划了、主线上没有），这一段显示为没做完；后面的段、或人改过这一段的文件时
   不能单独撤回（`later_changes`），只能整版回退。
8. **没有自动决定的段**一直显示「自主完成」，直到整次运行的决定都复核完（没有单项可点）。
9. **门禁修不好的一段会被撤回**（只留一个挂起的问题），这一段已经做的内容不保留；回答挂起问题后在陪伴模式下整段重做。
10. **`include_optional`** 只影响分段里的可选步骤（规则）与分段之后的可选节点，不会把旁支（指标、流程图）加进来。
11. **运行中不能用别的对话**：暂停、中断的运行也挡着（这一段的变更集还开着），要先续跑、停在安全点或转为陪伴。
12. **改判、挂起问题的补做**都发到运行对话，跑出来的是一轮陪伴建模的修改（来源 assistant），要用户确认生效；改判时不会先撤掉原来的条目。
12a. **台账条目的 `affected`（影响到的条目）现在为空**：取的是决策卡的改动预览，引擎侧还不给（同 B4b 第 11 条）；Studio 的「需复核」按段
    生效时改到的条目标。
12b. **PL-07 `effective=next_autobuild`** 仍按下一条消息生效（B3a 第 7 条），没有「只在下次自主构建时启用」的待生效队列。
12c. **出错停下（`failed`）时这一段已经做的修改**留成一轮「修改待确认」（来源 autobuild），用户确认或撤回；运行报告里这一段是部分完成或未开始。

### 前端 I1 要对齐

13. 运行器发的步骤消息不进对话流（没有 `message.user`），运行对话里只有开始卡、分段进展块、报告卡、提示行、建议条与助手的输出；
    运行对话里发的消息回 `delivery=deferred`，进历史但不会马上有助手回复。
14. AB-01 没有 `boundary_text`、多 `not_enforced`；AB-03 没有 `production`；AB-04 可能回 `409 changeset_in_progress`（有没确认的修改），
    开始弹窗按错误文案提示即可（见报告「需要改设计」第 1 条）；SY-17 多 `not_enforced`。
15. 建议条：暂停、中断时 `mode=autobuild_controls`、`primary: null`、只有「运行详情」；运行对话结束时主推「去复核」。
16. 卡片取值：`slice_progress.state` 取 `done|running|queued|partial`（停下时没开始的段仍是 `queued`）；运行详情矩阵里没生效的部分完成格子是
    `skipped`；`result` 文案见 contract-changes B5b 第 7 条；运行报告卡带 `review_count`。
17. 待处理：自主构建复核项的 `meta` 为空串（UI 文案口径 ⑥）；低于这次运行阈值的决定 `high_confidence: false`（阈值不一定是 0.8）；合并项的
    `slice.name` 是「n 段」；门禁没过的挂起问题没有选项，只能自己写或「按通用做法」。
18. P-01 卡片的 `autobuild.state` 按 6 个取值归并（见 contract-changes B5b 第 26 条），`status` 可能有 `autobuild_done`。
19. 别的对话在运行没结束时发消息会 409 `autobuild_running`（错误文案前端已有），见报告「需要改设计」第 2 条。

## 2026-11-02 · I1（前后端联调）

分支 `feat/onto-modeling-plugin-i1`（起点 c76c236d）。清单与逐项状态见 `i1-checklist.md`，契约见 contract-changes「I1」，取舍见 decisions-log 同名节。

### 留给 B5b 合回后

1. **自主构建的接口还不在**：AB-01～AB-18、SY-17（自主构建限制）、SY-18（模型单价）回 404。冒烟里工作台、项目设置每次进来都有 `autobuild/runs`、`autobuild/policy` 的 404（控制台各一条），项目设置「自主构建策略」分区显示读取失败，系统设置不画自主构建限制卡。B5b 合回后这些自然消失。
2. **前端自主构建页与 AB-* 实际口径对齐**（运行页、运行条、台账、挂起、运行报告卡、策略表单、系统设置限制卡）、蓝图卡的「确认，一路建完…」（`confirm_options` 出 L4）：等 B5b 合回后联调。
3. **A-13 的 `autobuild_*` 通知**：B5b 直接调 `services/notifications.notify(...)`。V-02 的 `review`、`run` 恒为 null。

### 留给 V 阶段

4. **真实模型没跑过**：冒烟用的是假引擎（`本机临时目录`，每条消息在副本里写一个合成对象、回一段带 `[^1]` 的正文）。资料分析出报告与蓝图、资料分析小结卡与「看报告」、对话角标里真实助手是否按约定写 `[^n]`、Jiuwen worker、脚本节点在真实沙箱里跑，都要 V 阶段用真实模型走。
5. **脚本验证沙箱**：冒烟环境没有沙箱网关，就绪检查「降级 5/7」（模型网关、沙箱网关未配）。

### 没做完、做不到的

6. **pdf、旧版 doc 没有抽取器**（core `source_extract` 不支持）：R-06 返回空块、EV-29 解析为 `unreadable`。前端提示「读不出文字（可能是扫描件）」对普通文本 pdf 也会出现，见报告「需要改设计」。
7. **资料读取的实时进度与候选实时流没有来源**：R-07 `read_pct` 只有 0 / 100，`materials.candidate` 不发；`position`、`queue_reason`、`blocked` 恒为 null。
8. **ST-04 属性的 `evidence`** 仍为 null；ST-07 表单里属性的 `evidence_anchor`、`expert_confirmed` 在原文里没有存放位置，只随草稿的 `form` 保留。
9. **ST-07 表单渲染的限制**：改描述只写纯文本（原来那一行里的 `[[链接]]` 会丢）；加关联用默认写法（对象关系名「关联」、操作的角色「支撑数据的对象」、指标写到「数据来源」一节），要用户自己改；在别的条目或正文里声明的关联在表单里删不掉。
10. **Studio 保存的确定性检查不查引用块里的链接**（0054c939：模板的填写指引在引用块里）；引用块里写错的链接要到确认生效、发布时的规则检查才报。
11. **报告**的修复轮次、专家裁决没有数据来源（`repair`、`arbitration` 为 null）。
12. **通知来源**：`nomination_reviewed`、`request_pending`（等 AD-08～AD-10 挂界面）、`evidence_conflict`（证据源）、`publish_done`（发布是用户自己同步做的）没接。
13. **导出器**：OTel 与外部平台没做（依赖里没有 OTLP exporter，没加新依赖）；只列 Prometheus 与本地日志文件。
14. **任务中心**：备份是同步执行的，任务里不能取消；O-37 失败仍回 500，另记一条失败任务。
15. **数据保留**：审计日志 365 天只是声明，没有按期清理；会话历史没有清理逻辑（不列）。
16. **EP-09 `exec.updated`** 后端发、前端没订阅（续跑卡走 `card.updated`，待处理走 `inbox.updated`，不影响显示）。
17. **地址直达 `wb-blueprint?dialog=blueprint` 而项目没有蓝图时**，弹窗里显示「还没有建模蓝图」，控制台一条 BP-01 的 404。正常入口（「去确认」「调整分段」）在没有蓝图时不会走到这里。
18. **插件详情**：PL-02 `project_stats` 仍为 null（两张统计卡显示「—」）。
19. **e2e 脚本**（`本机临时目录`）在最后一项等「插件已停用」超时时多截一张图、打印地址（断言没改）；起初连续 3 遍失败的原因是登录后晚到的 `navigate('/projects')`（07c51621 已修），之后连过两遍。
20. **Studio 新建的条目带着模板的填写指引**：ST-09 用的是给助手的条目模板，新建后编辑器里「数据来源」等栏显示 `{待填写：…}` 原文，用户要自己删。要不要在 Studio 新建时去掉指引（只留章节骨架），待定。

## 2026-11-02 · B5b 审查修复

交付分支 c7afa68d..0dab01f0，用例在 `backend/tests/test_b5b_review_fixes.py`；做法见 decisions-log「B5b 审查修复」。

1. **B5b 第 7 条前半句已解决**：撤回时会去掉这一段这次新记进蓝图的条目；后半句（后面的段或人改过就不能单独撤回）不变。
2. **自主构建不能改名、挪动已确认的条目**：在 git 里是删 + 加，合入时按删除拦下，这一段撤回并挂起「要删除已确认的内容」，要用户回答后
   在陪伴模式下做。
3. **删除只在合入时拦**：权限模式全放行时助手在副本里删文件不会被工具层挡，到合入时才整段撤回（这一段别的修改一起丢）；守卫没有按
   「已确认」区分删除，因为副本里的守卫看不到主线上哪些是已确认的。
4. **留到下一次的回答只在范围包含那一段时用上**：选定范围没选那一段，问题就一直留在待处理里；只能「现在回答」或下次把它选进来。
5. **修复之前开始的运行**撤回时没有运行前的确认记录，仍按主线现状重算（过期的段会显示已确认）。
6. **启动时补完撤回只看最近 50 个提交**：撤回之后又提交了 50 次以上才重启，就要用户再点一次撤回（会补完、不重写）。
7. **会话池用例偶发失败一次**：Linux 全量第一遍 `test_session_pool.py::test_pool_evicts_oldest_idle_session` 失败（p1 没被驱逐），再跑全量、
   连跑 5 遍、单独跑 10 遍都过；用例靠 20 次 `sleep(0)` 等连接，负载高时可能不够，不是这次改动引入的（这批修复没动驱逐逻辑），没改。

## 2026-11-02 · I1 审查修复

独立审查 11 条（c76c236d..28e38e5d），修复 af22d849..0ed48162（13 个提交），每条先补修复前失败的用例（`tests/test_i1_review_*.py`）。取舍见 decisions-log 同名节。

1. **读取一侧的竞争窗口没堵**：按路径读工作区文件的入口都是「先判（resolve / 是不是链接）再读」，判和读之间助手把目录换成链接仍可能读到外面；彻底的做法是逐段 `openat(O_NOFOLLOW)` 打开，本次没做。靠另外两层兜：守卫拦建链接、每轮开始与结束删掉指到根外的链接。
2. **守卫只看命令文本**：`tar` / `unzip` 解出链接、`cp -s` / `cp -P` / `rsync -l`、拼接或编码出来的路径、执行脚本文件（不是内联代码）建的链接拦不住；一轮之内这些链接要等轮末核对才删。硬链接不查（Linux `protected_hardlinks` 一般挡住链到不属于自己的文件）。
3. **跨项目读本来就挡不住**：`data/workspaces` 整体对 agent 组可读写，助手知道别的项目 ID 就能直接读；这次修的是「后端替它把工作区外的东西显示到前端」与读密钥、`/proc` 这类 agent 本身读不到的东西。按项目隔离 agent 在 P4。
4. **后端进程内跑的 core 脚本**（反查 `query.py`、知识检查等）仍以后端用户运行：`load_ontology` 已不读链接，其余脚本里的读取没逐个审；改成用 agent 用户跑子进程是根治，没做。
5. **资料分析回执的指纹**：回执里 `resource/` 下的资料经副本里的 `resource` 链接读（后端建的、在白名单里）；助手把这个链接换掉指到别处时，轮末核对只删指到根外的「白名单外」链接，不校验白名单链接的目标。
6. **V-03 下载**按批读 blob，单批仍有 60 秒超时（超时回 503，不再给空 zip）；上限是未压缩 256 MB。
7. **ST-09** 名称按 UTF-8 200 字节限长（约 66 个汉字）；`NAME_MAX` 80 字仍是字符上限，中文名超过字节上限回 400 `too_long`。

## 2026-11-02 · I1 合并（I1 合进交付分支）

做法见 decisions-log「I1 合并」。

1. **别的对话里的建议条**：运行开始后只出现运行条，那个对话的建议条要等下一次事件（一轮结束、确认生效等）才换成运行控制；FL-02 重新取是对的。
2. **FL-02 在运行对话结束后**按流程给下一步，「去复核」只在运行结束时推送的那份建议条里（刷新后从历史里取的是推送的那份，不受影响）。
3. **B5b 的 4 处「需要改设计」（R25～R28）仍等用户定**：开始弹窗里没有「去确认」入口、运行中别的对话输入框照常可用、没有自动决定的段一直显示「自主完成」、门禁没过的挂起没有「重新做这一段」。
4. **自主构建只用假引擎冒烟**（`本机临时目录`：3 段蓝图、一个挂起、一个低置信假设、暂停续跑、按段撤回）；真实模型下按注入约定写置信度与推荐项、步骤结束释放命令锁，要 V 阶段验证（gaps B5b 第 1 条）。
5. **端到端脚本**（`本机临时目录`）最后一项原来断言「SY-17 不在时不出限制卡」，B5b 合回后改成「限制卡按 SY-17 出、不出本期不生效的项」。

## 2026-11-03 · V（真实模型功能验证，只验证内测引擎）

分支 `feat/onto-modeling-plugin-v`（起点 1906b6ee），结论与证据见 `v-report.md`。这里只记没修、要后续接手的。

1. **平台默认配额不够一次资料分析**：默认每人每天 200 万 token、每项目每月 2000 万；内测引擎 + glm-5.2 一次资料分析就用 300～500 万（每次迭代重发整段上下文）。验证时三套栈都调大了，默认值待定。
2. **命令文档让助手把内部步骤说给用户**：「现在执行 Step 2.5：固化只读来源快照」「锁检查通过」、收尾写旧版的「已保存版本 N，如需回退回复"回退"」。要改命令文档的输出约定（USER_VOCABULARY 没管住）。
3. **首轮资料分析的「新增ID确认」**：命令文档（有明确默认的直接处理）与子代理文档（需真实人工确认）说法不一致，真实模型两种都做过。
4. **验收回执能被助手改写**：`.ontology-state/research-review.json` 在助手可写的目录里，检查器误报时助手写 wrapper 把它改回「已通过」。后端展示用自己跑的检查器，但提交钩子与续跑判断读回执。
5. **自主构建里能力包跟随升级一直被推迟**（子线 b）：每段先占变更集，跟随升级要等「没有未确认修改」，旧项目在自主构建里用不上新 core。
6. **助手常不释放命令锁**：陪伴建模 5 个命令有 3 个结束时锁还在；53553af4 让建议条主推「继续」兜底，根治要么改命令文档的收尾、要么平台在这一轮正常结束且修改已确认后代为释放。
7. **出错（引擎退出、模型流超时）后的建议条是空的**：只有用户打断时才主推「从打断处继续」；续跑卡上有按钮，不挡路。
8. **节点包没有单独的「撤回」**（子线 c）：生效后只有停用；节点上显示包名不是清单里的名字；beta 发布的包详情显示 stable。
9. **管理员审核提名接口 404、行业包示例项目用不了**：同 gaps 既有条目（AD-08～10 没画入口）。
10. **R-02 修订资料不发起增量更新**：`is_revision`、`batch_id` 收下不处理（同 F2 第 26 条），资料区的「修订资料」标签与入口不会出现。
11. **增量更新的确认被对话里的决策卡绕过**：内测引擎下 `/ontology-update` 第 3 步用 ask_user 确认并当轮执行，页面清单弹窗与「确认清单并执行」用不上；没带参考资料时清单「出处」全显示「手动补充」；回归只有检查、没有回归场景（同 B4b 第 4 条）。
12. **校验的修复把上游标成过期**：建模、指标、全量回归都过期，建议条主推重跑操作提取。
13. **自主构建（子线 b）**：预算用满要做完当前一段才停（一段 500 万～1200 万 token）；「一律挂起」按全文关键词匹配；模板自带的确认问题在自主构建里进台账；模型流超时重试后第 3 次请求挂了 32 分钟（引擎侧没有帧超时）。
14. **历史卡片**：611c4f05 之前生成的「这一轮改了什么」卡仍是「修改 修改「…」」（卡片随历史保存，不回改）。

## 2026-11-03 · F 第一批（V 修复审查 3 条 + V 未修里不需拍板的项）

已修的见 README 进度记录与 decisions-log「F 第一批」。这里只记没修、要后续接手的：

1. **旧项目没有 `lock_waits`**：锁定旧 core 的项目在跟随升级到新 core 之前，平台不代为释放锁，只在建议条上主推「继续」（53553af4 的兜底照旧）；命令文档的新约定也要跟随升级后才生效。
2. **跳步守卫的「回复 X（/命令）」没改**：extract-object / extract-operation 模式 A（两个来源都没有）、query、impact 的前置不满足时仍列「回复 资料分析（/ontology-research）…」。这时锁已放，属于跳步守卫格式，留着。
3. **validate 的 A4c**：A4b 写已应用的修复直接保留，A4c 又在处理用户回复，前后矛盾，没加 `confirm`（等回复时锁仍在 `start`，平台不会代为释放：这一步还没保存过）。
4. **历史卡片与旧对话**：F 之前生成的助手消息里的「已保存版本 N / 回复回退」不回改。
5. **稳定引擎**：命令文档、工作区规则（CLAUDE.md）两种引擎共用；内测引擎的系统提示另加了一句。稳定引擎下的表现没验证（V 起只验证内测引擎）。
6. **工具调用之间的过程话没管住**（真实模型抽查）：glm-5.2 在工作区规则与内测引擎系统提示都写明之后，资料分析、对象提取开头仍写「先做前置检查」「获取执行锁」「固化来源快照」「派遣资料分析子任务」，偶尔带脚本名（`source_extract.py`）；收尾已经干净（先放锁、两三句小结、指向界面上的「确认蓝图」，没有「已保存版本 N / 回复回退」和下一步菜单）。再往下靠提示词收效有限，见「需要改设计」：命令执行中的过程话收进「工作过程」组。
7. **助手副本里用文件工具读 `resource/` 失败**（真实模型抽查，F 没改）：资料分析开头助手的 read_file / list_files 读 `resource/` 报错（openjiuwen sys_operation「Failed to execute read_file」），助手说「resource/ 目录在沙箱限制之外」，随后改用 bash 跑 `source_extract.py` 读到了。疑似内测引擎文件沙箱的根只有副本、私有工件区、装配目录，副本里的 `resource` 是指向主线工作区的链接，按解析后的路径判越界。没定位到确切原因，不在本批范围，建议下一批确认后把主线的 `resource/` 加进只读沙箱根。

## 2026-11-03 · F2-nav（第一次切换页面白屏、插件安装入口）

1. **首屏后几秒内连续点**：预取和点击抢带宽，改后不白屏，但各页出现的时间和改前相近（Fast 3G 下 0.6～2.4 秒，改前 0.7～4.3 秒且这段时间是白屏）；停 3 秒以后再点全部即时（≤ 20 ms）。可以在换页 pending 时暂停排下一个预取，这次没做。
2. **流程图编辑器（`/drawio`，vendor 静态部署）没压缩、没长缓存**：文件名不带哈希，不能用 immutable；第一次打开编辑器在慢网下仍慢。要做的话在 build_release.sh 供应 webapp 时预压缩，后端同一个类挂上即可。
3. **接口 JSON 不压缩**：这次只管静态资源。
4. **HTTP/1.1 下长连接占位**：项目里项目流、对话流各占一个连接（Chrome 同源 6 个），剩下的给代码块和接口。后端直接对外（没有反向代理）时，前面加 HTTP/2 反向代理可缓解（反代不要再压缩、不要改两种缓存头，README 已写）。
5. **助手推荐（6 个安装入口之一）后端没实现**：PL-01 的 `recommendations` 恒为空，对话里不出 `pack_recommendation` 卡（前端卡片已有，这次只让它带上当前项目）。要先定推荐规则（设计稿的例子「资料里有表结构 → 系统逆向建模」依赖证据源，本期不开放）。
6. **普通用户没有装入平台的入口**（产品定只有管理员）。用户说的「安装」如果指的是这个，要决定是否允许普通用户上传、管理员审核。
7. **没有项目时插件中心每张卡都写「先建一个项目才能启用」**（管理员常见，9 张卡 9 次）。属于设计，见回报「需要改设计」。
8. **测试部署没验证**：测试部署只读，没部署；限速对比是本机后端 + Playwright（Fast 3G）。合入并重新部署后请用户复测。

## 2026-11-03 · F2-chat（子代理进展、对话页展示与字体、对话里切换构建方式）

1. **子代理名称常是「子任务」**：内测引擎的 task 工具多数只带一整段提示词、没有简短的 description，界面行名退成「子任务」（步骤与当前一步都有）。要有名字得让助手派发时带短描述（命令文档或工具说明），这次没改。
2. **思考片段不落盘**：「思考中」展开看到的思考只在这一次模型调用里有，刷新页面、看历史时没有（旧版历史里有折叠的思考过程）。
3. **过程话按规则判**：≤160 字、没有清单表格标题代码角标、同一条回复里有工具调用。偶尔有带清单的过程说明会留在正文；稳定引擎这次没用真实模型验证（规则对两种引擎一样）。
4. **助手读 `resource/` 的文件工具报错**（F 第 7 条已记）：这次真实模型里主助手 Read、Glob 照样出错，工作过程里显示「出错」，改用 bash 读到。不是界面问题。
5. **测试部署没验证**：测试部署只读。ks-vc 用本分支重建后真实模型验证过（截图 `本机临时目录`）；合入并重新部署后请用户复测。
6. **第 8 条只在 mock 与 ks-vc 走到预览前**：真实后端验了「没有蓝图只改默认」「蓝图草案先确认」（取消，没动 V 的项目），开始自主构建、运行中转为陪伴只在 mock 里验（真实运行要跑一段，额度留着）。
7. **旧版对照是回放**：旧前端接最小假后端，喂同一段录下的宿主记录（工具参数只剩摘要，按摘要还原），不是旧后端整栈。

## 2026-11-03 · F2-engine

1. **开始前的自动决定不进台账、不进复核**：自主构建项目在运行开始前的资料分析里策略代答的决定，只在对话里留「已自动决定」只读行。要进复核得先定没有段的条目改判怎么重做（见「需要主控决定」）。
2. **开始前不挂起**：「一律挂起」的类型、「低于阈值挂起」在资料分析里都按保守项记为假设。
3. **资料分析把拿不准的写进蓝图「待回答」**：真实模型下策略代答之后，助手仍把 3 个口径问题写进蓝图问题，进了待处理（不在对话里，符合原则；确认蓝图不挡）。
4. **产物确认按选项文字认**：「确认」类 + 「修改 / 调整…」类两个选项才认；写成三个以上选项或别的说法（如「继续 / 先做流程图 / 取消」）照普通问题进台账。
5. **稳定引擎的上下文用量仍只在一轮结束时报**（没验证，用户这次只用内测引擎）。
6. **存量项目资料区里的图例不动**：资料分析照旧会读到它、出决策卡、算进「不在范围」（测试部署上 用户的测试项目 第一问就提到「1 个 drawio」）。
7. **第 9 条依赖 openjiuwen 0.1.20 的内部方法**：`FsOperation._resolve_path`、`ShellOperation._check_shell_sandbox`、`_extract_abs_paths`、`_get_sandbox_roots`；升级框架时 `test_f2_jiuwen_resource_read.py` 会先报。glob、list 返回的是主线资料区的绝对路径（拿去写照旧被拒）。
8. **文字里的模板确认**（真实模型）：运行里助手没再用 ask_user 问「确认无误请回复确认」，但把这句写进了正文，接着写「用户回复"继续"，视为确认」；还有「已保存版本 6，回复"回退"」——验证镜像没带 F1 的命令文档输出约定，合并后应消失，合并后再看一次。

## 2026-11-03 · F3（去掉确认生效、检查只提示、资料分析停在蓝图、「片」改叫「段」）

1. ~~**插件声明的提交门禁只在助手合入里改成提示**~~ **已解决（F3b）**：主控定一律只提示。写入事务的提交门禁、产物编辑器的保存门禁、脚本节点的输出都只提示（AR-05 / AR-06 响应的 `warnings`、FL-05 的 `run.warnings`），字段定义不合法照旧 422；发布门禁链照拦。
2. **MCP 删除仍被引用的资源（`RESOURCE_REFERENCED`）照旧拒绝**：主控定保持（防误删，不是质量检查）。
3. **Studio 「模板外的章节」「该是表格却不是表格」仍转助手复核**：是否算「解析不了的格式错误」拿不准，保持原样；链接、主键已改成只提示。主控定保持（F3b）。
4. ~~**内置包版本后缀的内容摘要不含清单**~~ **已解决（F3b）**：摘要算清单（去掉版本号）加全部文件，只改清单也换一个版本；所有内置包一次性换版本号，跟随 stable 的项目在下一条消息前升上去、装配照常；codegen、evo 的手工补丁号退回 0.9.2、0.3.2。
5. **跨几轮的命令每轮都存一个版本**：命令中途用文字问用户、一轮结束时就合入；段、步骤的「确认过」按执行记录收尾时主线的状态记，中途被打断的那一轮也会记一次（建议条另有「从打断处继续」）。版本页上会多出几个版本。
6. **检查只提示后，主线上会带着问题**：靠卡片提示、待建计数、发布门禁和撤回这一轮兜底；警告不上卡，只在「查看改动」的明细里。
7. **流程走向问题的识别靠字面**：问题里「接下来 / 下一步 / 接着 / 然后 / 从哪……建、做、提取、开始、进入、画、校验」，或至少两个选项是流程动作（提取对象、画流程图、确认蓝图……）。业务流程里的「下一步由谁审批」不会命中（要带建模动作），但措辞变化大时可能漏掉或误伤；只在资料分析里生效。
8. **前端只去掉了历史里「确认生效」的建议，没有去掉升级前那张卡上的旧字段**：旧卡（`pending_confirm`）在升级迁移里会被更新成已生效；迁移前就被删掉对话的不更新，前端按「未生效」显示、没有按钮。
9. **mock 里的增量更新、批量推演按新流程改了，版本页 mock 的历史说明还有「助手：…」这种改写过的文字**：只影响开发用的 mock。
10. **仓库其他目录里的「片」**（`modeling/` 下别的应用、`consumer/`）不在本次范围（不是 onto-modeling），没改；onto-modeling 里剩下的「片」都是卡片、图片、片段、片区、片刻、芯片、照片、分片拼接，以及引用旧叫法的地方（契约叫法表、详设修订记录）。

## 2026-11-03 · F4a（连接器网关第一版：文档转换、资料的文本版、「数据接入」）

1. **第三方转换器的安装没做**（用户正在定安装模型）：PL-04、PL-19 仍回 501；只有随网关镜像交付的三个官方转换器。ADR-0073 列为待定项（不能有主容器往网关放代码的通道，倾向网关自己验签）。
2. **资料分析还没改成只读 md、助手说明里还没提 `resource/text/`**：属于 F4b。现在文本版已经在资料区，助手按「只读 .md」能找到（`resource/**/*.md`），但命令和工作区规则没指路。出处显示「第 3 页」也在 F4b：标记已在文本版里，`app.evidence.convert.location_at` 能按行号取位置。
3. **签票据的私钥在后端进程里**（详设 5.8.9 的缺口）：密钥文件在 0700 的密钥目录，助手读不到文件，但能读后端内存的进程内引擎理论上拿得到。网关的 jti 防重放只在内存里，网关重启后 10 分钟内的旧票据可以重放（前提是截获到票据）。
4. **共用测试镜像 `onto-test:py311` 没有转换库**：`test_f4a_converters.py`（11 条）和网关测试里用真转换器的 1 条在它上面会跳过；本任务用 `onto-test:f4a`（= py311 + `backend/connector-requirements.txt`）跑的全量。合并后请把 `_plan/test-image` 加上这个文件。内存上限那条只在 Linux 上跑（macOS 不支持 RLIMIT_AS）。
5. **上传一份 PDF 出两个版本**（上传一次、转换一次）：转换结果必须经写入路径；同步转换要让上传等不可信文件解析完。版本历史里转换那条来源是「插件」。
6. **Word 没有页码**：docx 里没有分页信息，文本版只按标题分节；出处只能到段落。
7. **加了用户密码的 PDF** 判「暂不支持」：代码按 pdfminer 的 `PDFPasswordIncorrect` 处理，测试里没有生成加密 PDF 的办法，这条没有用例。
8. **混合扫描件**：只转有文字层的页，图片页在文本里标「这一页是图片，没有转」，说明里列页码；没有 OCR。
9. **升级迁移一次排很多**：已有项目的 PDF、Word、Excel 在启动后后台排队，两个转换线程依次转；资料多的部署第一次启动后要等一阵，期间这些资料显示「转换中」。
10. **测试部署的远端部署脚本没改**（测试部署只读）：网关靠 compose 的 `pull_policy: build` 随整栈构建，远端的镜像冒烟（非 root 能读代码）不覆盖网关镜像；网关镜像里代码权限按构建上下文（deploy.sh 已用 umask 022）。
11. **离线包构建脚本改了但没实跑 `build.sh`**：网关目标单独 `docker build --target connector-gateway` 本机构建、`docker compose up --build` 起整栈都验证过。

## 2026-11-03 · F4b-research（蓝图目标、资料分析只读文本、编号不问人）

1. **内测引擎的读文件工具仍能在进程内解析 PDF、Word**：pdfplumber、python-docx 是内测引擎库的依赖，主服务镜像里可以直接 import；引擎自带的读文件工具遇到 PDF 会在进程内解析。资料分析的说明已不让助手这样读，但没有硬拦。建议在引擎的文件工具守卫上拦截 `resource/` 下有文本版的原件，回提示指向文本版（是否做待定）。
2. **schema 表头仍读 Excel 原件**：对象提取、字段核对用的 `table_profile` 读 `purpose=schema` 的 xlsx 原件（openpyxl），不在本任务范围；要做到「只读文本」需要改成读文本版里的 Markdown 表。
3. **共用的锁恢复提问没改**：各命令 Step 0 遇到命令锁时让用户选「续执 / 清理重来 / 取消」，属于内部机制，但 `STEP0_TEMPLATE.md` 所有命令共用，本任务没动。

## 2026-11-03 · F4b-paths（界面上不出工作区路径和文件名）

1. **F4b-research 管的文件没动**（避免冲突）：资料分析问题的正文 `text_md` 里直接写的资料路径（`research_view.py` 原样下发，报告页「资料分析」、资料地图弹窗、蓝图问题标题会露 `resource/…#p:3`）、资料地图与 `cards-materials.jsx` 的资料名带扩展名和路径悬浮、`anchor_label` 保留编号前缀。建议 F4b-research 合入后在 `research_view` 下发前用 `workspace_labels.humanize` / `material_name`。
2. **Studio 证据锚点仍要手填**：EntryEditor 的锚点输入框占位写「file:resource/…#p:7.3」，用户要自己写路径。要改成「选资料 + 位置」的选择器，属于改设计（见回报）。
3. **还会露文件名的低频处**：导入页的阻断原因（「缺少 ontology/ 目录」）、求解契约侧栏的文件角色名与检查信息、发布门禁明细里的条目路径、授权卡「查看技术细节」里的 JSON。均为开发者或包作者向，本次未改。
4. **对话里已有的历史卡片、气泡不回填**：只影响新产生的；旧记录里的文件名照旧。

## 2026-11-03 · F4c（主容器里不解析资料原件、锁恢复不问人、资料分析不露路径）

1. **后端还有四处在主容器里解析原件**：资料页的 Excel 预览（`excel_preview`）、上传时的用途识别（`material_detect`）、出处解析遇到旧格式锚点（`#p:2`、`#sheet:…`）时的抽取（`material_text.extract`）、评测移出的启动迁移读旧评测上传表的表头（`eval_retirement`，一次性，下个大版本删）。前三处建议改成读文本版。
2. **守卫防不住有意绕过**：比如先把原件复制改名再解析。和写守卫一样，要靠按项目隔离 agent 的进程（P4）。
3. **跳步守卫仍在问人**：STEP0 第 2 步缺前置时让用户选「继续 / 先做…」，属于「下一步做什么」，本任务没改。

## 2026-11-05 · F13 前端（证据页对接程序逆向）

1. **取消上传不释放配额**：EV-31～EV-33 没有删除上传的接口，前端取消只是中止请求；没传完的上传要等 24 小时自动清，期间占每人 2 个的名额。连取消两次就要等。建议加 `DELETE /api/v1/evidence/uploads/{upload_id}`（上传人），前端取消时调。
2. **插件中心的提供方卡片还叫 `rea-binary`**：mock 的 `plugins-data.js` 由系统逆向建模包子任务在改，本任务没动；证据页的提供方已改成 `rea-static`，两边要对齐。
