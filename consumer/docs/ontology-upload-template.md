# 用户本体上传 Markdown 模板

本文档定义用户上传本体 ZIP 包的推荐格式。上传入口为 `POST /agentic/ontologies/import-zip`，只接受 `.zip` 文件。

系统只接受**v0.1 本体包**：包含 `kind: ontology-package`、`schema_version: 0.1` 的 `manifest.yaml`，并使用各目录的 `index.md`。

旧格式包（源格式包，含 `SKILL.md`、`object/`、`logic/`、`action/`、`asset/` 等目录）不再由服务端自动转换。上传前需用离线转换脚本将其转为 v0.1 格式，见《本体包转换脚本使用指南》（`ontology-package-conversion-guide.md`）。安装目录和运行时不会保留或读取 `ONTOLOGY.md`、`objects-list.md`、`operations-list.md`、`scripts-list.md`。

## 通用限制

- ZIP 文件名必须以 `.zip` 结尾。
- ZIP 上传文件不能为空。
- ZIP 压缩包大小上限为 50 MB。
- ZIP 解压后总大小上限为 200 MB。
- ZIP 内路径不能包含绝对路径或 `..` 路径穿越。
- 本体 ID 只能包含字母、数字、下划线、短横线和点号，并且必须以字母或数字开头。
- 包内必须包含 `index.md`、`objects/index.md`、`operations/index.md`，且均为普通文件（不能是符号链接）。

## v0.1 本体包

v0.1 本体包目录结构如下：

```text
my-ontology.zip
└── my-ontology/
    ├── manifest.yaml
    ├── index.md
    ├── objects/
    │   ├── index.md
    │   └── 实体A.md
    ├── operations/
    │   ├── index.md
    │   └── 操作A.md
    ├── rules/
    │   └── index.md
    ├── metrics/
    │   └── index.md
    ├── scripts/
    │   ├── index.md
    │   ├── script_a.md
    │   └── script_a.py
    ├── mappings/
    │   └── object-data-mappings.json
    └── assets/                  # 可选
        └── common.conf
```

### manifest.yaml 模板

```yaml
kind: ontology-package
schema_version: 0.1
id: my-ontology
name: 我的业务本体
description: 描述该本体覆盖的业务范围、适用场景和关键对象。
version: 1.0.0
language: zh-CN
scope: []
```

### index.md 模板

```markdown
---
resource: root
key: root
title: 我的业务本体
---

# 我的业务本体

描述该本体覆盖的业务范围、适用场景和关键对象。
```

### objects/index.md 模板

```markdown
---
resource: object_index
key: objects
title: 实体清单
---

# 实体清单

| 中文名称 | 别名 | 业务定义 | 领域分类 |
| --- | --- | --- | --- |
| 实体A | A对象、业务A | 实体A在业务中的含义。 | 示例领域 |
```

### objects/实体A.md 模板

```markdown
---
resource: object
key: 实体A
title: 实体A
---

# 实体属性清单

| 中文名称 | 英文名称 | 描述 | 属性别名 |
| --- | --- | --- | --- |
| 实体A编号 | entity_a_code | 实体A的唯一编号。 | A编号、编码 |
```

### operations/index.md 模板

```markdown
---
resource: operation_index
key: operations
title: Operations 逻辑清单
---

# Operations 逻辑清单

| 名称 | 标签 | 业务描述 |
| --- | --- | --- |
| 操作A | 主流程 | 一句话描述该操作。 |
```

### operations/操作A.md 模板

````markdown
---
resource: operation
key: 操作A
title: 操作A
uses:
  objects: []
---

# 管理标签
#主流程

# 输入

| 参数名称 | 描述 | 输入源 |
| --- | --- | --- |
| 实体A编号 | 需要查询或处理的实体A编号。 | 用户输入 |

# 输出

| 参数名称 | 描述 |
| --- | --- |
| 处理结果 | 操作执行后的结论或结构化结果。 |

# 逻辑描述
```
按业务步骤执行。
```
````

### scripts/ 说明（可选）

可执行脚本放在 `scripts/` 下，并在 `scripts/index.md` 与 `scripts/<key>.md` 中描述。脚本描述文件使用脚本文件名作为 key，`bound_operation` 来自脚本顶部 `工具流程` 注释，`entrypoint` 指向原脚本文件名：

```markdown
---
resource: script
key: script_a
title: script_a
bound_operation: 操作A
runtime: python
entrypoint: script_a.py
---

| 参数 key | CLI 参数 | 类型 | 必填 | 是否多值 | 说明 | 别名 |
| --- | --- | --- | --- | --- | --- | --- |
| 实体编号 | --entity-code | string | 是 | 否 | 实体编号 | entity_code |
```

脚本参数生成分两层：优先读取脚本顶部 `parameters` / `params` / `参数` 注释，支持 JSON 单行或 YAML 注释块；如果没有声明，则静态解析脚本中的 `argparse.add_argument(...)`，提取 `flag`、`required`、`type`、`multiple` 和别名。不会执行脚本本身。也可以在 `scripts/<key>.md` 中直接维护同样的参数表。

### mappings/object-data-mappings.json（可选）

数据映射文件用于声明对象、指标、规则到 Provider 数据资源的字段映射，`fields` 的 key 使用业务字段 key。详见《本体包最终设计》（`ontology-package-final-design.md`）的 `object-data-mappings.json` 章节。

### assets/ 说明（可选）

`assets/` 用于存放多个操作或脚本共享的文本/配置公共文件，导入时保留相对目录结构。`ontology_context(resource="root")` 只返回公共文件目录树；需要正文时再按 key 读取单个文件。

```text
assets/
├── common.conf
└── rules/
    └── threshold.ini
```

支持读取的公共文件格式为 UTF-8 文本类文件：`.conf`、`.cfg`、`.ini`、`.env`、`.properties`、`.yaml`、`.yml`、`.json`、`.toml`、`.txt`、`.md`、`.csv`、`.tsv`、`.sql`、`.xml`。其他格式会保留在 ZIP 预览/目录中，但不会作为 `ontology_context(resource="asset")` 的正文读取对象。

#### 业务阶段 CheckPoint（可选）

需要启用业务阶段 CheckPoint 的知识包必须包含：

```text
assets/business-checkpoint-template.md
```

主流程 `operations/<operation_key>.md` 应定义稳定的 `stage`、`phase`、标题、阶段顺序和上报规则，并要求执行前通过 `ontology_context(resource="asset", key="business-checkpoint-template.md")` 加载通用模板。知识包不需要增加或修改 `ONTOLOGY.md`。

`waiting_user` CheckPoint 只负责阶段观察：上报后还必须调用 `request_user_interaction` 创建可恢复的等待输入状态，随后停止自动推进。用户回复并恢复执行后，先上报该阶段的 `completed` phase，再进入下一阶段。

## 旧格式包的转换

旧格式包（含 `SKILL.md`、`object/`、`logic/`、`action/`、`asset/` 的源格式包，以及使用 `*-list.md` 清单的迁移期包）需先离线转换为 v0.1 格式：

```bash
cd consumer/scripts
python legacy_package_converter.py ontology.zip           # zip 输入，默认生成 <name>_converted.zip
python legacy_package_converter.py /path/to/ontology_dir  # 目录输入，默认生成 <name>_converted 兄弟目录
```

脚本会执行「校验 → 探测 → 转换 → 再校验」管线，转换产物写入独立的输出目录或 zip，源包永不修改。转换规则与完整命令见《本体包转换脚本使用指南》（`ontology-package-conversion-guide.md`）。

## 导入前检查清单

- ZIP 内能看到 `my-ontology/manifest.yaml`，且 `kind: ontology-package`、`schema_version: 0.1`。
- `manifest.yaml` 中的 `id` 满足本体 ID 规则。
- 包内包含 `index.md`、`objects/index.md`、`operations/index.md`。
- `objects/`、`operations/` 下每个明细文件带合法 YAML frontmatter，并声明 `resource`、`key`、`title`；operation 明细还需声明 `uses.objects`（无依赖时为空数组）。
- `scripts/` 下脚本文件应为 `.py`；`scripts/<key>.md` 与 `scripts/index.md` 描述脚本参数。
- `mappings/object-data-mappings.json`（如有）为合法 JSON，`fields` 的 key 使用业务字段 key。
- `assets/`（如有）中需要模型读取正文的文件应使用受支持的 UTF-8 文本/配置格式。
- 需要开启业务阶段 CheckPoint 时，确认 `assets/business-checkpoint-template.md` 存在、非空且可读取，主流程 operation 已声明阶段映射。
- 旧格式包请先离线转换为 v0.1 格式后再上传。
