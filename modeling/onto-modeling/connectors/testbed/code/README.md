# 代码仓库测试床

给 `code-repo` 提供方联调用的 git 服务和三个合成项目，全部是虚构代码。

| 仓库 | 内容 |
|---|---|
| `orders-java.git` | Spring + JPA：4 个实体（客户、订单、明细、商品）、枚举、`@Query`、JdbcTemplate SQL、两个服务、一个控制器、请求 DTO |
| `orders-python.git` | FastAPI + SQLAlchemy + pydantic：3 个模型、`CheckConstraint`、`@validates`、`field_validator`、`text()` SQL、两个服务、路由 |
| `orders-ts.git` | NestJS + TypeORM + class-validator：3 个实体、枚举、DTO、两个服务、控制器、`query()` SQL |

## 起服务

```bash
GIT_TESTBED_TOKEN=$(openssl rand -hex 16) docker compose up -d --build
```

- 地址 `https://git.testbed.invalid:8443/<仓库>.git`，网络 `onto-testbed-biz`，自签证书在卷 `onto-testbed-git-certs` 的 `git.crt`。
- 用户名 `x-access-token`（默认）或 `oauth2`，口令是 `GIT_TESTBED_TOKEN`；只读，可按提交号取。
- `/moved/<仓库>.git` 回 301，用来验证不跟重定向。

## 接到网关

- 网关容器接入 `onto-testbed-biz`，挂证书卷，设 `ONTO_CONNECTOR_CODE_CA_FILE=/certs/git.crt`。
- 登记代码仓库源：地址填上面的 https 地址，分支 `main`，令牌填 `GIT_TESTBED_TOKEN`；出口白名单 `git.testbed.invalid:8443`。

后端测试不需要起这个服务：`tests/test_f16_code.py` 直接在 127.0.0.1 随机端口起 `server.py`（http，用提供方的测试参数放开）。
