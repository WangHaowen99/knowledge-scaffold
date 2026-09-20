# 0001 — Web 版架构：Claude Agent SDK 作为宿主运行时

## Context

本体建模工具集原生形态是 Claude Code CLI 上的提示词资产（19 命令 / 17 子代理 /
24 确定性脚本）。需要 Web 化以服务非技术业务用户。已有的 modeling 工作台
（自研 agent 循环）暴露出预算体系、上下文管理、每轮固定成本等结构性问题。

## Decision

新建独立 Web 产品（onto-web），Agent 运行时采用 Python 版 Claude Agent SDK：

- SDK 捆绑 Claude Code CLI（原生二进制，免 Node.js），原生加载 `.claude/`
  提示词资产与 hooks，工具集资产近乎原样复用
- Web 层收缩为三件事：项目/工作区管理（文件 + git）、会话编排 + SSE 流转发、
  交互协议 UI 化（canUseTool → 确认卡）
- 前端 React 19 + antd + @ant-design/x，三栏工作台（左导航 / 中工件 / 右对话），
  色系与 consumer 服务同源

## Alternatives considered

- 复用/扩展现有 modeling 工作台：受既有架构问题拖累，排除
- 自研 agent 循环（Anthropic API + tool use）：重演已知结构性问题，排除
- TypeScript 版 Agent SDK：后端团队肌肉在 Python，且需引入 Node 运行时，排除

## Consequences

- 收益：宿主能力（循环、上下文压缩、权限、hooks、子代理）零自研
- 代价：循环内部黑盒，扩展点限于 hooks / canUseTool / MCP / commands；
  SDK 0.x 版本节奏快，须锁版本并配评测回归
- 部署约束：x86_64 目标机必须暴露 AVX 指令集（捆绑二进制为 Bun 编译），
  预检 + host-passthrough 整改 + ARM 包 + 降级兼容包四级阶梯，详见设计文档
