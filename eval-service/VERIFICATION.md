# eval1 实施验证

验证日期：2026-10-23。全部测试输入、身份、凭据占位值和依赖响应均为合成数据。

## 已验证

| 范围 | 命令与结果 |
| --- | --- |
| 独立后端 | `PYTHONPATH=eval-service python -m pytest eval-service/tests -q`：34 项通过 |
| 提交前后端与部署回归 | `PYTHONPATH=eval-service python -m pytest eval-service/tests deploy/tests/test_evaluation_deploy.py deploy/tests/test_workbench_deploy.py -q`：77 项及 10 个子测试通过 |
| 评测前端 | `cd eval-service/frontend && npm test && npm run build`：1 个测试文件（3 个用例）通过，生产构建通过 |
| 建模前端 | `cd modeling/onto-modeling/frontend && npm test && npm run build`：15 个测试文件通过，生产构建通过 |
| 脚本及源码 | 四个相关部署/打包脚本 `bash -n`、修改的 Python 文件语法检查、`git diff --check` 通过 |

后端覆盖：独立账号隔离、Hub2 真实存储与派生校验、mapping 新增/覆盖、快照和正式发布、稳定请求重试、定义删除后的运行幂等、用例与上传校验、数据源连接失败阻断、问答未知提交对账、取消、Judge 重试、人工改判、恢复与删除记录重放。数据库适配测试覆盖隔离 SQL 和图节点/关系导入，使用合成连接替身。

consumer 空间预置测试执行实际路由函数，验证用户与服务身份双重约束；未启动 consumer 完整应用。前端测试覆盖跳转参数、按账号隔离请求标识、拒绝旧登录响应及换账号后不重放请求。

本地 FastAPI TestClient 在受限沙箱的线程启动处挂起，因此后端测试在允许本地线程运行的环境执行，并设置 90 秒上限。测试使用临时目录，无真实依赖调用。

新增内容已检查文件类型、凭据模式、示例地址和合成测试数据；未新增运行凭据、真实业务数据或高风险附件。consumer 修改文件保留规定版权头。

## 尚未验证的部署边界

- 未启动容器、构建 Docker 镜像或制作真实离线包；部署脚本通过语法检查及命令替身测试，覆盖默认全栈、独立启动、前置检查和离线包目录结构。
- 未连接真实 MySQL、Neo4j、模型或完整 consumer 服务；替身测试不能证明数据库兼容性和模型效果。
- 未执行浏览器端到端联调。有效登录免重复认证依赖同源独立网关配置，需部署后验证跨页面续期，以及停止 Onto/Core 后的完整评测闭环。
- 当前为单进程、单实例；不提供多副本调度。MODEL-CONFIG-01 留待 eval2 统一设计。

已知非阻塞提示：Starlette TestClient 的 httpx 兼容弃用提示；建模前端已有大 chunk 构建提示。

本记录描述本地验证结果，不表示已完成部署验收。
