# eval1 独立评测服务

## Context

Hub2 通用包以统一账号身份访问，已提供 evaluation mapping_update 契约。旧评测依赖 Onto 项目、Core 存储及建模 Agent 工作流；原 TypeScript Judge 默认加载 Core 模型配置，不能直接搬运运行入口。

## Decision

- 新增 `eval-service/`，使用仓库已有 FastAPI/Python、React/Vite 和独立 SQLite。优先直接复用 Python mapping 确定性规则，避免跨语言重写绑定；判分协议和既有容差移植到独立客户端，不加载建模 Agent。
- 运行期仅调用账号、Hub 和 consumer。用户身份按 issuer/subject 隔离，不存建模项目或成员表。用户令牌只保留内存，配置凭据和输入数据用外部提供的 Fernet 密钥保护。
- 以固定 Hub 版本生成 mapping 派生副本；只改 mapping 和 manifest version，由 Hub 再次校验白名单。快照和正式发布独立幂等。
- consumer 新增受限 evaluation 空间预置接口，同时验证用户和独立服务凭据；空间归属取当前用户，不接受 owner。已有安装、激活、运行与取消接口继续使用用户权限。
- 单进程持有文件锁，SQLite 保存请求和运行状态；最多两个执行线程、队列最多 16 项、每用户一个活动任务。重启后等待授权对账，不盲目重跑。提交未知时复用 consumer 已实现的 session fingerprint 与 Idempotency-Key。
- 同源独立路径复用现有浏览器登录态，两个前端使用相同 Web Locks 刷新锁，账号切换后拒收旧结果；评测认证直接连接账号服务。独立网关的路由配置属于部署前提。
- 独立部署参数 `--eval-only`，同时纳入默认全栈部署与完整离线包；不加入 `--onto-modeling-only`。仍使用独立 Compose、运行卷及外部 `EVAL_ENV_FILE`，部署前校验配置文件存在。
- 临时模型策略 MODEL-CONFIG-01：consumer 管问答模型，部署参数管 Judge；可选 NL2SQL 暂复用 Judge 客户端配置。eval2 决定最终统一方案。
- 可选 SQL adapter 使用 PyPI 的 `sqlglot`（MIT）解析并限制只读 SQL，避免仅靠正则判断跨库查询和写入。其余新增依赖来自仓库已有技术体系；不引入专有代码或数据。

## Alternatives considered

- 整体搬运 TypeScript 引擎：会保留 Core 仓储、配置和工作流依赖。
- Onto 代理发布和鉴权：重新引入项目归属耦合，建模停机影响评测。
- 复制工作区或建立虚拟项目：违反包来源与独立身份边界。
- 跨域 URL 传 token：不采用。跨源 SSO 留到明确设计后实施。

## Consequences

Python mapping 规则从原实现提取，后续修改应保持绑定行为测试一致。服务只支持单实例，不承诺多副本或绝对恰好一次。完整工具轨迹仍由 consumer 管理，本地保存运行引用并明确标记未采集。外部 SQL 连接只读，上传数据用隔离 dataset 库；图导入必须用户明确选择并使用本轮 scope。真实模型效果、数据库兼容性和现场网关路由须部署联调确认。
