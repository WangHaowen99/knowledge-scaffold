# Modeling 管理员控制新用户 LLM 默认配置

> 状态：已实施 · 2026-08-14

## 决策

平台默认 LLM 是一个只由 admin 维护的“初始化模板”，不是运行时全局兜底。

- admin 保存、修改或删除模板时，不写任何用户配置，也不追写任何用户快照；
- 通过 Modeling `/auth/register` 成功创建的新账号，复制注册成功时的模板作为独立快照；
- 老用户只有主动点击“同步默认 LLM 配置”时，才用点击时对应用途的模板替换自己的配置；
- 主模型与任务模型的运行时解析不直接读取当前平台模板。

这保证 admin 的配置变更不会静默影响正在使用 Modeling 的其他账号。

## 数据归属与存储

平台模板存储在 Modeling 自己的 `DATA_DIR/modeling.db`：

```text
llm_default_settings
  purpose = main | task
  base_url, model, api_key_ciphertext, max_tokens
  revision, updated_by, updated_at
```

它不属于 admin 的个人账号，不能放入 `user_llm_settings(username = 'admin')`，也不复用无类型的
通用 `settings` 表。

初始化和同步得到的用户快照存储在同一数据库的独立表：

```text
user_llm_default_snapshots
  username + purpose
  base_url, model, api_key_ciphertext, max_tokens
  default_revision, assigned_via, assigned_at
```

`assigned_via` 区分 `registration` 与 `sync`。快照包含当时完整的连接配置，因此后续模板更新或
删除不会改变它。平台模板与用户快照的 API Key 都使用 AES-256-GCM 加密，且使用不同的认证
上下文，不能跨用途或跨用户替换密文。GET API 只返回 `apiKeySet`，不回显明文或密文。

`MODELING_LLM_CONFIG_ENCRYPTION_KEY` 非空时必须至少包含 32 字节；为空时 Modeling 在
`DATA_DIR/.llm-config-encryption-key` 首次生成并复用持久化随机密钥。自动密钥文件必须与
`modeling.db` 一同持久化、备份和恢复。生产环境仍推荐由 secret manager 注入显式密钥。

## 初始化与同步

### 新用户

Modeling 的公开注册入口代理 auth-server。只有上游明确返回成功并带回新用户名后，才执行：

```text
POST /auth/register 成功
  └─ 清理该用户名可能残留的旧个人配置/旧快照
     └─ 若平台 main 模板存在：复制当前 main 及可选 task 为 registration 快照
        若平台 main 模板不存在：不写快照
```

清理同名残留用于避免“账号删除后复用用户名”时继承前一账号的密钥。注册失败、登录、读取
设置或普通业务请求都不会触发初始化。

Modeling 无法观察绕过 `/auth/register`、直接由 Consumer 或 auth-server 管理 API 创建的账号；
本次功能明确只覆盖 Modeling 建模工具，不修改 Consumer。此类账号进入 Modeling 时按没有快照
处理，绝不能因为当前恰好存在平台模板而静默补写或兜底。

### 主动同步

`POST /api/settings/llm/sync-default`：

- 必须使用当前登录用户身份，不接受目标用户名；
- 前端按当前用途提交 `purpose = main | task`；不带 `purpose` 的旧调用继续按整组同步处理；
- 同步主模型要求平台 main 模板；同步任务模型优先使用平台 task 模板，未设置时使用平台 main 模板；
- 模板不存在时返回 409，保留当前用户配置；
- 模板存在时，在单个事务中只删除发起用户对应用途的个人配置和旧快照，再写入该用途的当前模板快照；
- 不扫描、不修改其他用户；
- 同步完成后的快照仍不跟随 admin 后续修改。

前端按钮是否启用由 `platformDefaultAvailable` 决定。同步按钮旁的问号提示固定说明：
“只有 admin 账号设置了默认 LLM 配置后，才可使用。”

## 运行时解析

平台模板只在初始化和同步两个写入点被读取。认证用户运行时优先级为：

```text
主模型：完整个人 provider → 用户 main 快照 → MODELING_LLM_* 部署配置

任务模型：完整个人 task provider
       → 用户 task 快照
       → 当前用户解析后的主模型
```

这里的部署配置是既有系统启动配置，不是 admin 平台模板。当前 `llm_default_settings` 绝不参与
运行时解析，因此不会出现“没有个人配置就自动继承 admin 最新值”的隐式兜底。

`base_url`、`model` 和 `api_key` 必须从同一用途、同一作用域整体选取，不能逐字段混用。例如用户修改
快照中的 Base URL/模型后会脱离快照，未填写个人 Key 时使用空 Key，绝不能把平台或部署 Key
发送到用户指定的第三方端点。`max_tokens` 可以独立覆盖。

## admin 权限与界面

平台模板使用独立接口：

```text
GET  /api/admin/llm-defaults
PUT  /api/admin/llm-defaults
POST /api/admin/llm-defaults/test
```

服务端先验证 JWT，再强制要求 `role === 'admin'`。保存完整校验连接字段并以 revision 做乐观
并发控制；测试接口可测试未保存草稿，但不记录或返回 Key。admin 的“平台默认”与“我的覆盖”
是两个明确作用域，admin 也只能在“我的覆盖”里修改自己的用户配置。

admin 页面明确提示保存仅更新模板。配置主/任务模型时，“测试连接”和“保存默认”按钮等宽并
右对齐；清除模板只改变未来初始化/同步的可用性，不修改任何已有用户快照。

## 失败边界

auth-server 建号与 Modeling SQLite 不在同一事务中。建号成功后若快照初始化异常，路由不能把
已经成功的注册伪装为失败并诱导重试，因此保留上游成功响应并记录服务端错误。该用户不会被
运行时平台模板兜底，可在故障修复后由用户主动同步。

## 验收标准

- 普通用户访问 admin 模板 API 返回 403；未登录返回 401；
- admin 保存模板不创建或修改 `user_llm_settings(username = 'admin')`；
- admin 保存、修改、删除模板不改变老用户、已有注册快照或已有同步快照；
- Modeling 注册成功的新用户得到注册时 revision，后续 admin 修改不追写；
- 没有模板时注册不创建快照，也不读取未来新增的模板；
- 主动同步只替换发起用户的当前用途，其他用途和其他用户保持不变；
- 主模型同步按钮在没有 main 模板时禁用；任务模型同步按钮在 main/task 模板均不存在时禁用，服务端同时以 409 防护；
- 个人 provider 不混用快照或部署 API Key；
- 平台和快照 API Key 均加密落盘，响应、日志与测试错误不泄露 Key；
- `MODELING_LLM_CONFIG_ENCRYPTION_KEY` 为空时可使用持久化自动密钥；
- 非法或并发冲突的保存不会留下半写入。

## 取舍

独立用户快照会复制加密后的配置，但它精确满足“只在新建和主动同步时生效”的时间语义。
引用可变模板无法满足隔离；保留全部不可变模板历史并让用户绑定 revision 虽能减少字段复制，
却会显著增加密钥轮换、历史版本保留与删除治理成本。当前选择专用快照表，结构和行为更直接。
