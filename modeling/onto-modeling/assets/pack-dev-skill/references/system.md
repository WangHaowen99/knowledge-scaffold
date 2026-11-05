# 系统知识包（system）开发参考

**结论先说**：系统知识包是 `kind: knowledge`、`category: system` 的纯数据包，承载某个商用系统的表字段字典、表到对象映射、状态码和典型规则，按项目选用，没有页面，启用后立即生效。

平台目前没有给它单独的文件格式和读取逻辑。它和行业包走同一套装配和加载，所以表字段字典目前按行业包的数据格式承载，写在 `reference/` 和 `library/` 里。清单没有 `tables`、`dictionary`、`status_codes` 这类字段，写了不报错，也没有程序读。

静态检查不解析 `concepts.yaml`、`rules.yaml`、`standards.yaml`，也不读知识条目，这些文件写错时上传照样通过。所以打包前要跑第 10 节的自检，启用后按第 11 节确认内容真的被读到。

骨架是 `references/examples/sample-system/`（合成的「示例订单系统」）：`pack_tool.py init <包名>` 复制它并替换包名，之后把合成内容换成需求内容。`kind` 取值、`contributes` 字段、保留名、上传限制见 `references/protocol.md`。

**数据安全**：表结构、字段定义、数据字典和映射关系都可能属于受保护资料。开发者只用有权使用的资料写包，不把真实系统的字典、客户名、内部地址、凭据和业务数据提交到代码仓库。骨架里的表名、字段名和状态码全部是合成的。静态检查只挡凭据形态的内容，其余靠开发者自查。

## 1. 了解当前能力边界

和行业包的差别只有四处：清单 `category: system`；插件中心筛选 `GET /api/v1/packs?category=system`；新建项目字段 `system_packs: ["<包名>"]`；`pack.json` 里 `"category": "system"`。

| 平台目前做不到的 | 应对 |
|---|---|
| 把字典导入项目 schema | 对象 `# 数据来源` 的表名必须已在项目 schema 里，否则发布被阻断；schema 由项目自己导入 |
| 校验对象字段和字典完全一致 | 用 `require_fields` 查关键字段 |
| 程序强制读 `reference/` | 把最重要的状态码和规则同时写进 `knowledge/` |
| 用「系统」字样显示知识分层 | 知识页仍在 `layer=industry`，加载注记写「行业包」，检查分类写「行业规则」，目录是 `.claude/industry/` |

## 2. 选择放内容的文件

| 系统资料 | 放哪 | 怎么被用到 |
|---|---|---|
| 表到对象映射 | `reference/object-extraction.md` | 抽对象前阅读 |
| 表字段字典 | `reference/object-extraction.md`，常用表再写成 `library/object/<对象>.md` | 抽对象前阅读；候选片段对照改写 |
| 物理表名、界面叫法 | `concepts.yaml` 的 `aliases` | 资料分析识别；`object:<表名>` 能命中概念 |
| 状态码 | `knowledge/terminology-*.md`，`reference/rule-metric.md` | `load` 注入；抽规则前阅读 |
| 状态流转 | `reference/operation-extraction.md`，`library/operation/` | 抽操作前阅读 |
| 典型规则、系统限制 | `knowledge/domain-rule-*.md` | `load` 注入，知识页只读展示 |
| 产物必须保留的字段和关系 | `rules.yaml` | `ontology_checker` 发警告或建议，不阻断提交和发布 |
| 项目应收集的系统资料 | `standards.yaml` | 资料分析逐项核对，缺的写进「缺口与待决」 |

完整字典放 `reference/`，别放 `knowledge/`：`load` 会把条目全文注入助手上下文，单次最多 10 条，长表格既占上下文又可能挤掉其他条目。表很多时按业务域拆成多个文件，每个都写 `decision_point: object_extraction`。

## 3. 写清单

要改 `name`、`label`、`description`、`changelog`，其余照抄骨架：`kind: knowledge`、`publisher: {kind: tenant, label: ""}`、`default_on: false`、`kernel_api: ">=1.0 <2.0"`、`engines: [claude, jiuwen]`、`depends: {core: ">=0.2"}`、`checksums: checksums.sha256`。校验和文件由 `pack_tool.py check` 或 `build` 生成，不手写。`label` 显示在知识页分层标题、加载结果的来源注记和检查提示里；适用的系统版本写进 `description`，插件中心能看到。

```yaml
name: order-sys
version: 0.1.0
category: system
label: 示例订单系统
contributes:
  knowledge_dir: knowledge
  concepts: concepts.yaml
  checks: rules.yaml
  standards: standards.yaml
  reference_dir: reference
  library_dir: library
```

| `contributes` 字段 | 程序是否读取 | 写法 |
|---|---|---|
| `knowledge_dir` | 读 | 包内相对路径；知识条目从这里读 |
| `concepts` | 读 | 文件路径；装配时另存为 `concepts.json` |
| `checks` | 读 | 文件路径；**检查规则写在这里**，装配时另存为 `checks.json` |
| `standards` | 读 | 文件路径；装配时另存为 `standards.json` |
| `reference_dir`、`library_dir` | 不读 | 写 `reference`、`library`。提示词固定找这两个目录，改名后建模助手找不到 |
| `examples` | 不读 | 可选，`examples/` 下的子目录名列表，只随包复制，平台不展示 |

不要写 `contributes.rules`（它是拼进 `CLAUDE.md` 的片段列表，把 `rules.yaml` 写进去后检查规则静默失效）和 `contributes.reference`（能力包的参考指南名字列表，不接受带 `/` 的路径）。`concepts`、`checks`、`standards` 指向的文件不存在时，上传和启用都不报错，这部分内容静默缺失。

## 4. 写概念表

业务叫法做 `name`，界面叫法和物理表名放 `aliases`（`concepts_api` 目前没有程序校验，写 1）：

```yaml
concepts_api: 1
concepts:
  - name: 销售订单
    aliases: [销货单, demo_order_head]
    description: 客户下达的订单抬头，存放在 demo_order_head 表。
```

`name` 是知识条目 `concept:` 和规则 `match.concept` 要写的值，不写别名。请求 `object:<名>` 时，名字等于概念名或别名，或包含概念名才命中：`object:demo_order_head`、`object:销售订单明细` 命中「销售订单」，`object:新销货单` 不命中。没有概念表时，只有 `global` 范围的知识条目能被加载。

## 5. 写知识条目

文件名 `<type>-<slug>.md`，放在 `knowledge/`，只写抽取和校验时必须知道的结论：

```markdown
---
name: 销售订单状态码 10 待审核、20 已审核、30 已发货、90 已关闭
type: terminology
slug: order-status
scopes:
  - "concept:销售订单"
source: order-sys
confidence: high
revisions: []
related: []
---
## 背景

示例订单系统把订单状态存为数字，存放在 demo_order_head.order_status。

## 规则

状态码取值：10=待审核、20=已审核、30=已发货、90=已关闭。

**Why:** 业务资料写中文名称，导出数据写数字，需要一张对照才能核对。

**How to apply:** 订单状态属性写 `取值：10=待审核 / 20=已审核 / 30=已发货 / 90=已关闭`。
```

| 字段 | 写法 | 说明 |
|---|---|---|
| `name` | 一句结论 | 知识页标题和加载结果的小标题 |
| `type` | 状态码、称谓用 `terminology`；系统口径用 `convention`；系统限制和业务规则用 `domain-rule`；纠正常见误解用 `correction` | 缺失时 `load` 退出码 1；未知值在知识页按 `convention` 显示 |
| `slug` | 小写英文连字符 | 项目里同 slug 的条目覆盖它，二次开发过的项目靠这个改口径 |
| `confidence` | `high`、`medium`、`low` | 高置信是硬约束，中低置信是软提醒。版本间可能变化的内容写 `medium` |
| `source`、`revisions`、`related` | 包名、`[]`、`[]` | |

| `scopes` 写法 | 结果 |
|---|---|
| `- "concept:销售订单"` | 请求范围命中该概念时加载 |
| `- global` | 每次 `load` 都加载且不限量，资料分析也能拿到；只给确实全局成立的少数条目用 |
| `- "concept:demo_order_head"`（写了别名）或 `- "object:销售订单"` | 不加载 |
| `scopes: ["concept:销售订单"]`（行内列表） | 不加载，也不报错 |
| `- concept:销售订单`（没有双引号） | `load` 退出码 1（`AttributeError`），这次加载的知识全部拿不到，项目自己的条目也一样 |

条目 id 是 `<包名>:<文件名去掉 .md>`。非 `global` 条目单次最多 10 条，按包名、文件名排序。开头不是 `---` 的文件会被知识页和 `load` 静默跳过。条目只读（`readonly: true`），知识页删除返回 403 `artifact_readonly`；项目要改口径时在 `ontology/knowledge/` 写同 slug 的条目。

## 6. 写检查规则

`rules.yaml`，在清单里写到 `contributes.checks`。每条写四类之一，四类都没写的规则被忽略，不报错。

```yaml
rules_api: 1
rules:
  - id: order-keeps-key-fields
    title: 销售订单要保留单号和状态字段
    applies_to: object
    match: {concept: 销售订单}
    require_fields: [order_no, order_status]
    level: warning
    message: 这两个字段是主键和状态字段，缺失时无法对接系统数据。
```

| 类型和例子 | 检查内容 | 适用条目 |
|---|---|---|
| `require_relations_to: [销售订单]` | 「关联对象」里有指向该概念（含别名）的关系 | 只对象 |
| `require_fields: [order_no, order_status]` | 属性中文名或英文名包含该词，字段名可以直接写 | 只对象 |
| `require_sections: [输入, 输出]` | 有标题包含该词 | 全部 |
| `prefer_terms: {销货单: 销售订单}` | 正文出现非标准称谓，标题含「别名」的小节除外 | 全部 |

- `applies_to`：`object`、`operation`、`rule`、`metric`、`any`，缺省 `any`。`level`：`warning` 显示「警告」，`suggestion` 显示「建议」。
- `match.concept`：按产物文件名匹配，文件名等于概念名或别名，或包含长度大于 1 的概念名或别名。
- 提示格式：`{title}（{label} {id}）：{问题}。{message}`，出现在检查结果的「行业规则」分类。
- `require_relations_to` 看关系行的目标：`- 订单明细 → 属于（order_no → order_no） → [[销售订单]]` 才算指向销售订单。

## 7. 写资料清单和字典

`standards.yaml` 列出要向项目要的系统资料，资料分析助手逐项核对：

```yaml
standards_api: 1
standards:
  - title: 数据字典
    purpose: 核对表名、字段名和字段类型，确认项目使用的系统版本与本包一致。
```

`reference/object-extraction.md` 放映射和字典，开头声明步骤：

```markdown
---
decision_point: object_extraction
title: 示例订单系统的表字段字典和表到对象映射
---

## 表到对象映射

| 表名 | 对象 | 说明 |
|---|---|---|
| demo_order_line | 订单明细 | 一单多行，通过 order_no 关联抬头 |

## 表字段字典：demo_order_head

| 字段 | 中文名称 | 类型 | 主键 | 说明 |
|---|---|---|---|---|
| order_status | 订单状态 | 枚举 | 否 | 取值见状态码 |
```

`decision_point` 取值：`object_extraction`（抽对象，放映射和字典）、`operation_extraction`（抽操作，放状态流转、前置状态和结果状态）、`rule_metric`（抽规则和指标，放状态码、典型规则、统计口径）。没有程序解析它，这些文件靠提示词引导建模助手阅读，也不进知识页。

## 8. 写候选片段

`library/object/<对象>.md`、`library/operation/<操作>.md` 按项目产物的模板写，建模助手对照项目资料改写后才进 `ontology/`。骨架里有完整写法，要点在这几个章节：

```markdown
# 数据来源
demo_order_head
> 表名要在项目 schema 里核对，不存在时发布会被阻断。

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
|---|---|---|---|---|---|---|
| 订单状态 | order_status | 订单当前状态 | 否 | 枚举 | | 取值：10=待审核 / 20=已审核 |

# 关联对象
- 销售订单 → 下单客户（customer_code → customer_code） → [[客户档案]]
```

- 对象章节依次是 `# 对象描述`、`# 对象别名`、`# 英文名称`、`# 数据来源`、`# 查询边界`、`# 对象属性清单`、`# 关联对象`；操作章节依次是 `# 业务描述`、`# 与operation的关联`、`# 与object的关联`、`# 与object的语义关联`、`# 触发条件`、`# 输入`、`# 输出`、`# 逻辑描述`。
- 属性英文名称逐字等于字段名，只含字母、数字、下划线、汉字。类型只填 `字符串`、`整数`、`小数`、`布尔`、`日期`、`枚举`、`对象引用`；状态码写 `取值：编码=含义 / 编码=含义`。
- 跨表关系写括注字段对，关系名不含两端对象名。

## 9. 查看装配结果

启用后所有文件复制到项目的 `.claude/industry/<包名>/`，另外生成 `pack.json`（名称、版本、label、category 和各文件的指向）和 `concepts.json`、`checks.json`、`standards.json`。`.claude` 软链到装配目录，内容不要改。

| 读取方 | 读什么 | 时机 |
|---|---|---|
| `GET /api/v1/projects/{id}/knowledge?layer=industry` | `knowledge/` | 知识页；详情 `GET .../knowledge/{id}` |
| `knowledge_helper.py load --scope object:<名>` | `knowledge/`、`concepts.json` | 抽取、校验、更新、概览步骤；资料分析只用 `--scope global` |
| `ontology_checker.py ontology --json` | `checks.json`、`concepts.json` | 校验步骤 |
| 资料分析助手；抽取步骤的建模助手 | `standards.json`、`concepts.json`、`pack.json`；`reference/`、`library/` | 资料分析；抽取前 |
| `GET /api/v1/packs/{name}` | 清单的展示字段 | 插件中心；知识包的 `contents` 为空属于正常 |

## 10. 跑打包前自检

下面的片段补静态检查不看的内容，只需要 PyYAML（`pack_tool.py` 也依赖它）。在包目录里运行，不要把它存进包：

```bash
python3 - <<'EOF'
import re, yaml
from pathlib import Path
bad = []
def read(path):
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        bad.append(f"{path} 读不出来：{str(exc).splitlines()[0]}")
        return {}
c = read("manifest.yaml")["contributes"]
if c.get("rules"):
    bad.append("contributes.rules 是拼进 CLAUDE.md 的片段，检查规则写在 contributes.checks")
if c.get("standards"):
    read(c["standards"])
names = {x["name"] for x in (read(c["concepts"]).get("concepts", []) if c.get("concepts") else [])}
rules = read(c["checks"]).get("rules", []) if c.get("checks") else []
rules = rules if isinstance(rules, list) else bad.append("rules 要写成列表") or []
for rule in rules:
    if not {"require_relations_to", "require_fields", "require_sections", "prefer_terms"} & set(rule):
        bad.append(f"规则 {rule.get('id')} 没有四类字段，会被忽略")
    if (rule.get("match") or {}).get("concept") not in names | {None}:
        bad.append(f"规则 {rule.get('id')} 的 match.concept 不在概念表里")
for f in sorted(Path(c.get("knowledge_dir", "knowledge")).glob("*.md")):
    head = re.match(r"---\n(.*?\n)---\n", f.read_text(encoding="utf-8"), re.S)
    fm = yaml.safe_load(head[1]) if head else {}
    bad += [f"{f}：缺字段 {k}" for k in ("name", "type", "slug", "scopes", "source", "confidence", "revisions", "related") if k not in fm]
    block = re.search(r"^scopes:\n((?:  - .+\n)+)", head[1], re.M) if head else None
    for scope in re.findall(r"  - (.+)", block[1]) if block else ["（没有多行列表）"]:
        hit = re.fullmatch(r'"concept:(.+)"', scope)
        if scope.strip('"') != "global" and not (hit and hit[1] in names):
            bad.append(f"{f}：范围 {scope} 要写成 global 或带双引号的概念表 name")
print("\n".join(bad) or "自检通过")
EOF
```

## 11. 上传并验证启用效果

1. 打包：`python3 <skill>/scripts/pack_tool.py build <包目录> --out dist`。
2. 上传：`POST /api/v1/me/pack-uploads`（multipart 字段 `file`），返回 202 和 `id`。`GET /api/v1/me/pack-uploads/{id}` 的 `state` 是 `passed` 或 `failed`，`checks` 列出各项；`GET .../{id}/report` 是 Markdown 报告。
3. 安装：`POST /api/v1/me/pack-uploads/{id}/install`，body `{}`。
4. 启用：`POST /api/v1/projects/{id}/packs`，body `{"name": "<包名>"}`，返回 201 且 `effective` 为 `immediate`。新建项目时也可以传 `system_packs: ["<包名>"]`。

以骨架内容为例，启用后这样确认：

| 确认什么 | 调用 | 预期 |
|---|---|---|
| 知识条目 | `GET /api/v1/projects/{id}/knowledge?layer=industry` | `items` 有 `<包名>:terminology-order-status`，`readonly` 为 true，`source.label` 为 `示例订单系统 0.1.0` |
| 按业务叫法和表名加载 | 项目根目录运行 `python3 .claude/scripts/knowledge_helper.py load --scope object:销售订单`，再用 `object:demo_order_head` | 输出都含条目标题和 `来源：示例订单系统（行业包，项目里同名条目优先）` |
| 字典随包到位 | 查看 `.claude/industry/<包名>/reference/object-extraction.md` | 文件存在 |
| 检查规则 | 写一个没有 `order_no` 的 `ontology/object/销售订单.md`，运行 `python3 .claude/scripts/ontology_checker.py ontology --json` | 有一项 `category` 为 `行业规则`，`message` 含 `销售订单要保留单号和状态字段` |

升级：改内容后升 `version`，再上传、安装。已启用的项目不会自动更新，要调用 `POST /api/v1/projects/{id}/packs/{name}/upgrade`，body `{"to_version": "<新版本>"}`，立即生效。系统大版本之间字典差异大时另发一个包，包名带上系统大版本。

## 12. 排查常见失败

| 现象 | 原因 | 修法 |
|---|---|---|
| 上传 400 `pack_upload_invalid`，`checksums_missing` | zip 里没有校验和文件 | 用 `pack_tool.py build` 打包 |
| `manifest`：`contributes.rules 必须是列表` | 把 `rules.yaml` 写到了 `rules` | 改写到 `checks` |
| `compat`：`依赖 core … 没有满足的版本` | `depends` 版本范围太高 | 写 `core: ">=0.2"` |
| `names`：`<包名> <版本> 已在包仓库里`；`private_name`、`pack_name_taken` | 同版本重复上传；包名已被占用 | 升 `version`；换包名 |
| `frontend`：`ui/ 之外有前端代码：<文件>` | 有 `.css`、`.js`、`.ts` 等文件，HTML 里有脚本，或路径含 `public`、`static`、`web`、`dist`、`frontend` 目录 | 改成 Markdown，目录换名 |
| `secrets`：`疑似凭据：<文件>` | 字典或样例里有 `password = "…"`、带账号密码的连接串、密钥 | 换成 `${API_TOKEN}` 这类占位 |
| 启用 409 `assembly_failed`，`details.reason` 写 `<文件> 无法解析` | YAML 语法错误，静态检查查不到 | 修 YAML，升版本重新上传，项目保持原版本 |
| `load` 退出码 1：`'dict' object has no attribute 'startswith'` 或 `KeyError: 'type'` | `scopes` 没加双引号，或条目缺 `type` | 改成 `- "concept:<名>"`，或补字段 |
| 知识页有条目，`load` 读不到；知识页也没有 | `scopes` 写了别名、`object:` 或行内列表，`concepts` 缺失或路径错；`knowledge_dir` 路径错，或文件开头不是 `---` | 对照第 4、5 节 |
| 检查结果没有「行业规则」 | 规则写到 `contributes.rules`；`rules` 写成字典；缺四类字段；`match.concept` 没匹配到文件名 | 对照第 6 节 |
| 发布被阻断，提示表名不存在 | `library/` 里的表名不在项目 schema 里 | 项目导入 schema，或把数据来源改成「无」 |
| 新建项目 404 `pack_not_found` | `system_packs` 里放了 `category` 不符的包，或别人的私有包 | 核对 `category` |
