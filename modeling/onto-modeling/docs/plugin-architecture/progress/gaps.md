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

视觉对照方法：Playwright 1440×900，设计稿 `file://…html#/<页面>` 与本地 mock 开发服务器同一状态各截一张，逐像素比（阈值 RGB 差 > 40），脚本在 `/tmp/ks-fe/`。

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
6. **还没接的弹窗和页面**（F2）：流程图编辑弹窗、待处理、资料分析 / 切片规划、增量更新、自主构建、Studio、资料、发布、插件页、项目设置、全局设置页先显示「后续版本接入」。对话里的产物卡双击会跳过去。
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

1. **契约里依赖以后阶段能力的字段给空值**：切片、自主构建、变更集、写入范围锁（`lock` 恒为 null）、执行记录、页面命令任务（`command_task`/`reread_task`/`review_task` 恒为 null）、证据、包——见 `contract-changes.md` B2 各条。随 B3、B4 补。
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
3. **流程运行时只做到 P1**：FL-03～FL-05（跳过、恢复、开始节点）、8 种节点状态与按切片的进度、完整的下一步优先级、`state_helper suggest`、`flow.lock` 与节点挂载都在 P2 / P3。`post_commit_hook.sh` 按提交前缀给的提示仍在（P2 随宿主注入退役）。
4. **受保护目录第 3、4 层不完整**：提交检查已拦暂存的 `resource/`、`.claude`、`.onto`；合入时的检查要等统一变更事务（C1）与变更集（C8）；第 4 层进程隔离（agent 改不了）要等密钥移出（E10）与按项目隔离（P4）。`git add -A && git commit` 写在一条命令里时提交检查看到的是旧暂存区，方案放在提交接口（C11，P2），这次没动。
5. **备份范围不含包仓库**：`services/backup.py`（运维面，B3b 范围）没加 `packs/`；异机恢复后锁定了上传版本的项目会退回 stable。需要 B3b 在备份范围里加 `data/packs/`（`assembled/` 可重建，不用备）。O-32 回滚前的包仓库备份已有（`pack-repo-backups/`）。
6. **写入路径还用镜像版本的契约**：`workspace/files.py` 的 `write_file` 同步脚本描述符仍用镜像版本的 `script_descriptor`（写入路径是 B3b 的范围）；B3b 的 ChangeTx 里改用 `contracts.for_project(root).script_descriptor`，锁文件提交也改走 ChangeTx。
7. **core 清单要跟上 B3b 新增的脚本**：`assets/workspace-toolkit/manifest.yaml` 的 `contributes.scripts` 要加 B3b 的 `onto_metamodel.py` 等新脚本（漏了只会在静态检查里给「未声明」警告，不影响装配）。
8. **不再用的装配目录不清理**：`assembled/` 只增不减（每个不同的锁组合一份）；P2 加引用计数清理。
9. **前端**：`LeftPane`、`ChatPane` 改读 `GET /flow`，记在 `frontend-progress.md`。

## 2026-11-01 · F2 前端（Studio、报告、流程图、待处理、自主构建、资料分析、建模蓝图）

视觉对照方法同 F1（Playwright 1440×900，阈值 RGB 差 > 40，脚本在 `/tmp/ks-f2/`）。示例换成合成场景后，数据文字和设计稿不同属于预期差异，各页的差异比例与原因见前端线 F2 报告。

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

视觉对照方法同 F1、F2（Playwright 1440×900，阈值 RGB 差 > 40，脚本在 `/tmp/ks-f3/`）。数据文字与设计稿不同属于预期差异，各页的差异比例见 `frontend-progress.md` F3 一节。

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
   - 统一下一步（`suggest`）的输入：节点与切片状态、写入范围锁。现在只按流程和执行记录算。
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

视觉对照方法同 F1～F3（Playwright 1440×900，阈值 RGB 差 > 40，脚本在 `/tmp/ks-f4/`）。各页差异比例见 `frontend-progress.md` F4 一节。

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
