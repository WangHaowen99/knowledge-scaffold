# 数据源：接口与格式（ADR-0104）

决策见 ADR-0104。本文是连接器插件、连接器网关、内核三方之间的接口与格式。改这里要同时改三方和契约
（`docs/api-v1.md` 3.16 PL-28、PL-29，3.19a DS-01～DS-04）。

```
插件中心「信任」──PL-28──▶ 内核：记信任 ──tar + 票据──▶ 网关 PUT /v1/plugins/{名}/{版本} ──▶ 插件卷
资料页「接入数据源」──DS-02──▶ 内核：凭据 ──票据──▶ 网关 PUT /v1/secrets/{项目}/{引用}
                              └─ 写 resource/excel/<名称>.datasource（无凭据）──▶ 资料转换队列
资料转换队列 ──票据 + 配置──▶ 网关 POST /v1/sources/collect ──▶ 子进程 collect(ctx) ──事件──▶ 网关：遮蔽、限量、渲染
          ◀── 文本版 Markdown（工作表 + 库表事实）── 内核写 resource/text/excel/<名称>.datasource.md
助手：table_profile.py --headers（含 foreign_keys）、文本版
```

## 1. 连接器插件

和转换器一样是 `kind: provider` 的包。清单顶层写 `source` 段，不放进 `contributes`。一个包里 `converter`、`source` 二选一。

```yaml
name: db-mysql
version: 0.1.0
kind: provider
category: provider
label: MySQL
description: 读取表结构、外键、约束和遮蔽后的样本。
publisher: {kind: official, label: 平台}
kernel_api: ">=1.0 <2.0"
source:
  entry: connector.py
  license: PyMySQL（MIT）
  vendor: vendor            # 可选：随包带的纯 Python 依赖目录
  fields:
    - {key: host, label: 主机, type: host, required: true}
    - {key: port, label: 端口, type: port, default: 3306}
    - {key: database, label: 数据库, type: text, required: true}
    - {key: user, label: 账号, type: text, required: true}
    - {key: password, label: 密码, type: secret, required: true}
    - {key: tables, label: 表范围, type: text, placeholder: "orders, customer*"}
```

**字段**（`fields`，1～20 个）：

| 键 | 说明 |
|---|---|
| `key` | `^[a-z][a-z0-9_]{0,31}$`，包内不重复 |
| `label` | 1～20 字 |
| `type` | `text`、`number`、`host`、`port`、`secret`、`select`；`host`、`port` 各最多一个，出口白名单取这两个字段的值 |
| `required` | 默认 false |
| `default` | 字符串或整数，`secret` 不能有 |
| `placeholder` | ≤ 60 字 |
| `options` | `select` 必填：`[{value, label}]`，1～20 项 |

`secret` 类字段的值只进网关的凭据库，`config` 里不出现；其余字段进数据源资料（助手看得到）。

**入口**：`entry` 是包根目录下单个 `.py`（同转换器的写法），导出两个函数：

```python
def test(ctx) -> None:      # 连接、自检，用 ctx.check 报每一项
def collect(ctx) -> None:   # 发表和事实，用 ctx.table、ctx.fact
```

**`ctx`（`app.connector_gateway.source.sdk.SourceContext`）**：

| 成员 | 说明 |
|---|---|
| `config` | 非密钥字段，已按字段类型转好（`port`、`number` 是整数），缺省值已补 |
| `secret` | 密钥字段 `{key: 值}`；不要写进任何事件、异常消息 |
| `check(name, ok, detail="")` | `test` 用：一项检查的结果；有任何一项 `ok=False` 就算测试没通过 |
| `table(name, columns, rows=(), comment="")` | `collect` 用：一张表。`columns` 是 `[{name, type, nullable, key, comment}]`（`key` 取 `pk`、`fk`、`uk` 或空）；`rows` 是和 `columns` 对齐的原始取值，最多送 200 行（宽表按 2 万格折算）供网关识别敏感列和码值，遮蔽后只留前 20 行落盘 |
| `fact(kind, **fields)` | `collect` 用：一条库表事实，`kind` 见第 3 节 |
| `progress(done, total)` | 进度 |
| `limitation(text)` | 局限，原样写进文本版 |
| `throttle()` | 每次查业务系统前调：限速，并检查截止时间 |
| `query_timeout_s()` | 单条语句的超时（不超过剩余时间） |
| `readonly(sql)` | 语句过词法白名单：单条，首个关键字是 `SELECT`、`SHOW`、`DESCRIBE`、`EXPLAIN`、`WITH`，不含写关键字；不过就抛错 |
| `in_scope(name)` | 表名在不在 `config` 的范围里（字段 `tables`：逗号分隔，`*` 只在末尾；没填就是全部） |
| `fail(code, message)` | 抛 `SourceError`，任务失败；`message` 里的凭据取值会被遮蔽 |

插件 `import` 的只有 `app.connector_gateway.source.sdk`（`SourceError`）和自己 `vendor/` 里的包。

**限制**（网关主进程执行，插件绕不过）：

- 每表样本最多 20 行，单元格最多 200 字；
- 最多 500 张表、5000 条事实；
- 输出合计 16 MiB；
- 采集任务限时 600 秒（`ONTO_CONNECTOR_SOURCE_TIMEOUT_S`），测试限时 30 秒；
- 子进程内存上限同转换器。

超出时截到上限，并写进局限。

## 2. 遮蔽（`mask-v1`）

网关主进程在落盘前，对 `table` 事件的样本行逐列处理。插件给的 `rows` 不直接落盘。

| 类别 | 识别（列名或注释，中英文；或取值正则） | 处置 |
|---|---|---|
| 口令、令牌、密钥 | `password`、`pwd`、`secret`、`token`、`salt`、`密码` | 整列丢弃 |
| 手机号 | 列名，或取值 `1[3-9]\d{9}` | 留前 3 位、后 4 位 |
| 证件号 | `id_card`、`身份证`、`证件`；取值是 18 位证件号 | 留前 2 位、后 2 位 |
| 姓名 | 人员前缀的 `*_name`、`姓名`、`联系人` | 留首字 |
| 地址、邮箱、银行卡、生日 | 列名或取值正则 | 掩码 |
| 金额 | `amount`、`price`、`balance`、`salary`、`金额`、`余额` | 按数量级分桶 |
| 关联键 | `key` 是 `pk`、`fk` 的列 | 加盐哈希，取前 12 位（盐按项目，网关由主密钥派生，同项目内各表一致，能看出谁关联谁） |
| 判断不了的文本 | 文本类型、取值不是短代码 | 掩码 |

每张表丢弃了哪些列、遮蔽了哪些列，都写进库表事实里的「列」。之后所有单元格再过一遍个人信息正则，命中的整格换成 `«已遮蔽»`。

不敏感、低基数（送来的行里不超过 20 个取值、重复过半、都不超过 32 字）的列，网关自动补一条「码值」事实；插件自己给了的不重复。

## 3. 文本版

位置和文件头同 ADR-0073：`resource/text/excel/<名称>.datasource.md`，第一行是内核写的 `onto:text` 文件头。
正文由网关渲染，插件不写 Markdown。

```markdown
<!-- 工作表：orders -->
## orders

| id | customer_id | status | amount |
| --- | --- | --- | --- |
| 3f2a9c1e0b7d | 9a0c… | PAID | 1e2～1e3 |

<!-- 库表事实 -->
## 列
| 表 | 列 | 类型 | 可空 | 键 | 注释 | 遮蔽 |
## 主键
| 表 | 列 |
## 外键
| 表 | 列 | 引用表 | 引用列 | 名称 |
## 唯一约束
## 检查约束
| 表 | 名称 | 表达式 |
## 码值
| 表 | 列 | 取值 |
## 视图
## 存储过程
## 触发器
## 局限
- ……

<!-- onto:anchors 1 … -->
<!-- onto:facts 1
{"foreign_keys": [{"table": "orders", "columns": ["customer_id"], "ref_table": "customer", "ref_columns": ["id"], "name": "fk_orders_customer"}], …}
-->
```

- 一张表一个工作表：首行是列名，下面是遮蔽后的样本行，和 Excel 文本版同一格式。`table_sources` 按 Excel 文本版的规则判断数据表、
  选 Sheet 范围，发布时的表列绑定用的就是工作表名和首行。
- 「库表事实」各节只在有内容时出现，节里的表格不算工作表（`table_sources` 每个工作表只取标记后的第一张表）。
- 事实类型（`ctx.fact(kind, …)`）：

  | `kind` | 字段 |
  |---|---|
  | `primary_key` | `table`、`columns` |
  | `foreign_key` | `table`、`columns`、`ref_table`、`ref_columns`、`name` |
  | `reference` | 同 `foreign_key`，另有 `basis`：没有外键约束，按命名或取值推断 |
  | `unique` | `table`、`columns`、`name` |
  | `check` | `table`、`name`、`expression` |
  | `values` | `table`、`column`、`values`（≤ 20 个，低基数且不敏感的列） |
  | `view` | `name`、`definition` |
  | `routine` | `name`、`type`（`procedure` / `function`）、`signature`、`definition`（截到 2000 字） |
  | `trigger` | `table`、`name`、`timing`、`event`、`definition` |

  其他 `kind` 网关拒收（`invalid_event`）；字段里不能再有 `kind`。表注释用 `table(comment=…)`，列注释在 `columns` 里。
  外键和推断关联两端的列，遮蔽时都按关联键加盐哈希。
- `onto:facts` 注释块是给脚本读的同一份事实（JSON）。工具箱只从数据源资料的文本版里读它，别的文本版里出现同样字样不认。
- 锚点同 Excel：`file:resource/excel/<名称>.datasource#sheet:orders!B2`。

## 4. 数据源资料

`resource/excel/<名称>.datasource`，UTF-8 JSON，由内核经写入路径写，助手只读：

```json
{"format": "onto-datasource/1", "connector": "db-mysql",
 "config": {"host": "db.example.invalid", "port": 3306, "database": "shop", "user": "reader", "tables": ""},
 "secret": "ds-1f2e3d4c5b6a", "secret_fields": ["password"]}
```

- `名称`：用户填写，同资料文件名规则，不能和已有资料重名；
- `secret`：凭据引用（网关凭据库里的键），不是凭据；`secret_fields`：存过的密钥字段名；
- 用途预填「数据 Schema」，全部数据表进 Sheet 范围（文本版出来之前是 pending，同 Excel）；
- 删除这份资料时，内核顺带删网关里的凭据（尽力而为，失败只记日志）。

## 5. 网关接口

鉴权同 ADR-0073：内核签的一次性票据，`Authorization: Bearer <票据>`。票据的 `scope` 是请求体的 sha256；DELETE 没有请求体，
`scope` 是下表「范围串」的 sha256。

| 接口 | 票据 `op` | 说明 |
|---|---|---|
| `GET /v1/providers` | — | `items`（转换器，同前）、`sources`（`[{id, version, label, description, fields, license, official}]`）、`plugins`（`[{name, version, origin: builtin\|installed, kind: converter\|source, sha256}]`） |
| `PUT /v1/plugins/{名}/{版本}` | `plugin_install` | 请求体是这个版本的 tar（第 6 节）；校验后装进插件卷，刷新清单。已装同一 sha256 回 200，不同 sha256 回 409 `plugin_conflict` |
| `DELETE /v1/plugins/{名}/{版本}` | `plugin_remove` | 范围串 `<名>@<版本>`；内置的删不了（409 `plugin_builtin`） |
| `PUT /v1/secrets/{项目}/{引用}` | `secret` | 请求体 `{"fields": {...}}`，和已存的合并（给的字段覆盖）；回 `{secret_fields}` |
| `DELETE /v1/secrets/{项目}/{引用}` | `secret` | 范围串 `delete:<项目>/<引用>` |
| `POST /v1/sources/test` | `source_test` | 请求体 `{project, connector, config, secret?, secrets?}`：`secret` 是已存的引用，`secrets` 是这次填的（覆盖已存的同名字段，不保存）；同步跑 `test`；回 `{ok, checks: [{name, ok, detail}], error_code?, error_message?}` |
| `POST /v1/sources/collect` | `source_collect` | 请求体 `{project, connector, config, secret}`；同步跑 `collect`；回 `{state: converted\|failed, markdown, stats: {tables, facts, rows}, limitations, error_code?, error_message?, connector: {id, version}}` |

- 票据另写 `project`，网关核对请求体里的 `project` 和路径里的项目都等于它，只读这个项目下的凭据。
- 网关先核对 `config` 里的 `host`、`port`（字段类型决定），作为子进程的出口白名单；`host` 不能是网关自己、内核或内部网络里的服务名（`egress_forbidden`）。
- 错误码：`ticket_invalid`、`issuer_unavailable`、`busy`、`plugin_not_found`、`plugin_invalid`、`plugin_conflict`、`plugin_builtin`、
  `connector_not_found`、`secret_missing`、`config_invalid`、`egress_forbidden`，以及子进程报的错误码
  （`connect_failed`、`auth_failed`、`account_not_readonly`、`timeout`、`egress_denied`、`exec_denied`、`memory`、`crashed`、`invalid_event`）。

## 6. 插件仓库与交付

- 网关里两处：内置目录 `ONTO_CONNECTOR_CONVERTERS_DIR`（默认 `/opt/connector/converters`，镜像里只读，放 `assets/packs` 里全部
  `kind: provider` 的包），插件卷 `ONTO_CONNECTOR_PLUGINS_DIR`（默认 `/var/lib/connector/plugins`，布局 `<名>/<版本>/`）。
- 同一个名字有多个版本时，用版本号最大的那个。内置和插件卷里都有时，同样比版本号。
- 内核交付的 tar：
  - 只包含这个版本目录里的普通文件，路径相对版本目录，按路径排序，`mtime=0`、属主 0，不压缩；
  - 网关解压前核对：单个最多 32 MiB、最多 2000 个文件；不能有绝对路径、`..`、链接或设备文件；
  - 清单的 `name`、`version` 要和路径一致，`kind: provider`，`converter`、`source` 二选一，写法合法；`checksums.sha256` 逐个对得上；
  - 先解压到临时目录，全部通过后改名进位，半份包不会出现。
- 内核对账：每次刷新网关的插件清单（30 秒缓存）时，比对「已信任的版本」和网关的 `plugins`，缺的交付、多的删除，在后台线程里做，
  失败只记日志、下一轮再试。

## 7. 凭据

- 存放：`ONTO_CONNECTOR_SECRETS_DIR`（默认 `/var/lib/connector/secrets`，卷 `connector-secrets`，只有网关挂），文件 `<项目>/<引用>.bin`，
  AES-256-GCM 加密，附加数据绑定 `<项目>/<引用>`。
- 主密钥：`ONTO_CONNECTOR_MASTER_KEY_FILE`（32 字节，base64）；没配时首次启动在凭据目录生成 `.master.key`（0600）。
- 遮蔽用的盐：`HMAC-SHA256(主密钥, "mask-v1:" + 项目)`，不出网关。
- 只有写、删接口，没有读出明文的接口。凭据只在子进程的标准输入里传一次。

## 8. 子进程

`python -m app.connector_gateway.source.worker`：

- 标准输入：第一行 JSON 头 `{entry, vendor, operation, config, egress, limits}`，第二行凭据 JSON，然后关闭。
- 标准输出：一行一个事件：`check`、`table`、`fact`（事实放在 `fact` 键下）、`progress`、`limitation`、`error`、`done`。
- 启动顺序：设资源上限 → 解析出口主机 → 装审计钩子（只放行出口白名单里的主机端口，不许起子进程）→ 把 `vendor/` 放进 `sys.path`
  → 导入入口 → 调 `test` 或 `collect`。
- 插件 `print` 的内容进标准错误。异常只报类型名；`SourceError` 报它的错误码，消息里的凭据取值遮掉。
- 审计钩子是纵深防御，不是沙箱。信任插件的人要对它的代码负责（ADR-0104「信任方式的代价」）。

## 9. 内核

- 信任（PL-28、PL-29，管理员）：信任记在包的通道记录上 `trusted: {versions: {<版本>: {sha256, by, at}}}`，sha256 是第 6 节 tar 的哈希；
  官方内置的包不用信任。撤销信任就从网关删掉这个版本。
- 连接器列表（DS-01）：网关 `sources` 和包仓库合起来，只列网关里已加载的。
- 接入（DS-02）：校验字段 → 生成引用 → 写凭据到网关 → 写数据源资料 → 进资料转换队列。写凭据失败就不写资料。
- 修改（DS-03）：`config` 整份替换；`secrets` 里给了的字段覆盖，没给的保持不变；连接器不能换。
- 测试（DS-04）：同步调网关 `test`，最长 40 秒。
- 采集走资料转换队列：资料类型 `source`（`kind_of`），「重新转换」（R-14）就是重新采集；网关不可用时记 `waiting`，
  恢复后自动补；启动时补采还没有文本版的数据源资料。
- 内核条件 `data_sources`：项目里有数据源资料。
