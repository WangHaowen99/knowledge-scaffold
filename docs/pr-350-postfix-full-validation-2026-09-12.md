# PR #350 修复后完整测试报告

- 验证日期：2026-09-12。
- 受测功能提交：`9614ac7c2eb522b1d57e375ef566f76ce0a54de0`，即验证时的 PR #350 代码版本。
- 范围：知识消费后端、前端、独立认证、两套 SDK、消费端部署，以及实际 API 和 JiuwenBox 执行链路。
- 结论：本轮自动化检查及当前依赖版本下的端到端检查全部通过，未发现需要继续修改的产品代码问题。

## 自动化检查

| 检查 | 本轮实际结果 |
| --- | --- |
| 消费后端全量 | 4047 通过、4 跳过，另 65 项子测试通过；零失败，耗时 790.68 秒 |
| 前端测试 | 496 通过、零失败 |
| 前端 ESLint | 通过 |
| 前端架构检查 | 导入边界、CSS 与入口可达性检查通过；133 个生产模块全部可达 |
| 前端构建 | 通过 |
| 独立认证服务 | 43 通过 |
| Python SDK | 5 通过 |
| TypeScript SDK | 干净安装、构建、类型检查通过；2 项测试通过 |
| 消费端部署测试 | 173 通过 |
| 后端 Dockerfile 构建 | 通过；镜像内依赖一致性检查通过 |
| JiuwenBox 镜像构建 | 按仓库固定的上游提交和运行契约构建通过 |

以上均为本轮重新运行的结果。后端的 4 项跳过分别为 Windows 句柄语义用例 1 项、macOS 目录别名用例 1 项，以及需要可选本地知识包文档的用例 2 项。

后端测试另输出 386 条警告；认证测试有依赖弃用提示。前端构建仍有单个 JS 包超过 500 kB 的提示，构建成功。

## 实际 API 与沙箱验证

使用新建的合成工作区及知识包，运行真实认证服务、当前 Dockerfile 构建的后端镜像，以及 JiuwenBox 沙箱。最终结果为 **12 项检查全部通过**。

| 场景 | 预期与实际结果 |
| --- | --- |
| 请求冒充运行时知识包字段 | 返回 `invalid_arguments`，拒绝执行 |
| 确定性直接执行 | 服务端注入运行时字段，脚本执行一次，生成并持久化产物 |
| 确定性解析器执行 | 解析器补齐参数后处理脚本成功；解析器无法使用产物写入能力 |
| 流式请求模式下的确定性执行 | 正常结束，产物可下载 |
| 处理脚本失败 | 进入 `handler_failed` 终态并完成清理 |
| 解析器失败 | 进入 `parser_failed` 终态并完成清理 |
| 真实模型同步调用 | 模型调用工具，实际沙箱脚本成功执行 |
| 真实模型流式调用 | 模型调用工具，实际沙箱脚本成功执行 |
| 非法生成标识被拒绝后修正 | 先返回 `invalid_generated_identifier`，修正后成功执行 |
| 无可信来源的设备标识被拒绝后修正 | 先返回 `ungrounded_identity`，修正后成功执行 |
| 重复提交非法参数 | 进入 `AGENT_LOOP_STALLED` 终态，公开终态不包含错误参数值 |
| 同步 Run API | `/agentic/runs/sync` 完成确定性执行，持久化产物可下载 |

真实模型的同步、流式场景共调用模型 4 次。最后三个参数拒绝场景使用受控模型响应，以稳定触发错误及重试；这些场景仍经过真实 HTTP API、参数校验和沙箱执行边界。确定性场景未调用模型。

同时验证：

- 异步 Run API 使用相同幂等键重复请求，返回同一 Run，没有重复创建。
- 事件订阅与回放一致，序号连续，终态事件唯一，持久化 Run 状态一致。
- 成功的确定性处理脚本执行次数为 1，产物通过下载 API 读取，内容与 SHA-256 校验值一致。
- 普通脚本实际读取沙箱运行契约标记，并确认无法访问后端测试目录。
- 各场景结束后，没有残留沙箱、知识包暂存副本或产物暂存目录。

## 版本与验证边界

- 自动化测试：Python 3.13.13、pytest 9.1.1、openjiuwen 0.1.17；Node.js 24.14.1。
- 最终 API 镜像：Python 3.11.15、openjiuwen 0.1.17、FastAPI 0.139.2、Uvicorn 0.51.0、Pydantic 2.13.4。
- JiuwenBox：Python 3.11.6。
- 本机原有后端镜像的 openjiuwen 为 0.1.14。发现差异后已按当前 Dockerfile 重建，并重新运行全部 12 项端到端检查；上表采用重建后的结果。运行环境需使用当前源码及其锁定依赖才能复现该结果。
- 本轮覆盖 DataAgent 适配器所在的后端测试，但没有实际连接 DataAgent/GaussDB 外部数据源，也未进行 Windows/macOS 实机验证。

## 复现入口与清理

后端先在代码树之外的虚拟环境安装 `consumer/backend/requirements-test.txt`，从干净检出执行以下命令。前端、SDK 构建与后端测试使用独立检出；实际运行环境再单独隔离，避免构建标记和运行数据影响静态检查。

```bash
# consumer/
PYTHONPATH=backend python -m pytest -q -ra --tb=short backend/tests
PYTHONPATH=.:backend python -m pytest -q -ra --tb=short deploy/tests

# consumer/frontend/
npm ci --no-audit --no-fund
npm run lint
npm test
npm run build

# auth-server/
PYTHONPATH=. python -m pytest -q tests

# consumer/sdk/python-sdk/
PYTHONPATH=src python -m pytest -q tests

# consumer/sdk/typescript-sdk/
npm ci --no-audit --no-fund
npm run build
npm run typecheck
npm test
```

已核对各测试检出的受版本控制源码没有变化。报告仅保存汇总结果和合成场景说明，不包含凭据、原始日志、数据库或构建制品。本报告已通过敏感信息检查；临时容器、专用镜像、虚拟环境、检出及测试文件均已清理。
