# 业务数据 Provider 扩展指南

本文说明当系统需要接入一种新的业务数据检索后端时，应该如何新增 provider。

这里的 provider 指 `business_data_search` 背后的业务数据检索实现。它不是全局系统配置，而是工作区级数据连接的一种类型：用户在某个 workspace 下创建数据连接，再把 ontology 显式绑定到该连接。运行时只使用当前 workspace + 当前 ontology 的绑定连接。

## 设计边界

新增 provider 时需要保持三个边界不变：

1. `business_data_search` 工具入参不变：`ontology_id`、`entity`、`query`。
2. provider 配置不进入 `system_config.json`，只存储在 workspace 的数据连接中。
3. 前端表单由后端 provider schema 驱动；后端 schema 只描述字段，不承载界面布局。

如果新需求只是某个 provider 的 URL、token、scene 等参数不同，不应该新增 provider，而应该新增一个数据连接实例。

## 当前调用链

```text
business_data_search
  -> resolve_business_data_connection(workspace_id, ontology_id)
  -> query_business_data_stream(request, connection)
  -> provider_registry.get_provider_spec(connection.provider)
  -> spec.factory({"ontology_search": {provider_name: connection.config}})
  -> provider.search_stream(query=query, entity=entity)
```

关键文件：

| 职责 | 文件 |
| --- | --- |
| provider 接口定义 | `runtime/data_search/agentic/business_data/provider_base.py` |
| provider 实现 | `runtime/data_search/agentic/business_data/providers/` |
| 业务数据 provider 注册与表单 schema | `runtime/data_search/agentic/business_data/provider_registry.py` |
| workspace 数据连接存储与配置归一化 | `runtime/data_search/agentic/business_data/connections_store.py` |
| 运行时 provider 调用 | `runtime/data_search/agentic/business_data/service.py` |
| 测试连接 | `runtime/data_search/agentic/business_data/connection_tester.py` |
| 前端数据连接表单 | `platform/console/src/components/DataConnectionManagement.jsx` |
| 后端测试 | `tests/backend/test_business_data_connections.py`、`tests/backend/test_business_data_search_resolution.py` |

## 新增步骤

### 1. 定义 provider id

provider id 使用小写 snake_case，例如：

```text
warehouse_http
```

这个 id 会同时用于：

- `PROVIDER_SPECS` 的 key
- 数据连接 payload 的 `provider`
- provider 读取配置时的 section 名：`config["ontology_search"]["warehouse_http"]`

不要使用展示名称、中文名或带空格的值作为 provider id。

### 2. 实现 provider 类

在 `runtime/data_search/agentic/business_data/providers/` 下新增实现文件，例如：

```python
"""Warehouse HTTP business data provider."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator

import requests

from ..provider_base import BusinessDataSearchEvent


class WarehouseHttpProvider:
    name = "warehouse_http"

    def __init__(self, config: dict):
        ontology_search = dict(config.get("ontology_search") or {})
        provider_config = dict(ontology_search.get("warehouse_http") or {})
        self.base_url = str(provider_config.get("url") or "").rstrip("/")
        self.token = str(provider_config.get("token") or "").strip()
        self.timeout = int(provider_config.get("timeout_seconds") or 600)
        self._session = requests.Session()

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[BusinessDataSearchEvent]:
        yield {"type": "start", "content": "开始查询业务数据"}

        response = self._session.post(
            "{}/query".format(self.base_url),
            headers=self._headers(),
            json={"entity": entity, "query": query},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload: Dict[str, Any] = response.json()

        yield {
            "type": "final",
            "content": json.dumps(payload, ensure_ascii=False),
        }
        yield {"type": "done", "content": "查询流程结束"}

    def close(self) -> None:
        self._session.close()

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer {}".format(self.token)
        return headers
```

实现要求：

- 必须实现 `search_stream(query, entity)`。
- 必须至少产出一个 `type=final` 的事件；`business_data_search` 会把最后的 `final.content` 作为工具结果返回给模型。
- 建议事件顺序为 `start -> intermediate* -> final -> done`。
- `final.content` 建议是 JSON 字符串，便于模型稳定读取结果。
- 如果 provider 内部持有 `requests.Session` 或其他连接资源，应提供 `close()`。
- 不要在 provider 里读取 env 或全局 system config。配置必须来自 workspace 数据连接。

### 3. 注册 provider schema

修改 `runtime/data_search/agentic/business_data/provider_registry.py`：

```python
from .providers.warehouse_http import WarehouseHttpProvider


PROVIDER_SPECS = {
    # existing provider specs omitted
    "warehouse_http": BusinessDataProviderSpec(
        name="warehouse_http",
        label="Warehouse HTTP",
        description="通过 Warehouse HTTP 服务检索本体业务数据。",
        factory=WarehouseHttpProvider,
        fields=[
            ProviderField("url", "服务地址", required=True),
            ProviderField("token", "访问 Token", secret=True),
            ProviderField("schema", "默认 Schema"),
            ProviderField("timeout_seconds", "超时时间", type="int", default=600),
            ProviderField("max_retries", "最大重试次数", type="int", default=3),
        ],
    ),
}
```

字段规则：

| 字段属性 | 含义 |
| --- | --- |
| `name` | 存储字段名，也是前端表单字段名 |
| `label` | 前端展示文案 |
| `type` | 当前支持 `str`、`int`、`bool` |
| `required` | 保存连接时必填 |
| `secret` | 不在列表接口明文返回，只返回 `has_<name>` |
| `default` | 新建连接和缺省归一化时使用 |

secret 字段的行为：

- 创建或更新时传入明文，例如 `token`。
- 列表和详情接口不会返回明文，只返回 `has_token: true`。
- 更新连接时 secret 留空会保留旧值。

如果 provider 有跨字段约束，在 `normalize_provider_config()` 末尾补充校验。例如“启用鉴权时密码必填”这类逻辑不应该放到前端。

### 4. 增加测试连接逻辑

修改 `runtime/data_search/agentic/business_data/connection_tester.py`。

入口函数增加分支：

```python
def test_business_data_connection(connection: ResolvedBusinessDataConnection) -> Dict[str, Any]:
    if connection.provider == "warehouse_http":
        return _test_warehouse_http(connection)
    # existing provider branches omitted
```

测试函数需要做真实连接探测，而不是只校验格式：

```python
def _test_warehouse_http(connection: ResolvedBusinessDataConnection) -> Dict[str, Any]:
    tested_at = _utc_now()
    config = dict(connection.config or {})
    url = _coerce_string(config.get("url")).rstrip("/")
    if not url:
        return {"success": False, "message": "Warehouse HTTP URL 未配置", "tested_at": tested_at}

    timeout = min(int(config.get("timeout_seconds") or 10), 10)
    headers = {"Content-Type": "application/json"}
    token = _coerce_string(config.get("token"))
    if token:
        headers["Authorization"] = "Bearer {}".format(token)

    try:
        response = requests.post(
            "{}/query".format(url),
            headers=headers,
            json={"query": "SELECT 1"},
            timeout=timeout,
        )
        response.raise_for_status()
        return {"success": True, "message": "Warehouse HTTP 连接正常", "tested_at": tested_at}
    except requests.exceptions.HTTPError as exc:
        response = exc.response
        status_code = response.status_code if response is not None else "unknown"
        detail = _error_detail(response)
        message = "Warehouse HTTP 连接失败（HTTP {}）".format(status_code)
        if detail:
            message = "{}：{}".format(message, detail[:200])
        return {"success": False, "message": message, "tested_at": tested_at}
    except requests.exceptions.RequestException as exc:
        return {"success": False, "message": "Warehouse HTTP 请求失败：{}".format(str(exc)), "tested_at": tested_at}
```

注意：

- 测试连接要覆盖网络、鉴权头、关键查询入口。
- 超时建议 capped 到 10 到 15 秒，避免管理界面长时间阻塞。
- 如果 provider 的健康检查接口无法证明查询接口可用，应优先探测查询接口。

### 5. 前端表单是否需要改

通常不需要改前端。前端会调用：

```text
GET /agentic/business-data/provider-schemas
```

然后按返回的 `fields` 动态渲染表单。

只有在字段宽度需要特殊处理时，才修改 `platform/console/src/components/DataConnectionManagement.jsx` 的 `PROVIDER_FIELD_LAYOUT`：

```js
const PROVIDER_FIELD_LAYOUT = {
  mindsdb: {
    fullWidth: ['url'],
  },
  nl2sql_http: {
    fullWidth: ['url', 'token'],
  },
  warehouse_http: {
    fullWidth: ['url', 'token', 'schema'],
  },
};
```

布局规则应留在前端。不要为了控制一列或两列，把 UI 布局字段加进后端 provider schema。

当前前端已经会自动把包含以下关键词的字段放成整行：

```text
url, endpoint, token, schema, prompt, query, json, headers, sql
```

如果新 provider 的字段命名遵守这个约定，一般不需要新增布局配置。

### 6. 增加测试

至少补充以下测试：

1. provider schema 是否包含新 provider。
2. 创建连接时字段归一化、必填校验、secret 脱敏是否正确。
3. 测试连接接口是否真的调用 provider 的探测接口。
4. `business_data_search` 是否通过 workspace + ontology 绑定解析到新 provider。

推荐测试位置：

```text
tests/backend/test_business_data_connections.py
tests/backend/test_business_data_search_resolution.py
```

测试连接示例：

```python
def test_data_connection_test_route_probes_warehouse_http_service(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({
            "url": url,
            "headers": headers or {},
            "json": json,
            "timeout": timeout,
        })
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    create_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "warehouse-main",
            "name": "Warehouse Main",
            "provider": "warehouse_http",
            "config": {
                "url": "https://warehouse.example.com",
                "token": "secret-token",
            },
        },
    )
    assert create_response.status_code == 201

    response = client.post("/agentic/data-connections/warehouse-main/test", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert calls[0]["url"] == "https://warehouse.example.com/query"
```

### 7. 更新文档

新增 provider 后同步更新：

- `docs/api.md` 中 `GET /agentic/business-data/provider-schemas` 示例。
- 如 provider 有特殊部署要求，更新 `README.md` 或 `backend/README-CN.md`。
- 如果新增了第三方依赖，更新 `backend/requirements.txt` 与部署说明。

## 验收命令

后端：

```bash
PYTHONPATH=platform/server:modeling/skills python -m pytest \
  tests/backend/test_business_data_connections.py \
  tests/backend/test_business_data_search_resolution.py \
  -q
```

如果 provider 会影响导入默认绑定或 ontology summary，再补跑：

```bash
PYTHONPATH=platform/server:modeling/skills python -m pytest tests/backend/test_ontology_routes.py -q
```

前端：

```bash
cd frontend
npm run lint
npm run build
```

最终检查：

```bash
git diff --check
```

## 开发检查清单

- [ ] provider id 使用小写 snake_case。
- [ ] provider 实现只读取 workspace 连接配置，不读取全局配置和 env。
- [ ] `PROVIDER_SPECS` 已注册 `factory` 和字段 schema。
- [ ] secret 字段设置了 `secret=True`，接口不会返回明文。
- [ ] 必填字段和跨字段约束在后端校验。
- [ ] 测试连接会真实探测远端服务。
- [ ] `search_stream()` 会产出 `final` 事件。
- [ ] 持有连接资源的 provider 实现了 `close()`。
- [ ] 前端只在必要时补充 `PROVIDER_FIELD_LAYOUT`。
- [ ] 后端测试覆盖创建、脱敏、测试连接、运行时解析。
- [ ] API 文档和部署说明已同步。
