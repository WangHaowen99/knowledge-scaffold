# pi spike 结论

> 2026-07-08 · 验证包：`@earendil-works/pi-agent-core@0.80.3` + `@earendil-works/pi-ai@0.80.3`（TS/Node，MIT）
> 目的：验证 design.md §9 列出的四个关键假设，决定 workflow 引擎与 pi 的集成方式。
> 结论：**四项全部通过，设计文档的集成方案成立，可进入 M1。** spike 代码用完即弃，本文件是留存的答案。

## 结果

| 假设 | 结论 | 依据 |
| --- | --- | --- |
| Q1 自定义工具注册与执行 | ✅ PASS | `AgentTool { name, label, description, parameters(typebox schema), execute(id, params, signal, onUpdate) }`；实测注册 `read_data_dict / ask_user / write_package_file` 三个工具，agent 均正确调用并产生副作用 |
| Q2 状态序列化 / 恢复继续 | ✅ PASS | `agent.state.messages`（`AgentMessage[]`）是纯数据，直接 `JSON.stringify` 存盘（实测 2.7KB）→ 销毁实例 → `new Agent({ initialState: { messages, systemPrompt, model, tools } })` 从盘恢复 → 续跑正常，历史 5 条增长到 9 条 |
| Q3 OpenAI-compatible 接入 | ✅ PASS（类型 + 内置佐证） | pi-ai 导出 `openai-completions` 的 `streamSimple` 与 `createProvider({ baseUrl, auth, api, models })` 工厂；内置几十个走 `openai-completions` 的 provider（deepseek/nvidia/groq/together/openrouter…），接任意兼容端点是一等公民。未联网跑真 key（避免消耗，能力已充分佐证） |
| Q4 人机交互中断建模 | ✅ PASS | 工具 `execute` 返回 `{ content, details, terminate: true }` 让 agent 在访谈处**自然停止**（无需挂起 Promise）；停止时 transcript 完整、可序列化；专家答案作为一条 `user` 消息注入后 `prompt()`/`continue()` 续跑，答案进入上下文并影响后续（实测恢复后据答案写出 `objects/STRUCTURE_LIST.md`） |

## 对设计的确认与修正

- **访谈挂起模式定案**：采用 **`terminate` 模式**（工具结果 terminate → agent 停 → 存 `state.messages` → 前端应答 → 注入 user 消息 → 续跑），而非"挂起 Promise"。优点：无常驻内存、无进程内等待，天然支持"关浏览器后恢复"，与 design.md §2「waiting_user 时序列化 agent 状态」一致——**序列化对象就是 `state.messages` 数组**。
- **持久化落点确认**：workflow 引擎无需 pi 的高层 `Session`/`jsonl-repo`；每个 agent 型节点只需把 `{ systemPrompt, messages, model 标识, 工具集标识 }` 存进 SQLite `wf_nodes` 的产物字段即可恢复。pi 的 Session/session-repo 作为可选增强，首版不用。
- **LLM 接入代码形态**（Q3，供 M1 实现）：
  ```ts
  import { createProvider } from '@earendil-works/pi-ai';
  import { streamSimple } from '@earendil-works/pi-ai/api/openai-completions';
  const provider = createProvider({
    id: 'modeling-llm', baseUrl: process.env.MODELING_LLM_BASE_URL,
    auth: { /* bearer: MODELING_LLM_API_KEY */ },
    api: streamSimple, models: [{ id: process.env.MODELING_LLM_MODEL }],
  });
  ```
  确切的 `auth`/model 元数据字段在 M1 接真实端点时按 `openai.d.ts` 内置 provider 抄一份即可。

## 关键 API 速查（供 M1）

- 构造：`new Agent({ streamFn, initialState: { model, systemPrompt, tools, messages } })`
- 驱动：`await agent.prompt(text | AgentMessage[])`；续跑 `await agent.continue()`（末条须为 user / toolResult）；`await agent.waitForIdle()`
- 事件：`agent.subscribe((event, signal) => …)`（`agent_end` 为终态）；工具级钩子 `beforeToolCall`（可 `{ block:true }`）/`afterToolCall`
- 快照：读 `agent.state.messages`；恢复 = 用它作 `initialState.messages`
- 中断：工具结果 `terminate:true`（整个 batch 都 terminate 才提前停）
- 测试底座：`createFauxCore({ provider, models })` → `{ streamSimple, getModel, setResponses([...]) }`，配 `fauxAssistantMessage` / `fauxToolCall` 编排逐步响应

## 遗留 / 下一步

- **未联网验证真实端点**：M1 接入 `MODELING_LLM_*` 时补一次真 key 冒烟（含 token/时延量级，即 design.md §9 的 Q4-cost 项）。
- **Node 版本**：本机 Node 25 通过；生产镜像锁 LTS（20/22）再复测一次 ESM 子路径导入（`@earendil-works/pi-ai/providers/faux`、`/api/openai-completions` 均走 exports 子路径）。
- **多工具并发 / executionMode**：数据探查类工具可能并发跑 SQL，`AgentTool.executionMode: 'sequential'|'parallel'` 已提供，M2 数据探查节点按需设置。
