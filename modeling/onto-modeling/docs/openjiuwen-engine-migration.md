# openJiuwen 引擎切换方案（Claude Agent SDK → openjiuwen DeepAgent）

状态：方案待拍板（2026-09-20 起草）。拍板后补 ADR 到 `docs/decisions/`。

## 0. 背景与动机

当前 agent 引擎为 Claude Agent SDK：每会话拉起一个捆绑 CLI 子进程（Node/Bun，实测 ~400MB），
模型侧只讲 Anthropic 协议（需网关）。两个诉求驱动本次评估：

1. **降内存**：8G 机器安全并发仅 12–15 会话，扩容靠加内存；
2. **模型解绑**：直连 OpenAI 兼容协议的国产模型（Qwen / DeepSeek / GLM），去掉协议网关依赖。

选型结论（对比 Pi、Codex CLI 后）：openjiuwen agent-core（`pip install openjiuwen`，Apache-2.0，
Python 3.11–3.13）的 DeepAgent 与本项目依赖面对位最好——

| 本项目依赖 | openjiuwen 对应物 |
|---|---|
| Task 派遣子代理（18 个专职 agent） | `task_tool` + `subagent_spawn/wait/resume` 运行时 |
| AskUserQuestion（请专家确认卡） | 内置 `ask_user` 工具 |
| `can_use_tool` 权限回调 | Rails：`before_tool_call` 拦截 / interrupt rail / tool_approval_rail |
| resume / 会话持久化 | session_store + checkpointing（框架核心特性） |
| 工具面（bash/文件/搜索/todo/web） | 齐备，另有 LSP、worktree、vision 等超集 |
| MCP（stdio / 对外暴露） | 官方支持 |
| CLAUDE.md / commands / agents 资产 | workspace 规则 + skills 库 + subagent 配置（格式不同，需迁移） |
| 模型协议 | OpenAI 兼容 `api_base/api_key` 原生直连 |

**决定性差异：openjiuwen 是 Python 进程内库，不是子进程 CLI。** 会话从"一个 400MB Node 进程"
变成同一 Python 进程里的上下文对象；后端本就是 FastAPI/Python，import 即用，无 sidecar、无 IPC。

## 1. 目标与非目标

### 目标

- 引擎可切换：Claude / Jiuwen 双引擎并存，**按项目灰度**，随时回切；
- 前端零感知：SSE 事件契约不变，ChatPane/确认卡/子代理卡/变更卡照常工作；
- 内存：8G 机器支撑 ≥30 并发活跃会话（Jiuwen 引擎下）；
- 模型：配置中心直填 OpenAI 兼容 base_url/key/model，免协议网关；
- 部署：Jiuwen 引擎不依赖捆绑 CLI 二进制 → **顺带消除 AVX 预检与降级兼容包整条负担**。

### 非目标

- 不改前端 UI 与交互；
- 不重写确定性脚本（`assets/workspace-toolkit/scripts/` 23 个，经 bash 调用，引擎无关）；
- 不承诺一次性全切：Claude 路径在观察期内保留为回退引擎；
- 不迁移存量会话的模型侧上下文（跨引擎切换 = 模型新对话；界面历史照常回放，见 §5 风险 7）。

## 2. 总体策略

**先抽象、后并存、再灰度**，四条不变量贯穿全程：

1. **事件契约是唯一稳定接口**：`stream_delta / assistant_message / tool_results / result /
   permission_request|resolved / session_state / queue_position / changes / interrupted / ping`
   ——两引擎都归一化到这套事件，前端与事件 JSONL 持久化完全不动；
2. **资产内容单一事实源**：markdown 资产不 fork，两引擎各写装载器（§3.4）；
3. **评测先行于切换**：换 harness 必然连着换模型，质量回归靠最小评测基线守门（§4 Phase 3 前置）；
4. **每阶段可独立回退**：引擎开关是项目级字段，出问题切回 claude 即恢复。

## 3. 架构设计

### 3.1 引擎抽象层（现状收口点 → `AgentEngine` 协议）

当前对 SDK 的耦合全部在 `backend/app/services/agent_sessions.py`（914 行）三处：

- `_create_client` / `_build_options`（约 556–615 行）：`ClaudeAgentOptions(cwd, system_prompt,
  setting_sources, permission_mode, can_use_tool, include_partial_messages, mcp_servers, env,
  resume, cli_path)`；
- `_read_loop` 内的消息归一化（约 790–850 行）：SDK Message → 平台事件；
- resume 元数据：`data/sessions/<pid>.meta.json`。

抽出引擎协议（会话池、TTL 回收、排队、事件 JSONL、SSE 广播全部留在 `SessionManager`，引擎无关）：

```python
class AgentEngine(Protocol):
    async def connect(self, ctx: EngineContext) -> None: ...
    async def send(self, text: str) -> None: ...
    def events(self) -> AsyncIterator[dict]:      # 已归一化的平台事件
        ...
    async def interrupt(self) -> None: ...
    async def disconnect(self) -> None:           # 保留续接点（TTL 回收用）
        ...
    async def close(self) -> None:                # 清除续接点（显式关闭）
        ...

@dataclass
class EngineContext:
    project_id: str
    cwd: Path                                     # 项目工作区（git）
    model_env: dict                               # 引擎自取所需协议的配置
    mcp_servers: dict
    permission_callback: PermissionCallback       # 确认卡回调（两引擎共用语义）
    resume_token: str | None                      # 引擎自定义格式，meta.json 加 engine 字段
```

`ClaudeEngine` = 现有实现平移进接口，行为零变化（Phase 1 的全部内容，用现有 60 个测试守护）。

### 3.2 JiuwenEngine

- **实例形态**：每项目一个 `create_deep_agent(...)` 实例（进程内对象）。活跃 = 对象在跑；
  空闲 = 落 checkpoint 后即可释放对象；恢复 = 从 checkpoint 重建。会话池的"并发上限 12 个进程"
  退化为 **asyncio 信号量（并发轮次上限）**，排队/位次事件机制原样保留但阈值可大幅放宽；
- **事件桥**：openjiuwen loop_event 流 → 平台事件对照表（Phase 2 首件交付物，含
  流式 delta、工具调用/结果、子代理生命周期、result 终态五类映射）；
- **确认卡**：`ask_user` 工具与 tool_approval rail 统一桥到现有
  `permission_request → future 等待 → respond_permission 回填` 机制（`PendingPermission`
  结构复用，超时/孤儿失效/410 语义全部继承）；
- **守卫 hooks → rails**：`settings.json` 的 PreToolUse shell 守卫
  （`pre_commit_check / toolset_write_guard / resource_write_guard / lock_file_guard`）
  改写为 Python rails（`before_tool_call` 判定 block/pass，判定逻辑从 shell 直译，
  脚本本体保留供 Claude 引擎继续用）；`settings.json` 的 permissions 白名单映射到
  security engine 配置；
- **resume**（按 PoC 实测修订）：openjiuwen 的续接形态是**历史回放**——新实例调
  `agent.create_new_context_engine(session_id, messages=历史消息)` 注水上下文引擎
  （UserMessage/AssistantMessage 列表）。历史直接来自我们已有的事件 JSONL，引擎适配层
  做一次事件→消息的映射即可；天然跨进程，且**彻底摆脱容器 HOME 卷上 CLI transcript
  的依赖**（Dockerfile 里为此做的 HOME 重定向可退役）。`meta.json` 扩展为 `{engine, token}`；
- **成本指标**：现取自 Claude result 消息的 usage；Jiuwen 侧挂 token_tracker rail 采集，
  归一化到同一 `result` 事件字段（单轮成本/输入输出 tokens/缓存命中，缓存命中率取决于所接模型
  服务是否回报，取不到则该项显示"—"）；
- **子代理**：我们的命令全部是 `main → 专职 agent` 一层委派（Claude Code 子代理本就不能再派生），
  openjiuwen 当前"仅支持一层委派"的限制**对我们不构成约束**；子代理生命周期事件桥到现有
  子代理卡（运行中/完成/失败三态）。

### 3.3 模型接入

`model_settings.json` 增加 `provider` 维度：

- `anthropic`（现状）：`ANTHROPIC_BASE_URL / API_KEY / AUTH_TOKEN / MODEL`，Claude 引擎消费；
- `openai-compatible`（新增）：`api_base / api_key / model`，Jiuwen 引擎消费
  （`ModelClientConfig` 直传）。

配置中心「模型接入」页按 provider 分组展示；配置指纹热切换机制平移（改配置 → 下一条消息
断开重连生效，无需重启）。

### 3.4 资产迁移（工作量大头）

| 资产 | 数量 | 迁移方式 |
|---|---|---|
| `workspace-rules.md`（工作区 CLAUDE.md） | 1 | Jiuwen system prompt sections 注入，内容共享 |
| `commands/*.md` | 20 | 装载为 Jiuwen skills；斜杠命令语义由后端消息预处理注入（前端命令菜单不变） |
| `agents/*.md` | 18 | 装载为 subagent 配置（frontmatter → factory 参数，正文 → 子代理 system prompt） |
| `scripts/` 确定性脚本 | 23 | **零迁移**（bash 调用，引擎无关） |
| `settings.json` permissions | 1 | 映射 security engine / rails 配置 |
| `settings.json` hooks（4 shell 守卫） | 4 | 直译为 Python rails（§3.2） |
| `templates/`、`USER_VOCABULARY.md` 等 | — | 内容零改（被命令引用，随命令装载） |

**关键设计：写 `JiuwenAssetLoader`（解析现有 markdown 目录结构 → Jiuwen 装配），而不是把资产
转存成第二套格式。** 资产升级通道（设置页上传资产包）对两引擎同时生效，不产生双维护。

措辞调优（提示词为 Claude 行为调过）在 Phase 3 的评测回归里按失分项逐条修，修订同样写回
单一资产源——原则：改动对 Claude 引擎至少无害，评测双跑验证。

### 3.5 部署链变化

- 镜像：新增 openjiuwen wheel 进 `vendor/wheels`；Jiuwen-only 部署可去掉捆绑 CLI（省 ~100MB）、
  `IS_SANDBOX=1`、workspace trusted 登记、`ONTO_WEB_CLI_PATH` 降级包钩子，
  **preflight 的 AVX 检查降级为仅 Claude 引擎需要**；
- 双引擎期两套依赖都在镜像里（体积代价 ~100MB，可接受）；全切后出瘦身版；
- `.env` 新增 OpenAI 兼容配置项；`docker-compose` 无结构变化，数据卷不动。

## 4. 分阶段计划

### Phase 0：PoC（1 人周）——先行，Go/No-Go 门

独立脚本（不动产品代码）：`create_deep_agent` + OpenAI 兼容模型跑通 `/ontology-research` 全链
（读资料 → 联网检索 → ask_user 目标确认 → 产出调研报告 → git 提交）。

验收清单：
1. ask_user 挂起/回填闭环可编程对接（不依赖它的 CLI REPL）；
2. rail 能拦截一次违规写（等价 lock_file_guard 场景）；
3. 中断后从 checkpoint 恢复继续；
4. 单会话峰值内存 <80MB（RSS 增量），10 会话并发曲线线性；
5. 调研报告质量人工评：与 Claude 产出对照可接受。

**No-Go 条件**：质量不可接受、子代理/ask_user 不稳、或 API 形态与文档严重不符
→ 停止本方案，回落「LiteLLM + 即用即弃」路线（该路线独立有效，不受本方案影响）。

### Phase 1：引擎抽象重构（1 人周，行为零变化）——已完成（2026-09-20）

`AgentEngine` 协议 + `ClaudeEngine` 平移。落地形态：

- `services/engines/base.py`：`AgentEngine` 协议、`EngineContext`（权限回调 +
  resume token 存取 + 错误留档，引擎不反向依赖会话层）、平台事件契约文档、
  中立权限决策 dict（`{"behavior": "allow"|"deny", ...}`，SDK 类型不再泄漏到会话层）；
- `services/engines/claude.py`：全部 SDK 耦合迁入（选项构建、消息归一化、错误翻译、
  resume 捕获与失败清点、`config_stale()` 配置热更检测）；测试注入点移至
  `ClaudeEngine._create_client`；
- `agent_sessions.py` 变为引擎无关编排层（池化/排队/TTL/事件持久化/SSE/确认卡
  future），经 `_engine_class()` 选择引擎（Phase 2 起按项目 meta 路由）；
  兼容重导出保留既有调用方与测试的导入路径。

验收结果：60 后端测试全绿；v0.8.2 构建部署（5100）健康；真实 CLI 回归——发消息 →
回复 → result（含成本），回收后重连 `resumed: true` 且上下文找回（暗号测试）。
meta.json 的 engine 字段留到 Phase 2 引擎开关一起做（现仍为单引擎，无歧义）。

### Phase 2：JiuwenEngine 接入（2 人周）——代码完成（2026-09-20），线上验收待批

落地形态：

- `services/engines/jiuwen.py`：进程内 DeepAgent 引擎——`_ChunkBridge` 事件桥
  （llm_output/llm_reasoning→stream_delta+assistant_message、tool_call/result→
  工具事件并自造 id FIFO 配对、`parallel_tool_calls=False` 保证配对正确）；
  ask_user interaction → `AskUserQuestion` 确认卡（questions 契约同形直传），
  审批 interaction → 通用确认卡；答案经 InteractiveInput 回填续跑；
  历史回放续接（`create_new_context_engine` 注水，24k 字符预算从最近取）；
  cwd（项目 git 区）/ workspace（DATA_DIR/jiuwen/<pid> 私有工件区）分层不污染；
  result 事件带 TokenTrackingRail 的轮内 tokens 增量（无单价来源，cost 为 None）；
- 引擎路由：项目 meta `engine` 字段（`_engine_class_for`，无字段回落 claude）；
  `PUT /api/projects/{id}/engine` 切换（写权限，切换即关会话）；
  前端项目卡「更多 → 会话引擎」切换弹窗；
- 模型配置：`model-settings.json` 增 `jiuwen` 段（`PUT /api/settings/model/jiuwen`，
  admin，掩码读取），env 兜底 `ONTO_WEB_JIUWEN_BASE_URL/API_KEY/MODEL`；
  配置中心「模型接入」页分 Anthropic / OpenAI 兼容两组；
- 依赖：requirements 增 openjiuwen（`python_version>=3.11` 标记，本地 venv 已重建为
  3.11 并顺带清理改名遗留软链）；**mcp 由 2.x 降为 1.30**（openjiuwen 的 fastmcp
  约束），`mcp_expose` 加 1.x/2.x 双版本垫片（FastMCP settings 构造 ≙ MCPServer
  streamable_http_app 参数）；镜像含双引擎依赖，体积增至 ~2.05GB。

验证状态（2026-09-21 冒烟通过，Phase 2 完成）：69 后端测试全绿；线上端到端
（v0.9.0 @5100，真实 glm-5.2）——发消息/回复/result 带 tokens；ask_user 弹出
结构化确认卡（header/options/description 完整）→ API 回填 → agent 按所选继续；
回收后重连 `resumed: true` 且历史回放找回上下文（暗号测试）。冒烟发现并修复：
历史回放不再注水尾部未获回复的用户消息（避免模型看两遍当前 prompt、首轮误标已续接）。

### Phase 3：资产装载器 + rails + 质量回归（2–4 人周，弹性最大）——装载层完成（2026-09-21）

已完成（`engines/jiuwen_assets.py`，单一资产事实源，零转存）：

- **JiuwenAssetLoader**：workspace-rules.md + 命令索引 + 运行时适配注记 → system
  prompt；`/命令 参数` 后端展开为命令文档全文（`$ARGUMENTS` 代入，与 CLI 等价）；
  子代理不需预注册——命令资产的既有模式就是"Read agents/*.md 内容作 Task prompt"，
  由 `add_general_purpose_agent=True` 的通用子代理承接；
- **ClaudeHooksRail**：直接执行 settings.json 里 PreToolUse 的 shell 守卫脚本本体
  （同 matcher、同 stdin JSON 协议，deny 输出转 rail 拒绝）——4 个守卫零逻辑复制；
- **沙箱三根**：自建 SysOperation，`sandbox_root = [项目区, 私有工件区, 资产目录]`
  ——工作区 `.claude/CLAUDE.md` 符号链接按 resolve 判定，不加资产根读不穿；
  资产写保护由 toolset_write_guard 承担（沙箱不分读写）。

验证：74 后端测试全绿（新增 5 个：命令展开/系统提示词/守卫脚本真实执行的
拒绝与放行矩阵）；线上（v0.10.0 @5100，jiuwen 引擎 + 完整资产）——`/ontology`
展开执行 state_helper 状态检测并按命令话术输出下一步建议，与 Claude 引擎行为一致；
resource/ 写入被拒（system prompt 规则第一道防线 + hooks rail 硬兜底）。

**剩余**：最小评测基线（Builder 全链脚本化 + Judge 评分）与三命令双跑质量回归
（Claude 基线 vs Jiuwen+glm-5.2，失分项逐条修资产，目标 ≥ 基线 90%）；
web 检索工具接入（research 命令的联网调研在 jiuwen 侧尚不可用，评测需避开或先补）。

### Phase 4：灰度与收尾（观察期 2–4 周）

新项目默认 Jiuwen，存量项目管理端手动切；观察内存/错误留档/用户反馈；
达标后默认全切，Claude 路径降级为兼容选项；出瘦身镜像；本文档转 ADR。

## 5. 风险与应对

| # | 风险 | 应对 |
|---|---|---|
| 1 | **框架成熟度**：subagent 架构在 dev-stable 重构中，已知文件边界/沙箱逃逸 bug | 锁定 openjiuwen 版本进 vendor；PoC 首验子代理；不启用其沙箱模式（我们有容器边界 + 自有 rails）；升级必过评测基线 |
| 2 | **质量回归**：6000 行资产为 Claude 调优，换模型后产出漂移 | Phase 3 评测门 + 失分项逐条修；灰度期新项目先行，存量不动 |
| 3 | ask_user/审批 API 形态与我们确认卡协议有出入 | PoC 验收项 #1；桥接层兜底（最坏情况：ask_user 降级为 rail 拦截 + 平台侧合成提问事件） |
| 4 | 事件流粒度差异（如无 thinking delta、流式分块不同） | 事件桥做缺省降级（无 thinking 则不发该类事件，前端本就容忍缺失） |
| 5 | openjiuwen API 迭代快，小版本破坏兼容 | pin 版本 + vendor wheel 离线化；升级视同资产升级走评测门 |
| 6 | 双引擎期维护两套引擎代码 | 引擎面收口在 Protocol 后各 <500 行；观察期设上限（4 周）强制收敛决策 |
| 7 | 跨引擎切换丢模型上下文（checkpoint 与 CLI transcript 互不相通） | 产品语义定为"切换引擎 = 重启会话"（界面历史照常回放）；切换入口给出明确提示 |
| 8 | 成本/缓存指标依赖模型服务回报，部分网关不给 usage | token_tracker 兜底估算 tokens；给不出的字段显示"—"，不伪造 |

## 6. 验收标准（全量切换前）

1. 功能奇偶清单过检：确认卡（含超时/失效/410）、走查卡、子代理卡、变更卡、打断续接、
   排队位次、成本指标、MCP 工具、资产升级通道；
2. 内存：8G 部署 30 并发活跃会话不 OOM，空闲回落到基线；
3. 评测：标准领域得分 ≥ Claude 基线 90%；
4. 测试：现有 60 后端测试 + 新引擎测试组全绿；
5. 部署：发布包在无 AVX 机器上以 Jiuwen 引擎正常运行（降级包机制可退役的证明）。

## 7. 工作量汇总

| 阶段 | 工作量 | 备注 |
|---|---|---|
| Phase 0 PoC | 1 人周 | Go/No-Go 门 |
| Phase 1 抽象 | 1 人周 | 行为零变化 |
| Phase 2 引擎 | 2 人周 | 前端零改动 |
| Phase 3 资产+评测 | 2–4 人周 | 弹性最大（调优轮次不可精确预估） |
| Phase 4 灰度 | 观察 2–4 周 | 低强度 |
| **合计** | **6–8 人周工程 + 调优弹性** | |

## 8. 待拍板决策

1. ~~PoC 用什么模型~~ **已定（2026-09-20）**：glm-5.2 @ 阿里云百炼 OpenAI 兼容端点
   （与 tc 机 modeling 服务同套配置），Phase 0 实测通过；
2. **质量阈值**：评测得分 ≥ Claude 基线 90% 是否为可接受线；
3. **是否接受"切引擎 = 模型侧新会话"**的产品语义（§5 风险 7）——注：PoC 后此项弱化，
   历史回放续接意味着跨引擎切换也能把界面历史注水成模型上下文，损失小于原预期；
4. 双引擎观察期上限（建议 4 周）与全切判据由谁拍板。

## 9. Phase 0 PoC 结果（2026-09-20）——结论 GO

环境：本机 WSL，`openjiuwen==0.1.18`（PyPI，Python 3.11.15 venv），glm-5.2 @ 百炼；
脚本与产物在 `~/poc-openjiuwen/`（仓库外，凭据运行时经 ssh 管道注入不落盘）。

| # | 验收项 | 结果 | 实测数据 |
|---|---|---|---|
| 1 | ask_user 程序化闭环 | **通过** | 调研链中 agent 弹出结构化提问（问题 + 3 个带标签选项，与确认卡 schema 同形），`InteractiveInput.update(iid, {"answers": ...})` 回填后续跑 |
| 2 | 守卫 rail 拦截 | **通过** | 自定义 `BaseSecurityRail` 子类拦下 locked/ 写入，文件未创建，agent 如实转述被拒原因（lock_file_guard 同语义） |
| 3 | 跨实例续接 | **通过** | 历史回放注水新实例后暗号找回；会话隔离正常；跨进程按构造成立（历史来自我们的 JSONL） |
| 4 | 内存 | **通过（远超目标）** | 运行时基线 220MB 一次性；**空闲会话 ≈3MB、活跃轮次 ≈12MB**（目标 <80MB；对比 Claude CLI ~400MB/会话差两个数量级） |
| 5 | 产出质量 | **通过（待专家复核）** | 调研链 99.7s 收敛：读资料→确认目标→报告 5830 字，16 个字段全映射、规则完整提取、按确认目标组织 |
| 6 | 子代理派遣（风险 #1 首验） | **通过** | `task_tool` 派遣专职提取子代理→产物落盘→协调者回读汇报，52s，一层委派与现有模式同构 |

### 实测发现（迁移时要用）

- **API 坑**：`BaseSecurityRail` 子类必须声明 `supported_events`（默认空集 → 钩子静默不注册）；
- 用量指标随事件流下发（`llm_usage` / `context.usage` chunk），成本桥接不依赖 tracker rail；
- chunk 类型（`llm_output/llm_reasoning/tool_call/tool_result/__interaction__/todo.updated`）
  与平台事件契约可逐一映射，含思考流；
- 框架多处刻意兼容 Claude Code 约定：skills 默认扫 `~/.claude/skills`、MCP 配置文件即
  Claude Code 格式——资产装载器成本低于预期；
- 模型连通 1.1s 首响，直连百炼 OpenAI 兼容端点，无需协议网关。

### PoC 未覆盖（进入 Phase 2/3 验证）

- Web 检索工具（`create_web_tools` 需搜索服务配置，调研链本次仅用本地资料）；
- 长会话上下文增长下的内存曲线与压缩（ContextProcessorRail 未启用）；
- `enable_subagent_runtime` 常驻子代理模式（本次只验了 task_tool 一层派遣）；
- 20 条真实命令资产在 glm-5.2 下的产出质量（Phase 3 评测基线双跑）。
