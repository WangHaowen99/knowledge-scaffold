# NL2SQL Template Provider

## 功能简介

NL2SQL Template 是一个业务数据查询 Provider，核心思路是**用预定义的查询模板将自然语言精确映射到 SQL**，避免 NL2SQL 服务的语义理解偏差，并提升查询速度。

工作流程：

1. 收到用户查询后，先尝试模板匹配。
2. **模板命中**：直接执行模板中的 SQL，返回结果。
3. **模板未命中**：回退到 NL2SQL 服务，由服务端将自然语言转为 SQL。

模板匹配支持两种方式：

- **默认匹配器**：从当前激活版本目录下的 `scripts/query_templates.json` 读取模板，按查询文本精确匹配 key。
- **自定义匹配器**：在当前激活版本目录的 `scripts/sql_matcher.py` 中编写 Python 函数，基于 `query_templates.json` 中的模板数据实现模糊匹配、参数提取等灵活逻辑。

## 本体需要提供的文件

NL2SQL Template Provider 从**当前激活的本体版本目录**读取 `scripts/` 下的模板文件。Provider 会先解析 `<workspace_root>/<ontology_id>/manifest.yaml` 中的 `active_version`，再定位到对应版本目录。

版本化本体目录结构如下：

```text
<workspace_root>/<ontology_id>/
├── manifest.yaml
└── versions/
    └── <active_version>/
        └── scripts/
            ├── query_templates.json    # 必需，定义查询模板
            └── sql_matcher.py          # 可选，自定义匹配逻辑
```

未迁移到版本目录的旧本体，Provider 会回退到 `<workspace_root>/<ontology_id>/scripts/` 读取同名文件。

### 1. `scripts/query_templates.json` — 查询模板（必需）

此文件定义所有查询模板。无论使用默认匹配器还是自定义匹配器，都需要提供此文件。

它是一个 JSON 对象，key 是查询文本，value 是 SQL 或包含 SQL 与去重配置的对象。

#### 简单格式：key → SQL 字符串

```json
{
  "查询零件": "SELECT * FROM Part",
  "查询供应商": "SELECT * FROM Supplier"
}
```

默认匹配器下，用户查询文本与 key 完全一致时，直接执行对应的 SQL。

#### 丰富格式：key → 对象

```json
{
  "查询零件": {
    "sql": "SELECT * FROM Part WHERE id IN ({IDS}) ORDER BY updateTime DESC",
    "dedupe_key": "id"
  }
}
```

对象字段说明：

| 字段 | 类型 | 必填 | 说明                                                                                                                                                |
| --- | --- | --- |---------------------------------------------------------------------------------------------------------------------------------------------------|
| `sql` | string | 是 | 要执行的 SQL 语句。                                                                                                                                      |
| `dedupe_key` | string | 否 | 去重依据字段。设置后，查询结果按该字段去重，每个字段值只保留第一条记录。要求 SQL 已通过 `ORDER BY` 排好序，使期望保留的记录排在同值的首位。通常用来简化时序数据场景下模板中的SQL，避免窗口函数导致模板中的SQL不可维护，单纯去重可直接在SQL中使用DISTINCT关键字。 |

**去重约定**：当 `dedupe_key` 存在时，Provider 按 SQL 返回的顺序，对 `dedupe_key` 字段值相同的记录只保留首条。因此 SQL 需自行排序（如时序数据场景使用 `ORDER BY updateTime DESC`），Provider 不做排序假设。

### 2. `scripts/sql_matcher.py` — 自定义匹配器（可选）

默认匹配器只支持精确匹配。当需要模糊匹配、参数提取、组合条件等灵活逻辑时，在当前激活版本目录的 `scripts/sql_matcher.py` 中实现自定义匹配函数。自定义匹配器通常会读取同目录下的 `query_templates.json` 作为模板数据源，在其基础上实现更灵活的匹配逻辑。

如果此文件存在，Provider 会优先使用它；若不存在，则使用默认匹配器。

#### 函数签名

```python
def matcher(query: str) -> tuple[str | None, dict | None]:
    ...
```

- **入参** `query`：用户的自然语言查询文本。
- **返回值**：
  - 匹配成功：返回 `(sql, metadata)`，其中 `sql` 是要执行的 SQL，`metadata` 是元数据字典。
  - 未匹配：返回 `(None, None)`，Provider 会回退到 NL2SQL 服务。

`metadata` 字典支持以下字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `from_template` | string | 模板标识，会附加到查询结果中。 |
| `dedupe_key` | string | 去重依据字段。设置后，查询结果按该字段去重，每个字段值只保留第一条记录。要求 SQL 已排好序。 |

#### 示例：基于模板数据的自定义匹配

```python
import json
from pathlib import Path

# 加载同目录下的模板数据
_templates = json.loads(
    (Path(__file__).parent / "query_templates.json").read_text(encoding="utf-8")
)


def matcher(query):
    # 1. 先尝试精确匹配
    if query in _templates:
        entry = _templates[query]
        if isinstance(entry, str):
            return entry, {"from_template": query}
        metadata = {"from_template": query}
        if entry.get("dedupe_key"):
            metadata["dedupe_key"] = entry["dedupe_key"]
        return entry["sql"], metadata

    # 2. 模糊匹配：查询包含模板 key 的关键词
    for key, entry in _templates.items():
        if key in query:
            sql = entry if isinstance(entry, str) else entry["sql"]
            return sql, {"from_template": key}

    # 3. 未匹配，回退到 NL2SQL
    return None, None
```

## Ontology 数据源配置

在知识包的「数据源」页选择 NL2SQL Template，并为当前 ontology 填写以下私有配置：

| 字段 | 说明                         | 必填 |
| --- |----------------------------| --- |
| 服务地址 | NL2SQL 服务的 URL             | 是 |
| 访问 Token | 服务启用鉴权时使用的 Bearer Token | 否 |
| 数据源名称 | NL2SQL 服务端的场景标识，决定查询哪个数据源  | 是 |
| 超时时间 | HTTP 请求超时秒数，默认 600         | 否 |
| 最大重试次数 | 请求失败重试次数，默认 3              | 否 |

## 匹配器优先级规则

1. 若当前激活版本目录下存在 `scripts/sql_matcher.py`，使用自定义匹配器。
2. 若不存在，使用默认匹配器（对 `scripts/query_templates.json` 的 key 做精确匹配）。
3. 两种匹配器未命中时，均回退到 NL2SQL 自然语言查询。

`sql_matcher.py` 是为存量知识包保留的 legacy 能力。运行时继续保持上述优先级以避免兼容性变化，但每次动态加载都会产生 `legacy_sql_matcher_load` 结构化审计事件。新知识包应优先使用声明式 `scripts/query_templates.json`。
