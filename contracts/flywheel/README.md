# 数据飞轮：消费 → 评测的反馈信号契约

| 文件 | 内容 |
|---|---|
| `feedback-signal.v1.schema.json` | 单条反馈信号 FeedbackSignal v1 的 JSON Schema（draft 2020-12） |

## 谁用、怎么用

- **消费服务（发送方）**：投递前用这份 Schema 校验每条信号，或在契约测试里用它校验 `FeedbackSignalV1` 的样例，校验不过的信号评测侧会以 `schema_invalid` 拒收。
- **评测服务（接收方）**：Schema 由 `eval-service/evaluation/feedback/models.py` 生成，评测的测试保证文件与模型一致、且 JSON Schema 校验器与评测服务对同一批信号的接受和拒收结果相同。

```python
import json
from jsonschema import Draft202012Validator

validator = Draft202012Validator(json.load(open('contracts/flywheel/feedback-signal.v1.schema.json', encoding='utf-8')))
errors = [error.message for error in validator.iter_errors(signal)]  # 空列表表示可以发送
```

## Schema 之外的约定

- 推送：`POST /evaluation/api/internal/feedback-signals`，请求体 `{"schema": "consumer-feedback-signal-batch.v1", "signals": [...]}`，每批 1–50 条、只含同一个用户的信号。
- 请求头：`Authorization: Bearer <信号提交者的访问令牌>`，`X-Eval-Service-Token: <FLYWHEEL_SERVICE_TOKEN>`。
- 单条信号不超过 256 KB；`actor.user_id` 必须等于访问令牌对应的用户名。
- 接收方忽略不认识的字段；删除字段或改变字段含义须升级为 v2。
- `signal_id` 的算法、各字段何时有值见评测侧设计文档 8.2 节。

## 修改

字段变化先改 `eval-service/evaluation/feedback/models.py`，再重新生成，消费侧同步更新：

```bash
cd eval-service && python -m evaluation.feedback.models
```
