# NL2SQL Template Provider

> 2026-07-22 安全说明
>
> - backend 不再动态加载知识包中的 `scripts/sql_matcher.py`
> - 自定义 matcher 仅支持声明式 `scripts/sql_matcher.json`
> - `sql_matcher.json` 仅支持 `exact` / `contains` 两种匹配类型
> - 每条规则只允许提供静态 `sql`，不支持 `sql_template`
> - 旧 `sql_matcher.py` 只会触发安全审计并被忽略，不会在 backend 进程执行

## 功能简介

NL2SQL Template 是一个业务数据查询 Provider。执行顺序如下：

1. 先尝试声明式 `sql_matcher.json`
2. 若未命中，再尝试 `query_templates.json` 的默认精确匹配
3. 若仍未命中，再回退到 NL2SQL 自然语言查询接口

## 文件结构

当前激活版本目录下的 `scripts/` 需要提供以下文件：

```text
<workspace_root>/<ontology_id>/
├── manifest.yaml
└── versions/
    └── <active_version>/
        └── scripts/
            ├── query_templates.json
            └── sql_matcher.json
```

未使用版本化目录的旧知识包，仍会回退到 `<workspace_root>/<ontology_id>/scripts/` 读取同名文件。

## query_templates.json

`query_templates.json` 是必需文件，支持两种格式：

简单字符串格式：

```json
{
  "查询零件": "SELECT * FROM Part",
  "查询供应商": "SELECT * FROM Supplier"
}
```

带去重元数据的对象格式：

```json
{
  "查询零件": {
    "sql": "SELECT * FROM Part ORDER BY updateTime DESC",
    "dedupe_key": "id"
  }
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `sql` | string | 是 | 要执行的静态 SQL |
| `dedupe_key` | string | 否 | 返回结果按该字段去重，保留首条记录 |

## sql_matcher.json

`sql_matcher.json` 是可选文件，用于在默认精确匹配之外增加声明式规则。

顶层结构：

```json
{
  "version": 1,
  "rules": [
    {
      "match": {
        "type": "contains",
        "value": "零件"
      },
      "sql": "SELECT * FROM Part",
      "metadata": {
        "from_template": "零件模板"
      }
    }
  ]
}
```

### 支持的匹配类型

- `exact`
- `contains`

### 不支持的能力

出于安全原因，以下能力已明确禁用：

- `regex`
- `sql_template`
- 任意 Python matcher 执行

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `version` | integer | 是 | 当前仅支持 `1` |
| `rules` | array | 是 | 规则列表 |
| `rules[].match.type` | string | 是 | `exact` 或 `contains` |
| `rules[].match.value` | string | 是 | 匹配值 |
| `rules[].match.ignore_case` | bool | 否 | 是否忽略大小写 |
| `rules[].sql` | string | 是 | 要执行的静态 SQL |
| `rules[].metadata` | object | 否 | 可选元数据 |
| `rules[].from_template` | string | 否 | 便捷写法，会写入 `metadata.from_template` |
| `rules[].dedupe_key` | string | 否 | 便捷写法，会写入 `metadata.dedupe_key` |

### 元数据约束

允许通过 `metadata` 传递附加字段，但目前只有以下字段对 Provider 行为有内置影响：

- `from_template`
- `dedupe_key`

未知字段不会参与 SQL 生成。

### 非法规则的处理方式

- 单条非法规则会被忽略，并记录 warning 日志
- 整个 `sql_matcher.json` 无法解析时，会回退到默认 matcher
- 旧 `sql_matcher.py` 存在时，只会记录安全审计日志，不会执行

## 优先级规则

1. 若存在合法的 `sql_matcher.json`，先按其中规则匹配
2. 若未命中，再回退到 `query_templates.json` 的默认精确匹配
3. 若仍未命中，再回退到 NL2SQL 自然语言查询

## 配置建议

- 把需要动态条件拼接的场景收敛到上游 NL2SQL 服务，不要在 matcher 内拼接用户输入
- 只在 matcher 中配置静态、可审计的 SQL
- 对多条候选规则，优先写更具体的规则在前面
