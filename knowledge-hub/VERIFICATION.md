# Hub2 开发验证记录

本记录覆盖本地实现和合成数据回归，未执行提交、镜像发布或运行环境部署。

## 已验证

- Hub 后端及建模相关回归：145 项通过（包含最新 develop 的 Solver 包往返回归）。覆盖独立包身份、用户隔离、服务能力限制、无项目外部导入、mapping 派生范围、不可变版本、幂等重试、撤权、Git 故障恢复、备份恢复，以及项目最新资料同步和清理。
- 旧 Hub 项目包 API 已删除，测试确认旧路径返回 404；建模发布和现有评测快照使用 Hub2。没有历史复制、双发布目标或旧包读取适配。
- 建模前端：14 个测试文件通过；生产构建通过，仍有大分块提示。标准构建曾在压缩体积统计阶段被系统终止，最终使用 Vite 的 `reportCompressedSize: false` 完成构建，产物仍正常压缩。
- 建模部署脚本语法、两份 Compose YAML 解析及单一建模凭据映射检查通过。仅配置原有 `ONTO_HUB_SERVICE_TOKEN` / `HUB_SERVICE_TOKEN` 的回环测试同时完成资料同步、包发布和下载；评测/消费不能调用建模资料清理接口，重复服务凭据被拒绝。

后端针对性回归命令（在安装依赖的 Python 环境执行）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=modeling/onto-modeling/backend:knowledge-hub/backend python -m pytest knowledge-hub/backend/tests modeling/onto-modeling/backend/tests/test_hub_packages.py modeling/onto-modeling/backend/tests/test_knowledge_hub.py modeling/onto-modeling/backend/tests/test_hub_transport.py modeling/onto-modeling/backend/tests/test_publish_binding_gate.py modeling/onto-modeling/backend/tests/test_modeling_eval.py modeling/onto-modeling/backend/tests/test_solver_package_roundtrip.py -q -p no:cacheprovider
```

本次使用临时 Python 3.12 环境运行；HTTP 测试使用本机回环地址和合成身份服务。测试输出有两项提示：TestClient 的 httpx 使用方式弃用提示，以及刻意构造重复 ZIP 成员时的警告。

补充验证：0.2 operation-centric 求解包可归档，mapping 派生不能修改求解 sidecar 或 capabilities；正式发布执行求解门禁，评测校验后的对象变化会在预留 Hub 版本前拒绝。

## 验证边界

消费服务未修改；外部导入和评测发版仅验证 Hub 服务端能力，使用合成调用方，不代表消费适配或 eval1 已实施。现有建模评测只替换知识包交接，未扩展独立评测服务。

未访问真实账号、业务数据或模型服务，未验证实际部署网络、容量及真实评测质量。部署前需注入统一身份服务配置和各调用服务独立凭据，确认鉴权可达，并按 README 执行备份恢复演练。没有 Hub1 存量数据，不提供迁移流程。
