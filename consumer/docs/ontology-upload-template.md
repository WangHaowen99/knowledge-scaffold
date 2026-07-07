# 用户本体上传 Markdown 模板

本文档定义用户上传本体 ZIP 包的推荐格式。上传入口为 `POST /agentic/ontologies/import-zip`，只接受 `.zip` 文件。

系统支持两类 ZIP：

- **源格式包（推荐）**：包含 `object/` 和 `logic/` 目录，系统会自动转换为运行时本体结构。
- **已转换本体包**：包含 `manifest.yaml`，并包含 `objects/`、`operations/`、`objects-list.md`、`operations-list.md` 中至少一类内容。

## 通用限制

- ZIP 文件名必须以 `.zip` 结尾。
- ZIP 上传文件不能为空。
- ZIP 压缩包大小上限为 50 MB。
- ZIP 解压后总大小上限为 200 MB。
- ZIP 内路径不能包含绝对路径或 `..` 路径穿越。
- 本体 ID 只能包含字母、数字、下划线和短横线，并且必须以字母或数字开头。

## 推荐源格式包

源格式包适合用户直接整理业务本体材料。系统会从 `SKILL.md` 读取本体元数据，从 `object/` 生成对象定义，从 `logic/` 生成操作定义，从 `action/` 生成脚本注册，从 `asset/` 复制公共文件。`object/`、`logic/`、`action/`、`asset/` 均支持递归扫描；`object/`、`logic/`、`action/` 导入时会平铺处理，`asset/` 会保留目录结构。

```text
my-ontology.zip
└── my-ontology/
    ├── SKILL.md
    ├── object/
    │   ├── 实体A.md
    │   └── 实体B.md
    ├── logic/
    │   ├── 操作A.md
    │   └── 操作B.md
    ├── action/                 # 可选
    │   ├── script_a.py
    │   └── .env                # 可选
    └── asset/                  # 可选
        ├── common.conf
        └── rules/
            └── threshold.ini
```

必要文件：

- `SKILL.md`
- `object/`
- `logic/`

可选文件：

- `action/`
- `asset/`

### SKILL.md 模板

```markdown
---
id: my-ontology
name: 我的业务本体
description: 描述该本体覆盖的业务范围、适用场景和关键对象。
---
```

### object/实体A.md 模板

每个对象一个 Markdown 文件，文件名就是实体名。系统读取 `对象描述`、`对象别名`、`对象属性清单`，并生成 `objects-list.md` 与 `objects/<实体名>.md`。

```markdown
# 对象描述
实体A代表业务中的某类核心对象。

# 对象别名
A对象,业务A

# 英文名称
EntityA

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
| 实体A编号 | entity_a_code | 实体A的唯一编号。 | 是 | 字符串 | A编号、编码 | 非空 |
| 状态 | status | 实体A当前业务状态。 | 否 | 字符串 | 业务状态 | 无 |

# 关联对象
- 当前对象 -> 关系名 -> [[实体B]]
```

转换规则：

- `objects-list.md` 生成列：`中文名称`、`别名`、`业务定义`、`领域分类`。
- `objects/<实体名>.md` 生成列：`中文名称`、`英文名称`、`描述`、`属性别名`。
- `主键`、`类型`、`属性规则` 当前不进入运行时对象文件。

### logic/操作A.md 模板

每个操作一个 Markdown 文件，文件名就是操作名。系统生成 `operations-list.md` 与 `operations/<操作名>.md`。

````markdown
# 业务描述
一句话描述该操作能回答什么业务问题，或完成什么业务动作。

# 与logic的关联
- 被流程使用 > 无
- 使用流程 > [[子流程A]]

# 与object的关联
- 输入数据的对象：[[实体A]]
- 输出数据的对象：[[实体B]]
- 支撑数据的对象：无

# 输入
| 参数名称 | 描述 |
| --- | --- |
| 实体A编号 | 需要查询或处理的实体A编号。 |

# 输出
| 参数名称 | 描述 |
| --- | --- |
| 处理结果 | 操作执行后的结论或结构化结果。 |

# 逻辑描述
```
1. 根据输入参数读取业务数据。
2. 必要时执行 script_a 脚本。
3. 输出处理结果。
```
````

转换规则：

- 如果文件顶部存在 `#工具流程` 标记，目标 operation 的 `管理标签` 生成为 `工具流程`。
- 如果没有 `#工具流程` 标记，系统读取 `# 与logic的关联` 中的 `被流程使用`：有具体上游流程时生成 `子流程`，为 `无` 或未填写时生成 `主流程`。
- `# 业务描述` 进入 `operations-list.md` 的 `业务描述` 列。
- `# 输入` 转换为 `参数名称`、`描述`、`输入源` 三列；如果源表没有 `输入源`，主流程默认 `用户输入`，子流程和工具流程默认 `父流程传递`。
- `# 输出` 转换为 `参数名称`、`描述` 两列。
- `# 逻辑描述` 原样进入 operation 文件。
- `# 与logic的关联` 和 `# 与object的关联` 当前不写入运行时 operation 文件。

### action/script_a.py 模板

`action/` 是可选目录。导入时系统会复制所有 `action/**/*.py` 以及 `.env` / `*.env` 文件到目标 `scripts/`，并根据 Python 脚本生成 `scripts-list.md`。脚本文件名可以保持英文；如果脚本对应某个 `logic/<操作名>.md`，在脚本顶部用注释声明对应的工具流程。

```python
# 工具流程: 操作A
# 参数:
# - name: 实体编号
#   flag: --entity-code
#   description: 实体编号
#   required: true
#   type: string
#查数脚本 （涉及mindsdb查数的脚本会加这个标签）
print("script output")
```

生成后的 `scripts-list.md` 优先使用脚本顶部 `工具流程` 注释作为 key，路径仍指向原脚本文件名：

```markdown
# 工具流程脚本清单

当工具流程已在此清单中维护对应脚本时，优先调用 `execute_script`，不要先读 operation markdown 再手工复刻逻辑。

## 脚本映射
- `操作A` -> `scripts/script_a.py`
  `parameters`: `[{"name":"实体编号","flag":"--entity-code","description":"实体编号","required":true,"type":"string","aliases":["entity_code"]}]`
```

如果没有 `工具流程` 注释，系统会退回使用脚本文件名去掉 `.py` 后的名称作为 key，适合那些直接按脚本名调用、没有对应 `logic/*.md` 的脚本。下划线开头的 helper 脚本和 `.env` 文件会被复制到 `scripts/`，但不会写入 `scripts-list.md`。

脚本参数生成分两层：优先读取脚本顶部 `parameters` / `params` / `参数` 注释，支持 JSON 单行或 YAML 注释块；如果没有声明，则静态解析脚本中的 `argparse.add_argument(...)`，提取 `flag`、`required`、`type`、`multiple` 和别名。不会执行脚本本身。

也可以在已转换本体包中直接维护结构化参数：

```markdown
- `操作A` -> `scripts/script_a.py`
  `parameters`: `[{"name":"entity_code","flag":"--entity-code","description":"实体编号","required":true,"type":"string"}]`
```

### asset/common.conf 模板

`asset/` 是可选目录，用于存放多个 logic 或脚本共享的文本/配置公共文件。导入时会复制到运行时 `assets/`，并保留相对目录结构。`ontology_context(resource="root")` 只返回公共文件目录树；需要正文时再按 key 读取单个文件。

```text
asset/
├── common.conf
└── rules/
    └── threshold.ini
```

生成到 root catalog 后，公共文件 key 使用 `assets/` 下的相对路径：

```text
assets_catalog:
supported formats: .cfg, .conf, .csv, .env, .ini, .json, .md, .properties, .sql, .toml, .tsv, .txt, .xml, .yaml, .yml
key format: pass the shown file key to ontology_context(resource="asset", key=...).
- common.conf
- rules/
  - threshold.ini (key: rules/threshold.ini)
```

支持读取的公共文件格式为 UTF-8 文本类文件：`.conf`、`.cfg`、`.ini`、`.env`、`.properties`、`.yaml`、`.yml`、`.json`、`.toml`、`.txt`、`.md`、`.csv`、`.tsv`、`.sql`、`.xml`。其他格式会保留在 ZIP 预览/目录中，但不会作为 `ontology_context(resource="asset")` 的正文读取对象。

## 已转换本体包

如果你已经准备好运行时本体结构，也可以直接上传已转换包。

```text
my-ontology.zip
└── my-ontology/
    ├── manifest.yaml
    ├── ONTOLOGY.md              # 可选
    ├── objects-list.md
    ├── operations-list.md
    ├── objects/
    │   └── 实体A.md
    ├── operations/
    │   └── 操作A.md
    ├── scripts-list.md          # 可选
    ├── scripts/                 # 可选
    │   └── script_a.py
    └── assets/                  # 可选
        └── common.conf
```

### manifest.yaml 模板

```yaml
kind: ontology
id: my-ontology
name: 我的业务本体
description: 描述该本体覆盖的业务范围、适用场景和关键对象。
version: 1.0.0
language: zh-CN
scope: []
```

### objects-list.md 模板

```markdown
# 实体清单

| 中文名称 | 别名 | 业务定义 | 领域分类 |
| --- | --- | --- | --- |
| 实体A | A对象、业务A | 实体A在业务中的含义。 | 示例领域 |
```

### objects/实体A.md 模板

```markdown
# 实体属性清单

| 中文名称 | 英文名称 | 描述 | 属性别名 |
| --- | --- | --- | --- |
| 实体A编号 | entity_a_code | 实体A的唯一编号。 | A编号、编码 |
```

### operations-list.md 模板

```markdown
# Operations 逻辑清单

| 名称 | 标签 | 业务描述 |
| --- | --- | --- |
| 操作A | 主流程 | 一句话描述该操作。 |
```

### operations/操作A.md 模板

````markdown
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

## 导入前检查清单

- ZIP 中能看到 `my-ontology/SKILL.md`、`my-ontology/object/`、`my-ontology/logic/`，或能看到 `my-ontology/manifest.yaml`。
- `SKILL.md` frontmatter 中的 `id` 满足本体 ID 规则。
- `object/` 下每个实体文件至少包含对象描述和对象属性清单。
- `logic/` 下每个操作文件至少包含业务描述、与logic的关联、输入、输出和逻辑描述。
- 如果使用 `action/`，可执行脚本文件应为 `.py`；`.env` / `*.env` 会作为脚本资产复制，但不会注册为工具流程。
- 如果使用 `asset/`，需要模型读取正文的文件应使用受支持的 UTF-8 文本/配置格式。
