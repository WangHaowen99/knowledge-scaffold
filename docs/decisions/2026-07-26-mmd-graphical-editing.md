# 点击 .mmd 走图形化编辑：复用流程图编辑器而非引入 mermaid

## Context

知识包里的 `diagrams/process_N.mmd` 在文件树中点开后只能看到 mermaid 原始文本，无法看图、更无法改图。
需求是点击 `.mmd` 直接渲染出图，并支持增删改节点与边、拖拽交互。

关键事实：`renderDiagramFiles()`（`modeling/src/flowchart/projection.ts`）在同一次写入中产出三个文件——
`diagrams/index.md`、`diagrams/processes.json` 和各 `diagrams/process_N.mmd`。
其中 `processes.json` 是完整的 `FlowchartDocument`，`.mmd` 只是它的单向派生产物。
而 `modeling/frontend/src/flowchart-editor/` 中已有一个基于 React Flow 12 的完整编辑器，
编辑的正是 `FlowchartDocument`，且是纯 props 驱动、reducer 无副作用。

## Decision

点击 `process_N.mmd` 时不解析 mermaid 文本，而是读取同级 `processes.json`，
取第 N 张图交给既有的 `FlowchartEditor` 渲染和编辑。

保存新增 `PUT /api/projects/:id/package/diagram`：接收整份文档，服务端做
`normalizeFlowchartDocument` + `validateFlowchartDocument` 校验，再用 `renderDiagramFiles()`
一次性原子重写全部派生文件，并清理图数量减少后残留的 `process_N.mmd`。

缺少 `processes.json`、JSON 损坏、或 `.mmd` 序号在文档中不存在时，降级回原有的原文视图并说明原因。

## Alternatives considered

- **引入 `mermaid` 库渲染**：能显示图但不能编辑，且与 React Flow 两套渲染观感不统一，还多一个依赖。
- **写 `.mmd` 解析器反向生成文档**：可覆盖手写 `.mmd`，但 mermaid 语法面广（subgraph、样式、多种箭头与形状），
  往返保真度难保证，工作量与出错风险都显著上升。既然结构化源文档就在同目录，没有理由绕远路。
- **纯前端复用 `PUT /package/file`**：需把 `renderMermaid` 投影逻辑在前端 JS 再实现一份（易漂移），
  且多次写入非原子，中途失败会让 `.mmd` 与 `processes.json` 不一致。

## Consequences

- 未新增任何依赖；渲染质量与交互灵敏度直接继承既有生产编辑器。
- `.mmd` 明确定位为只读派生产物，界面上对流程图只提供「图形 / 源码」切换，不再允许直接改 mermaid 文本
  （改了也会在下次保存时被覆盖）。
- 编辑器编辑的是整份文档，因此保存会一并重写所有图；未打开的图原样保留。
- 手写的、没有配套 `processes.json` 的 `.mmd` 不支持图形编辑。若之后确有此需求，再单独评估解析器。
- `createFlowchartEditorSession` 新增可选 `activeDiagramId`，`FlowchartEditor` 新增 `initialDiagramId` prop，
  默认行为不变（仍停在第一张图）。
