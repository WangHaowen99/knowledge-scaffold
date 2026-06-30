# 本体包定义案例

本文档说明一个可上传到 Meta Wise 的本体包应该如何组织和定义。这里描述的是“已转换本体包”格式，也就是 zip 解压后能找到 `manifest.yaml`，并且同级目录下包含 `objects/`、`operations/`、`mappings/` 或 `scripts/` 等本体内容。

真实参考可以看示例业务 DOMAIN 示例：

- `examples/ontology-packages/example-domain-knowledge-agent.zip`

## 本体包解决什么问题

本体包把一个业务领域的知识拆成几类文件：

| 类型 | 目录或文件 | 作用 |
| --- | --- | --- |
| 包清单 | `manifest.yaml` | 声明本体 id、名称、版本、语言和是否启用 |
| 根说明 | `index.md` | 给智能体预加载的领域总览 |
| 对象 | `objects/*.md` | 定义业务对象，例如设备、工单、物料、AO、TargetItem |
| 操作 | `operations/*.md` | 定义可回答或可执行的业务问题、流程和计算逻辑 |
| 映射 | `mappings/object-data-mappings.json` | 把业务对象映射到实际 provider 里的资源、表、标签和字段 |
| 脚本 | `scripts/*.md` 和 `scripts/*.py` | 给某个 operation 绑定稳定的 Python 直执行实现 |

对象不是数据库表，操作也不是普通函数。本体包先用对象和操作表达业务语义，再通过 mapping 和绑定关系连接真实数据源。

## 推荐目录结构

```text
my-ontology-package/
|-- manifest.yaml
|-- index.md
|-- objects/
|   |-- index.md
|   |-- 设备.md
|   `-- 维修工单.md
|-- operations/
|   |-- index.md
|   `-- 查询设备近期维修工单.md
|-- mappings/
|   `-- object-data-mappings.json
`-- scripts/
    |-- index.md
    |-- query_recent_work_orders.md
    `-- query_recent_work_orders.py
```

上传 zip 时，可以把 `my-ontology-package/` 整个目录打进 zip，也可以只把目录内容打进 zip。导入接口会递归查找 `manifest.yaml`，并选择包含本体结构的目录作为包根目录。

导入后，平台会把包安装到工作区运行目录，并做版本化存储，形态大致如下：

```text
.onto-runtime/workspaces/<workspace>/ontologies/<ontology_id>/
|-- manifest.yaml
`-- versions/
    `-- 1.0.0/
        |-- index.md
        |-- objects/
        |-- operations/
        |-- mappings/
        `-- scripts/
```

所以，上传前看到的目录结构和导入后的运行目录结构不同，这是正常的。

## manifest.yaml

`manifest.yaml` 是本体包入口。平台导入 zip 时会读取它来确定本体身份和版本。

```yaml
kind: ontology-package
schema_version: 0.1
id: equipment-maintenance-demo
name: 设备维修知识包
description: 面向设备维修场景的示例本体包，覆盖设备、维修工单和近期维修记录查询。
version: 1.0.0
language: zh-CN
enabled: true
scope:
  - equipment-maintenance
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `kind` | 是 | 支持 `ontology` 或 `ontology-package` |
| `id` | 是 | 本体唯一标识，导入接口要求字母或数字开头，只包含字母、数字、下划线和短横线 |
| `name` | 是 | 展示名称 |
| `description` | 是 | 给用户和智能体看的领域说明 |
| `version` | 是 | 版本号，支持字母、数字、点、下划线和短横线 |
| `language` | 是 | 推荐 `zh-CN` |
| `enabled` | 否 | 是否启用，缺省为 `true` |
| `scope` | 否 | 业务范围标签 |

导入后，平台会补充或更新 `active_version` 和 `versions`。手工写包时通常只需要写 `version`。

## index.md

根目录 `index.md` 是本体总览，会进入选中本体的预加载上下文。它适合写领域边界、核心对象、核心操作和查询原则。

```markdown
---
resource: ontology_index
key: root
title: 设备维修知识包
---

# 设备维修知识包

本体覆盖设备资产、维修工单和维修记录查询。回答问题时应先识别用户提到的设备编码，再判断是否需要查询维修工单。

# 领域边界

- 只回答设备维修、保养、工单和故障记录相关问题。
- 不在本体和 mapping 未声明的情况下猜测真实表名或字段名。
- 涉及真实数据时，优先使用本体绑定的数据源和 `mappings/object-data-mappings.json`。
```

## objects/index.md

`objects/index.md` 是对象目录，主要用于让智能体快速知道这个本体里有哪些业务对象。

```markdown
---
resource: object_index
key: objects
title: 对象目录
---

# 对象目录

| object key | 标题 | 说明 |
| --- | --- | --- |
| 设备 | 设备 | 被维修、巡检或保养的业务资产 |
| 维修工单 | 维修工单 | 针对设备发起、派工、处理和关闭的维修记录 |
```

建议 `object key` 和 `objects/<object key>.md` 的文件名保持一致。部分运行期路径会按对象 key 直接定位文件。

## objects/*.md

单个对象文件用 frontmatter 声明对象身份，用正文说明业务含义、字段和查询边界。

`objects/设备.md`：

```markdown
---
resource: object
key: 设备
title: 设备
description: 被维修、巡检或保养的业务资产。
aliases:
  - 资产
  - 装置
---

# 业务定义

设备是企业内部需要维护的物理资产，例如泵、压缩机、生产线设备或关键工装。

# 字段

| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 |
| --- | --- | --- | --- | --- | --- |
| equipment_id | 设备编码 | string | 是 | 设备的唯一编码 | 资产编码,装置编码 |
| equipment_name | 设备名称 | string | 否 | 设备展示名称 | 资产名称 |
| status | 状态 | string | 否 | 当前运行状态 | 设备状态 |
| location | 位置 | string | 否 | 设备所在区域或车间 | 安装位置 |

# 查询边界

- 本对象是业务对象，不等同于数据库表。
- 真实查询应通过 `mappings/object-data-mappings.json` 找到 provider、resource 和字段。
- 未在字段表或 mapping 中声明的字段，不应自行推断。
```

## operations/index.md

`operations/index.md` 是操作目录。每一行对应一个用户可能提出的问题、业务流程或计算能力。

```markdown
---
resource: operation_index
key: operations
title: 操作目录
---

# 操作目录

| operation key | 标题 | 说明 |
| --- | --- | --- |
| 查询设备近期维修工单 | 查询设备近期维修工单 | 根据设备编码查询最近一段时间内的维修工单 |
```

## operations/*.md

单个操作文件定义输入、输出、逻辑步骤、关联对象和消费边界。

`operations/查询设备近期维修工单.md`：

````markdown
---
resource: operation
key: 查询设备近期维修工单
title: 查询设备近期维修工单
description: 根据设备编码查询最近一段时间内的维修工单。
uses:
  objects:
    - 设备
    - 维修工单
---

# 业务目标

当用户给出设备编码时，查询该设备最近一段时间内的维修工单，并返回工单号、状态、故障描述和处理结果。

# 输入

| 参数名称 | 描述 | 输入源 |
| --- | --- | --- |
| 设备编码 | 设备唯一编码 | 用户提供 |
| 天数 | 查询最近多少天，缺省 30 天 | 用户提供或默认值 |

# 输出

| 参数名 | 描述 |
| --- | --- |
| 工单列表 | 匹配到的维修工单 |
| 查询结果 | 成功、无数据或失败 |

# 逻辑描述

```text
前置校验
    如果设备编码为空，先追问用户。
查询设备
    在 object:设备 中确认设备是否存在。
查询维修工单
    在 object:维修工单 中按设备编码和时间范围查询。
结果汇总
    返回工单号、工单状态、故障描述、处理结果和关闭时间。
```

# 业务动作关联

| 动作名称 | 触发条件 | 关联实体 | 执行顺序 |
| --- | --- | --- | --- |
| 维修记录维护 | 设备存在但没有维修工单 | object:维修工单 | 1 |

# 消费边界

- 缺少设备编码时必须追问，不能猜测。
- 不直接猜测真实表名，应先通过 mapping 或本体绑定解析数据源。
- 如果本操作绑定了脚本，优先使用脚本执行稳定查询。
````

## mappings/object-data-mappings.json

mapping 用来把本体对象连接到真实数据 provider。对于 SQL、MindsDB、NL2SQL 一类 provider，`resource` 通常表示表、视图或远程资源名。对于图检索 provider，`resource` 会作为图标签，`fields.*.source` 会作为图属性名。

```json
{
  "schema_version": "0.1",
  "default_provider_alias": "maintenance",
  "objects": {
    "设备": {
      "provider_alias": "maintenance",
      "resource": "Equipment",
      "primary_key": ["equipment_id"],
      "fields": {
        "equipment_id": {
          "source": "equipment_id",
          "type": "string",
          "required": true,
          "readable": true
        },
        "equipment_name": {
          "source": "equipment_name",
          "type": "string",
          "readable": true
        },
        "status": {
          "source": "status",
          "type": "string",
          "readable": true
        },
        "location": {
          "source": "location",
          "type": "string",
          "readable": true
        }
      },
      "policy": {
        "read_only": true,
        "max_rows": 500
      }
    },
    "维修工单": {
      "provider_alias": "maintenance",
      "resource": "WorkOrder",
      "primary_key": ["work_order_id"],
      "fields": {
        "work_order_id": {
          "source": "work_order_id",
          "type": "string",
          "required": true,
          "readable": true
        },
        "equipment_id": {
          "source": "equipment_id",
          "type": "string",
          "readable": true
        },
        "fault_description": {
          "source": "fault_description",
          "type": "string",
          "readable": true
        },
        "status": {
          "source": "status",
          "type": "string",
          "readable": true
        },
        "closed_at": {
          "source": "closed_at",
          "type": "datetime",
          "readable": true
        }
      },
      "policy": {
        "read_only": true,
        "max_rows": 500
      }
    }
  }
}
```

注意事项：

- `objects` 下的 key 必须和对象文档中的 `key` 对齐。
- `primary_key` 推荐同时出现在 `fields` 中。
- `source` 写真实数据源里的字段名或图属性名。
- `provider_alias` 表示这个对象默认使用哪个业务数据连接别名，具体连接仍然需要在平台“本体绑定”里配置。
- 不要在 mapping 里写账号、token 或数据库密码。

## scripts/index.md

如果某个 operation 需要稳定、可复现的直执行逻辑，可以在 `scripts/` 下登记脚本。

```markdown
---
resource: script_index
key: scripts
title: 脚本目录
---

# 脚本目录

| script key | 标题 | 绑定操作 | 参数摘要 |
| --- | --- | --- | --- |
| query_recent_work_orders | 查询设备近期维修工单脚本 | operation:查询设备近期维修工单 | equipment_id, days |
```

## scripts/*.md

脚本文档负责把 operation 和 Python 入口文件绑定起来。

`scripts/query_recent_work_orders.md`：

```markdown
---
resource: script
key: query_recent_work_orders
title: 查询设备近期维修工单脚本
description: 根据设备编码和天数查询近期维修工单。
runtime: python
entrypoint: query_recent_work_orders.py
bound_operation: 查询设备近期维修工单
timeout_seconds: 300
---

# 脚本用途

用于稳定执行 operation:查询设备近期维修工单。

# 输入参数

| name | flag | type | required | description | aliases |
| --- | --- | --- | --- | --- | --- |
| equipment_id | --equipment-id | string | true | 设备编码 | 资产编码,装置编码 |
| days | --days | int | false | 最近天数 | 时间范围 |

# 输出

| output key | type | description |
| --- | --- | --- |
| ok | boolean | 是否成功执行 |
| equipment_id | string | 设备编码 |
| work_orders | array | 维修工单列表 |
| query_result | string | 成功、无数据或失败 |

# 执行边界

- 只能通过本体包登记后的 `execute_script` 调用。
- `entrypoint` 必须指向本体包 `scripts/` 目录下的 Python 文件。
- 参数只接受上方参数表声明的字段。
- 脚本不保存数据库账号、token 或连接密钥，真实连接由运行时和本体绑定提供。
```

脚本参数表推荐使用英文表头 `name`、`flag`、`type`、`required`、`description`、`aliases`，这样当前运行期解析最稳定。说明和值可以继续写中文。

## scripts/*.py

脚本文件必须是 Python 文件，路径不能是绝对路径，不能包含 `..`。`entrypoint` 可以写 `query_recent_work_orders.py`，运行期会归一化为 `scripts/query_recent_work_orders.py`。

`scripts/query_recent_work_orders.py`：

```python
import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--equipment-id", required=True)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    # 示例只展示脚本输入输出结构。
    # 真实项目中，数据查询应通过运行时注入的本体绑定连接完成。
    result = {
        "ok": True,
        "equipment_id": args.equipment_id,
        "days": args.days,
        "work_orders": [],
        "query_result": "无数据",
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

运行期会根据脚本文档中的参数表，把用户输入转成 CLI 参数。例如：

```json
{
  "ontology_id": "equipment-maintenance-demo",
  "operation_key": "查询设备近期维修工单",
  "arguments": {
    "equipment_id": "EQ0001",
    "days": 30
  }
}
```

会转成类似下面的命令：

```bash
python scripts/query_recent_work_orders.py --equipment-id EQ0001 --days 30
```

## 打包方式

在 WSL 或 Linux 中：

```bash
cd my-ontology-package
zip -r ../equipment-maintenance-demo.zip . -x "**/__pycache__/*" "*.pyc"
```

在 Windows PowerShell 中：

```powershell
Compress-Archive -Path .\my-ontology-package\* -DestinationPath .\equipment-maintenance-demo.zip -Force
```

导入限制：

- 只支持 `.zip`。
- zip 文件大小上限是 50 MB。
- 解压后内容大小上限是 200 MB。
- Markdown、YAML、JSON、Python 文件建议统一使用 UTF-8。

## 校验清单

提交或上传前，建议逐项检查：

| 检查项 | 要求 |
| --- | --- |
| 包入口 | zip 解压后能找到 `manifest.yaml` |
| 本体结构 | `manifest.yaml` 同级存在 `objects/`、`operations/`、`objects-list.md` 或 `operations-list.md` 中至少一种 |
| 本体 id | `id` 合法、稳定、不要和已有本体冲突 |
| 版本 | `version` 合法，重复版本导入时需要选择覆盖或新建版本 |
| 对象 key | `objects/index.md`、`objects/*.md`、mapping 中的对象 key 保持一致 |
| 操作引用 | `operations/*.md` 的 `uses.objects` 能对应到真实对象 |
| 数据映射 | `mappings/object-data-mappings.json` 中的 `resource`、`primary_key`、`fields.source` 和真实 provider 对齐 |
| 脚本绑定 | `scripts/*.md` 的 `bound_operation` 能对应到真实 operation key |
| 脚本入口 | `entrypoint` 指向存在的 `.py` 文件，且不使用绝对路径或 `..` |
| 密钥 | 包内不包含数据库密码、token、API key |

## 和示例业务 DOMAIN 示例的对应关系

示例业务 DOMAIN 包可以作为真实规模参考：

| 示例文件 | 对应职责 |
| --- | --- |

如果要新建一个领域包，建议先照本文档的最小示例跑通导入、绑定和查询，再逐步扩展对象、操作和脚本。
