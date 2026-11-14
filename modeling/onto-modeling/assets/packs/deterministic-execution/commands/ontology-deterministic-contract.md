为用户明确选中的 Operation 定义确定性执行契约。只能选择以下一种数据访问方式：

- 仅使用输入参数：`data_access: none`。
- 读取工作空间数据：`data_access: workspace_provider`。

不要询问或记录数据库品牌、地址、库名、账号、密码、Cypher 或 SQL。工作空间数据模式只能通过 Consumer 的 `DeterministicProviderClient` 使用白名单只读能力。

确认业务操作、自然语言触发样例和业务输入输出。技术字段由助手根据已确认业务生成，不要求普通用户编写。读取 `.claude/reference/deterministic-agent-draft.md`，按协议写入 `ext/deterministic-execution/drafts/<handler>.json`；首次制作时提示用户在面板重新扫描、制作草稿、确认保存并验证；已有草稿或正式契约时提示点击“读取助手新版草稿”，核对变化后确认保存并重新验证。不要直接写 `ext/deterministic-execution/contracts/`，也不要声称草稿已成为正式契约。
