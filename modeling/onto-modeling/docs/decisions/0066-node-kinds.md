# 0066 四类节点：agent、脚本、人工、外部

## Context

详设 6.5 把节点分成四类：agent 节点由助手执行入口命令；脚本节点由内核在沙箱里跑确定性脚本、不调用模型；
人工节点只产出一张决策卡（比如登记线下会签的结果）；外部节点经连接器网关调外部系统，需要对应的证据源提供方（H2）。
B3a～B4b 只有 agent 节点（FL-05 等价于 S-20）。证据源本期只留接口（ADR-0056），没有任何提供方。

## Decision

1. **声明**（节点声明里按 `kind` 多出的字段，静态检查见 ADR-0065）：
   - 脚本：`script: {name: <本包脚本>, inputs: [工作区路径通配], args: [], output: <落在 produces 里的路径>}`；
   - 人工：`decision: {title, background, options: [{id, label}] (≥2), record: <落在 produces 里的路径>}`；
   - 外部：`provider: {kind: <证据源提供方类型>}`。
   完成判定照旧用探针（例如记录文件出现），节点状态的算法不变。
2. **脚本节点**（`services/flow/node_kinds.run_script`，FL-05 同步跑完返回 200）：脚本取本项目装配目录里哈希核对过的
   那份；声明的输入文件（最多 200 个）打进同一个包的 `inputs/` 下，经脚本验证网关的 `/v1/verify` 放进 jiuwenbox 一次性
   沙箱运行（新函数 `jiuwenbox_client.run_in_gateway`）。**不在本机回退**：脚本来自包作者，不能在主容器里执行；网关没配
   或沙箱不可用回 `503 service_unavailable`，打包超过沙箱参数上限回 `413 payload_too_large`，脚本退出码非 0 或超时回
   `422 node_run_failed`（带退出码与 stderr 末尾）。通过时标准输出（执行器截到 1500 字符）经写入事务写进 `output`，
   提交 `Onto-Source: plugin`、`Onto-Node: <节点>@<版本>`。
3. **人工节点**：FL-05 往对话里出一张 `question.asked`（kind `decision`、不带「其他，我来说」），同一节点有没回答的卡就
   沿用、不重复出；问题记在数据目录 `flow-human/<项目>.json`。S-11 在会话里找不到这个问题时交给流程运行时：选项校验、
   结论（选项、记录人、时间、补充说明）经写入事务写进 `record`（`Onto-Source: web`、`Onto-Node`），对话里追加
   `question.answered`，回答过再答回 410。对话记录都在事件循环里追加。
4. **外部节点**：需要的提供方从 `app/evidence.available_providers()` 查，本期恒为空。启用、升级时回
   `409 pack_unavailable`（`reason=not_installed`、`providers`），PL-03 的 `depends` 检查不通过、写明缺哪个提供方；
   FL-05 同样回 409。不注册任何假的执行器。

## Alternatives considered

- **脚本节点沙箱不可用时本机回退**（同 `script_verify`）：`script_verify` 跑的是项目里的脚本，回退是为了不挡开发；节点包
  的脚本是第三方代码，在主容器里跑等于让包作者拿到后端进程的权限。
- **脚本节点异步跑、经命令任务推进度**：沙箱单个用例 20 秒封顶，同步返回更简单；以后要跑长任务再接后台任务。
- **人工节点的卡只放对话里**：B5c 合并时改成也进待处理（见文末补充）。

## Consequences

- 脚本节点的产出受执行器输出上限（1500 字符）限制，适合检查结论、摘要；大产出要等沙箱支持回传文件。
- 人工节点的问题文件不随删除项目清理（删除流程在会话宿主里，B5b 范围），残留无害。
- 证据源实现时，`available_providers()` 改成查平台已安装的提供方，外部节点经连接器网关执行，接口形状不变。

## 补充（B5c 合并，2026-11-02）

- 人工节点没回答的决策卡也进待处理的「决策」（`inbox._human_nodes`，读 `flow-human/<项目>.json`）：`source` 为
  `plugin`、`pack` 是节点所属的包（算进插件页角标），`detail` 就是那张 Question。IN-03 `answer` 与 S-11 同一个退路，
  回答后从待处理消失。包停用了、节点不在当前流程里的卡不列。
- FL-05 出卡后立即重算流程与待处理，`inbox.updated` 跟着推。

## 补充（B5c 审查修复）

- 脚本输出、决策记录在运行时再查一遍（规范化、不碰受保护目录、在本节点的写入范围里），不过回 403 `path_forbidden`；
  脚本的输入不能读 `.git`、`.claude`、`.onto`、`.ontology-state`（审查第 1 条）。
- 执行器回报输出有没有被截断（`stdout_truncated`），截断了回 422、不写产出（审查第 6 条）。
- 脚本节点的提交（来源 plugin）照样过包声明的 commit 门禁（审查第 12 条）；已归档项目不能回答人工节点的卡（审查第 11 条）。
