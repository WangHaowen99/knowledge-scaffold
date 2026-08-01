# 助手动作的语义由后端声明

## Context

建模助手的按钮全部靠 `action.id` 这一个字符串驱动，前端在一条 232 行、37 个分支的
`handleStructuredAction` 里按 id 猜它属于哪一类。28 个 id 实际承载了五种完全不同的语义：

1. 调后端写接口（`confirm_*`、`analyze_materials`…）
2. 打开本地弹窗（`defer_validation_errors`、`open_task_*`）
3. 跨组件导航（`open_package_file`、`open_optimization`）
4. 直接操作 DOM（`explain_goal` 去 focus 一个 `[data-question-id="goal_text"]`）
5. 往输入框预填一句中文，等用户自己按发送（`review_materials`、`review_package`、`view_validation`）

第 5 类是「假按钮」：点了看不出发生什么。第 4 类的耦合完全不可见——问题 id 一改，
按钮就静默失效。而且动作有两个生产方（后端 next_actions、前端消息卡片自造），
没有任何机制能发现「前端处理了但后端不发」或反之，`open_optimization` 就这样做了一段时间的孤儿。

同期还有两个相关问题：体检面板的「自动修复错误」按钮只看错误数决定是否显示，
遇到未发布草稿时点击**静默返回**；`mw-package-updated` 与 SSE 事件描述同一次变更，
一次保存会触发两次 `load()`，先后顺序不确定。

## Decision

**动作目录成为单一事实来源，kind 在消息写入时统一打上。**

- `src/ai-construction/assistant-actions.ts` 声明 id → kind（`command` / `dialog` / `navigate`）。
- `messages.ts` 的 `normalizeBlock` 在写入 `next_actions` 时查目录并写入 `kind`；
  未登记的 id **在写入时就失败**，不会变成点了没反应的按钮。
- 前端 `assistantActions.js` 镜像同一份目录，外加导航目标声明；
  `tests/assistant-actions.test.ts` 逐项比对两边，并扫描后端源码里出现的动作 id 是否都已登记。

**消灭预填假按钮。** 三个动作改为真导航：`view_validation` 展开体检面板、
`review_package` 打开知识包首页、`review_materials` 跳到建模资料上传步骤。

**按钮可用性由后端声明。** `packageRepairAvailability()` 计算「能否自动修复」及原因
（无阻断错误 / 有未发布草稿 / 没有可承载修复的阶段），随 check 响应下发；
前端据此禁用并在 tooltip 与点击时给出原因。

**刷新在接收端合流。** 本地 window 事件与 SSE 事件共用 `PackageWorkspace` 的
200ms 防抖窗口，一次变更只触发一次 `load()`。

**阻塞待办滚出视野时给出回跳提示。** 待办渲染在消息流末尾，往上翻历史时会完全消失。
`PendingInteractionHint` 在其不可见时浮出一条可点提示。

## Alternatives considered

- **动作里直接带 endpoint，前端完全不理解业务**：更彻底，但让前端按服务端下发的字符串
  发请求，API 面和安全模型都要重新考虑；当前先用 kind + 本地注册表，收益的大头
  （消灭 if 链、孤儿动作可检测）已经拿到。
- **逐个删除与 SSE 重复的 `mw-package-updated` dispatch**：10 个 dispatch 点分布在 4 个文件，
  需要逐一确认对应写路径是否都发了服务端事件；判断错就是「保存后界面不刷新」。
  在接收端合流风险为零、收益相同。
- **把待办固定在助手栏顶部**：会长期占用本就只有 360px 的栏宽，且待办和消息的时间关系被打乱。

## Consequences

- 新增动作必须在目录登记，否则消息写入失败——把「点了没反应」从运行期问题变成写入期错误。
- `next_actions` 的 action 多出 `kind` 字段，断言精确结构的测试需要同步。
- 前端 `handleStructuredAction` 的导航分支收敛成一张表；命令与弹窗分支仍按 id 分派。
- 仍未处理：编排器 33 个公开方法各自硬编码一步流程，`advanceAfterMilestoneConfirmation`
  仍是 if 链。这块改动面大，宜单独排期。
