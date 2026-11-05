# 行业包（industry）开发参考

**结论先说**：行业包是 `kind: knowledge`、`category: industry` 的纯数据包，不带代码和页面，启用到项目后立即生效。它给建模助手提供领域概念、业务知识、资料清单和候选建模片段，给 `ontology_checker` 提供只发警告和建议的检查规则。

静态检查不解析 `concepts.yaml`、`rules.yaml`、`standards.yaml`，也不读知识条目，这些文件写错时上传照样通过。所以打包前要跑第 11 节的自检，启用后按第 12 节确认内容真的被读到。

骨架是 `references/examples/sample-industry/`：`pack_tool.py init <包名>` 复制它并替换包名，之后把合成内容换成需求内容。`kind` 取值、`contributes` 字段、保留名、上传限制见 `references/protocol.md`。

## 1. 选择放内容的文件

| 需求 | 放哪 | 平台怎么用 |
|---|---|---|
| 统一称谓、识别同义词 | `concepts.yaml` | 资料分析按标准称谓归并；决定加载哪些知识条目 |
| 领域规则、口径、术语含义 | `knowledge/*.md` | `knowledge_helper.py load` 注入抽取、校验步骤，知识页只读展示 |
| 产物应满足的结构要求 | `rules.yaml` | `ontology_checker` 发警告或建议，不阻断提交和发布 |
| 项目应收集哪些资料 | `standards.yaml` | 资料分析逐项核对，缺的写进「缺口与待决」 |
| 抽取时的判断要点 | `reference/*.md` | 抽取步骤开始前由建模助手阅读 |
| 常见对象、操作的候选写法 | `library/` | 建模助手当候选参考，不直接复制 |
| 完整的小型示例 | `examples/<id>/` | 只随包复制，平台不展示，提示词不引用 |

每一项都能省略，但没有 `concepts.yaml` 时，只有 `global` 范围的知识条目能被加载。

## 2. 写清单

要改 `name`、`label`、`description`、`changelog`，其余照抄骨架：`kind: knowledge`、`publisher: {kind: tenant, label: ""}`、`default_on: false`、`kernel_api: ">=1.0 <2.0"`、`engines: [claude, jiuwen]`、`depends: {core: ">=0.2"}`、`checksums: checksums.sha256`。校验和文件由 `pack_tool.py check` 或 `build` 生成，不手写。

```yaml
name: field-inspection
version: 0.1.0
category: industry
label: 设备巡检
contributes:
  knowledge_dir: knowledge
  concepts: concepts.yaml
  checks: rules.yaml
  standards: standards.yaml
  reference_dir: reference
  library_dir: library
  examples: [inspection-mini]
```

`label` 显示在知识页分层标题、加载结果的来源注记和检查提示里，`description`、`changelog` 显示在插件中心。

| `contributes` 字段 | 程序是否读取 | 写法 |
|---|---|---|
| `knowledge_dir` | 读 | 包内相对路径；知识条目从这里读 |
| `concepts` | 读 | 文件路径；装配时另存为 `concepts.json` |
| `checks` | 读 | 文件路径；**检查规则写在这里**，装配时另存为 `checks.json` |
| `standards` | 读 | 文件路径；装配时另存为 `standards.json` |
| `reference_dir`、`library_dir` | 不读 | 写 `reference`、`library`。提示词固定找这两个目录，改名后建模助手找不到 |
| `examples` | 不读 | `examples/` 下的子目录名列表 |

不要写 `contributes.rules`（它是拼进 `CLAUDE.md` 的片段列表，把 `rules.yaml` 写进去后 YAML 原文进 `CLAUDE.md`，`checks.json` 不生成，检查规则静默失效）和 `contributes.reference`（能力包的参考指南名字列表，不接受带 `/` 的路径）。

`concepts`、`checks`、`standards` 指向的文件不存在时，上传和启用都不报错，这部分内容静默缺失。

## 3. 写概念表

`concepts.yaml`（`concepts_api` 目前没有程序校验，写 1）：

```yaml
concepts_api: 1
concepts:
  - name: 巡检记录
    aliases: [巡检单, 点检单]
    description: 一次巡检的结果，记录被巡检设备、巡检时间和判定结论。
```

`name` 是标准称谓，知识条目的 `concept:` 和规则的 `match.concept` 都写它，不写别名。`aliases` 是资料里的其他叫法。

请求 `object:<名>` 时，名字等于概念名或别名，或包含概念名才命中。以概念「设备」、别名「机台」为例：`object:设备`、`object:机台`、`object:设备台账` 命中，`object:老机台` 不命中。概念名别取太短，否则名字相近的对象都会加载它的知识条目。

## 4. 写知识条目

文件名 `<type>-<slug>.md`，放在 `knowledge/`：

```markdown
---
name: 距上次巡检超过周期 1.5 倍的设备判定为逾期
type: domain-rule
slug: overdue-inspection
scopes:
  - "concept:设备"
  - "concept:巡检记录"
source: field-inspection
confidence: high
revisions: []
related: []
---
## 背景

巡检周期按设备类别设定，现场允许有限顺延。

## 规则

距上次巡检的时长超过巡检周期的 1.5 倍时判定为逾期。

**Why:** 各车间单独约定顺延比例时，逾期率无法横向比较。

**How to apply:** 抽取巡检相关的操作和规则时按这个口径判定逾期。
```

| 字段 | 写法 | 说明 |
|---|---|---|
| `name` | 一句结论 | 知识页标题和加载结果的小标题 |
| `type` | `correction`、`convention`、`terminology`、`domain-rule` | 缺失时 `load` 退出码 1；未知值在知识页按 `convention` 显示 |
| `slug` | 小写英文连字符 | 项目里同 slug 的条目覆盖它 |
| `confidence` | `high`、`medium`、`low` | 高置信是硬约束，中低置信是软提醒；非法值按 `medium` |
| `source`、`revisions`、`related` | 包名、`[]`、`[]` | |

| `scopes` 写法 | 结果 |
|---|---|
| `- "concept:巡检记录"` | 请求范围命中该概念时加载 |
| `- global` | 每次 `load` 都加载且不限量，资料分析也能拿到；只给确实全局成立的少数条目用 |
| `- "concept:点检单"`（写了别名） | 不加载 |
| `- "object:巡检记录"` | 行业层不加载 |
| `scopes: ["concept:设备"]`（行内列表） | 不加载，也不报错 |
| `- concept:设备`（没有双引号） | `load` 退出码 1（`AttributeError`），这次加载的知识全部拿不到，项目自己的条目也一样 |

条目 id 是 `<包名>:<文件名去掉 .md>`。正文的 `## 背景`、`## 规则` 显示为知识详情的 `background_md`、`rule_md`。开头不是 `---` 的文件会被知识页和 `load` 静默跳过。

非 `global` 条目单次最多 10 条，按包名、文件名排序，同一概念下堆太多时排在后面的读不到。行业条目只读（`readonly: true`），知识页删除返回 403 `artifact_readonly`。项目要改口径时在 `ontology/knowledge/` 写同 slug 的条目，项目条目优先。

## 5. 写检查规则

`rules.yaml`，在清单里写到 `contributes.checks`：

```yaml
rules_api: 1
rules:
  - id: inspection-needs-device
    title: 巡检记录要关联设备
    applies_to: object
    match: {concept: 巡检记录}
    require_relations_to: [设备]
    level: warning
    message: 巡检记录必须能追溯到被巡检的设备。
```

每条规则写下面四类中的一类。四类都没写的规则被忽略，不报错。

| 类型和例子 | 检查内容 | 适用条目 |
|---|---|---|
| `require_relations_to: [设备]` | 「关联对象」里有指向该概念（含别名）的关系 | 只对象 |
| `require_fields: [等级, 发现时间]` | 属性中文名或英文名包含该词 | 只对象 |
| `require_sections: [输入, 输出]` | 有标题包含该词 | 全部 |
| `prefer_terms: {点检单: 巡检记录}` | 正文出现非标准称谓，标题含「别名」的小节除外 | 全部 |

- `applies_to`：`object`、`operation`、`rule`、`metric`、`any`，缺省 `any`。`level`：`warning` 显示「警告」，`suggestion` 显示「建议」。
- `match.concept`：按产物文件名匹配，文件名等于概念名或别名，或包含长度大于 1 的概念名或别名。不写时对该类型全部条目生效。
- 提示格式：`{title}（{label} {id}）：{问题}。{message}`，出现在检查结果的「行业规则」分类。

`require_relations_to` 看关系行的目标。`- 巡检记录 → 针对（device_no → device_no） → [[设备]]` 才算指向设备，`[[设备]] → 包含 → 巡检记录` 的目标是当前对象自身，不算。

## 6. 写资料清单

`standards.yaml` 列出这个领域应该有哪些资料：

```yaml
standards_api: 1
standards:
  - title: 巡检制度
    purpose: 确认各类设备的巡检周期、顺延比例和判定标准。
    note: 示例性规范，以项目实际资料为准。
```

资料分析助手读 `standards.json` 逐项核对项目资料，缺的写进研究报告的「缺口与待决」，并在「信息来源」注明包名和版本。

## 7. 写抽取提示

`reference/` 下每个文件开头声明对应的抽取步骤，正文写短句，只写抽取时才需要的判断：

```markdown
---
decision_point: object_extraction
title: 设备巡检的对象抽取提示
---

- 设备、巡检记录、缺陷各建一个对象。一次巡检可能发现多条缺陷。
```

`decision_point` 取值：`object_extraction`（抽对象，习惯文件名 `object-extraction.md`）、`operation_extraction`（抽操作，`operation-extraction.md`）、`rule_metric`（抽规则和指标，`rule-metric.md`）。这些文件靠提示词引导建模助手阅读，没有程序解析 `decision_point`，也不进知识页。

## 8. 写候选片段

`library/object/<对象>.md`、`library/operation/<操作>.md` 按项目产物的模板写，建模助手对照项目资料改写后才进 `ontology/`。骨架里有完整写法，要点在这几个章节：

```markdown
# 数据来源
无
> 候选片段，表名按项目资料填写。

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
|---|---|---|---|---|---|---|
| 判定结论 | verdict | 本次巡检的结论 | 否 | 枚举 | | 取值：正常 / 异常 |

# 关联对象
- 巡检记录 → 针对（device_no → device_no） → [[设备]]
```

- 对象章节依次是 `# 对象描述`、`# 对象别名`、`# 英文名称`、`# 数据来源`、`# 查询边界`、`# 对象属性清单`、`# 关联对象`；操作章节依次是 `# 业务描述`、`# 与operation的关联`、`# 与object的关联`、`# 与object的语义关联`、`# 触发条件`、`# 输入`、`# 输出`、`# 逻辑描述`。
- `# 数据来源` 首行只写表名或「无」，补充说明另起一行以 `> ` 开头。表名必须在项目 schema 里存在，否则发布被阻断，所以行业包一般写「无」。
- 类型只填 `字符串`、`整数`、`小数`、`布尔`、`日期`、`枚举`、`对象引用`；枚举写 `取值：A / B / C`，编码写 `取值：1=是 / 0=否`。
- 跨表关系写括注字段对，关系名不含两端对象名。

## 9. 写示例工作区

`examples/<id>/` 放 `README.md`、`research/*.md`、`ontology/object|operation|rule/*.md`，可以省略。平台只把它复制到 `.claude/industry/<包名>/examples/`，实际用途是给开发者自测：把其中的 `ontology/` 放进项目后跑 `ontology_checker`，确认检查规则不误报。骨架的 `inspection-mini` 这样跑时「行业规则」分类没有提示。

## 10. 查看装配结果

启用后所有文件复制到项目的 `.claude/industry/<包名>/`，另外生成 `pack.json`（名称、版本、label、category 和各文件的指向）和 `concepts.json`、`checks.json`、`standards.json`。`.claude` 软链到装配目录，内容不要改。

| 读取方 | 读什么 | 时机 |
|---|---|---|
| `GET /api/v1/projects/{id}/knowledge?layer=industry` | `knowledge/` | 知识页；详情 `GET .../knowledge/{id}` |
| `knowledge_helper.py load --scope object:<名>` | `knowledge/`、`concepts.json` | 抽取、校验、更新、概览步骤；资料分析只用 `--scope global` |
| `ontology_checker.py ontology --json` | `checks.json`、`concepts.json` | 校验步骤 |
| 资料分析助手 | `standards.json`、`concepts.json`、`pack.json` | 资料分析 |
| 抽取步骤的建模助手 | `reference/`、`library/` | 抽取前 |
| `GET /api/v1/packs/{name}` | 清单的展示字段 | 插件中心；知识包的 `contents` 为空属于正常 |

## 11. 跑打包前自检

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

## 12. 上传并验证启用效果

1. 打包：`python3 <skill>/scripts/pack_tool.py build <包目录> --out dist`。
2. 上传：`POST /api/v1/me/pack-uploads`（multipart 字段 `file`），返回 202 和 `id`。`GET /api/v1/me/pack-uploads/{id}` 的 `state` 是 `passed` 或 `failed`，`checks` 列出各项；`GET .../{id}/report` 是 Markdown 报告。
3. 安装：`POST /api/v1/me/pack-uploads/{id}/install`，body `{}`。
4. 启用：`POST /api/v1/projects/{id}/packs`，body `{"name": "<包名>"}`，返回 201 且 `effective` 为 `immediate`。新建项目时也可以传 `industry_packs: ["<包名>"]`。

以骨架内容为例，启用后这样确认：

| 确认什么 | 调用 | 预期 |
|---|---|---|
| 知识条目 | `GET /api/v1/projects/{id}/knowledge?layer=industry` | `items` 有 `<包名>:domain-rule-overdue-inspection`，`readonly` 为 true，`source.label` 为 `设备巡检 0.1.0` |
| 概念和加载 | 项目根目录运行 `python3 .claude/scripts/knowledge_helper.py load --scope object:巡检记录` | 输出含条目标题和 `来源：设备巡检（行业包，项目里同名条目优先）`；`--scope object:完全无关` 输出 `（暂无相关业务知识）` |
| 检查规则 | 写一个没有关联的 `ontology/object/巡检记录.md`，运行 `python3 .claude/scripts/ontology_checker.py ontology --json` | 有一项 `category` 为 `行业规则`，`message` 含 `巡检记录要关联设备` |

升级：改内容后升 `version`，再上传、安装。已启用的项目不会自动更新，要调用 `POST /api/v1/projects/{id}/packs/{name}/upgrade`，body `{"to_version": "<新版本>"}`，立即生效。

## 13. 排查常见失败

| 现象 | 原因 | 修法 |
|---|---|---|
| 上传 400 `pack_upload_invalid`，`checksums_missing` | zip 里没有校验和文件 | 用 `pack_tool.py build` 打包 |
| `manifest`：`contributes.rules 必须是列表` | 把 `rules.yaml` 写到了 `rules` | 改写到 `checks` |
| `compat`：`依赖 core … 没有满足的版本` | `depends` 版本范围太高 | 写 `core: ">=0.2"` |
| `names`：`<包名> <版本> 已在包仓库里`；`private_name`、`pack_name_taken` | 同版本重复上传；包名已被占用 | 升 `version`；换包名 |
| `frontend`：`ui/ 之外有前端代码：<文件>` | 有 `.css`、`.js`、`.ts` 等文件，HTML 里有脚本，或路径含 `public`、`static`、`web`、`dist`、`frontend` 目录 | 改成 Markdown，目录换名 |
| `secrets`：`疑似凭据：<文件>` | 正文有 `password = "…"`、带账号密码的连接串、密钥 | 换成 `${API_TOKEN}` 这类占位 |
| 启用 409 `assembly_failed`，`details.reason` 写 `<文件> 无法解析` | YAML 语法错误，静态检查查不到 | 修 YAML，升版本重新上传，项目保持原版本 |
| `load` 退出码 1：`'dict' object has no attribute 'startswith'` 或 `KeyError: 'type'` | `scopes` 没加双引号，或条目缺 `type` | 改成 `- "concept:<名>"`，或补字段 |
| 知识页有条目，`load` 读不到 | `scopes` 写了别名、`object:` 或行内列表；`concepts` 字段缺失或路径错 | 对照第 3、4 节 |
| 知识页没有条目 | `knowledge_dir` 路径错，或文件开头不是 `---` | 改路径或补字段 |
| 检查结果没有「行业规则」 | 规则写到 `contributes.rules`；`rules` 写成字典；缺四类字段；`match.concept` 没匹配到文件名 | 对照第 5 节 |
| 发布被阻断，提示表名不存在 | 从 `library/` 抄来的表名不在项目 schema 里 | 导入 schema 或把数据来源改成「无」 |
| 新建项目 404 `pack_not_found` | `industry_packs` 里放了 `category` 不符的包，或别人的私有包 | 核对 `category` |

包里只放合成内容或有权使用的公开资料，不放真实客户名、内部地址、凭据和真实业务数据，示例域名只用 `example.com`。静态检查只挡凭据形态的内容，其余靠开发者自查。
