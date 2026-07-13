# Jiuwen owned runtime 生命周期与压力门禁

## 生命周期契约

`JiuwenReactLoopEngine` 默认创建的 ReActAgent 由本次调用完整持有，必须遵守以下约束：

1. `state.session_id`、trace 和业务事件继续使用业务 session ID。
2. 每个默认 Agent 使用唯一内部 Jiuwen `conversation_id`。同一业务 session 的并发调用不能共享 Jiuwen checkpoint。
3. Agent 结束后在进程唯一的 `jiuwen-loop` 中依次注销运行时工具、清理该 Agent 的回调和指标，并调用公共 `Runner.release(internal_session_id)`。
4. 清理只移除该 Agent 持有的 callback event、metric、resource card、tool、tag link 和 checkpoint；其他 Agent 的资源必须保留。
5. 清理步骤不接收业务 `cancel_event`。成功、业务错误和取消结果已经确定后，清理失败只记录日志，不能替换原始结果。
6. 注入 `agent_factory` 的 Agent 生命周期由调用方管理，继续接收业务 session ID，engine 不执行 owned cleanup。
7. managed stream 取消时必须取消并等待 child producer 结束，再保存 context 和提交 session，不能留下 `jiuwen-managed-stream-producer` task。

## 离线压力脚本

脚本使用真实 OpenJiuwen `ReActAgent` 和进程内 `OfflineModel`。`OfflineModel` 直接返回固定 `AssistantMessage`，不会发起网络请求。

默认运行 500 次短调用，分成 25 批，每批保持 20 个 model call 同时进入屏障：

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=consumer/backend:/private/tmp/meta-wise-openjiuwen-site \
/Users/example/vscodePo/onto-agent/venv/bin/python \
  consumer/backend/scripts/jiuwen_runtime_pressure.py
```

快速复核一批并发：

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=consumer/backend:/private/tmp/meta-wise-openjiuwen-site \
/Users/example/vscodePo/onto-agent/venv/bin/python \
  consumer/backend/scripts/jiuwen_runtime_pressure.py --runs 20 --workers 20
```

脚本失败时以非零状态退出，并输出完整 JSON。门禁同时验证：

- 成功数等于请求运行数，且无执行错误。
- 每批达到 20 个并发 model call，默认共完成 25 批。
- 峰值为 120 个 tool/resource card、80 个 callback。
- 结束后 owned resource、callback、metric、checkpoint store/blob 全部为零。
- 进程只使用一个 `jiuwen-loop`，结束后没有 pending task、ready handle 或 scheduled handle。

`tag_keys=1` 是 OpenJiuwen resource manager 的空 tag 索引，`callback_locks=1` 是 callback framework 的进程级单例锁。两者不持有 Agent 数据；门禁检查 tag link、callback event、callback、metric 和 checkpoint 的实际 owned 内容。

## 2026-07-13 执行证据

在冻结的本地 `develop` 基线上执行默认参数：

| 指标 | 结果 |
|---|---:|
| runs / successes | 500 / 500 |
| workers / 最大同时 model call | 20 / 20 |
| 并发批次 | 25 |
| 耗时 | 14.164 秒 |
| 峰值 resource cards / tools | 120 / 120 |
| 峰值 callback event keys / callbacks | 80 / 80 |
| 峰值 callback metrics / agent stores | 20 / 20 |
| 结束后 owned resources | 全部为 0 |
| Jiuwen loop 遗留 task / ready / scheduled | 0 / 0 / 0 |

对应定向回归位于 `consumer/backend/tests/test_jiuwen_runtime_cleanup.py`，覆盖精确 owned 清理、无关 callback/metric 保留、同业务 session 并发隔离、自定义 factory 边界、清理异常不覆盖 success/error/cancel，以及 managed child producer 取消等待。
