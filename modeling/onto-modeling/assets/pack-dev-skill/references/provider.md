# 连接器包（provider）：文件转换器与数据源连接器

**结论先说**：连接器包有两种，清单里二选一：

- **文件转换器**（`converter` 段）：把用户上传的资料转成 Markdown 文本版；
- **数据源连接器**（`source` 段，第 12 节）：连一种业务系统，把表结构、关系、样本采成一份资料的文本版。

两种都跑在连接器网关的子进程里。包上传、安装后，要**平台管理员在插件中心「信任」这个版本**，平台才把它装进连接器网关，
下一个任务就用上；不用重建镜像、不用重启。助手交付时必须把「要管理员信任」这一点告诉用户（第 11 节）。

## 1. 包结构

骨架是 `references/examples/sample-convert/`（`.kvtxt` 键值文本 → 两列表格，只用标准库）。`pack_tool.py init <名字>` 复制它并改名。

```
<名字>/
  manifest.yaml   清单，必须带 converter 段
  converter.py    转换器入口，提供 convert()
```

- `checksums.sha256` 由 `pack_tool.py check` 或 `build` 生成，不手写。
- 连接器包不带 commands、agents、skills、scripts、ui、settings.json。
- 改完名字后要改的：`label`、`description`、`converter.types`、`converter.license`、`convert()` 的解析逻辑。

## 2. 清单

| 字段 | 写法 | 说明 |
|---|---|---|
| `name` | 小写字母开头，2～40 位小写字母、数字、连字符 | 必须等于目录名，网关按目录名加载 |
| `version` | 语义化版本，如 `0.1.0` | 同名同版本不能重复上传 |
| `kind` / `category` | `provider` / `provider` | |
| `label` / `description` | 中文短句 | 插件卡片上显示 |
| `publisher` | `{kind: tenant, label: ""}` | 官方身份只认镜像内置，清单写 official 也不算 |
| `kernel_api` | `">=1.0 <2.0"` | 取值见 protocol.md |
| `converter.entry` | `converter.py` | 包根目录下单个 `.py`，匹配 `^[A-Za-z0-9_][A-Za-z0-9_.-]*\.py$` |
| `converter.types` | `[".kvtxt"]` | 至少一个；小写、带点、点后 1～8 位字母或数字 |
| `converter.license` | 用到的库与许可证 | 只用 MIT、BSD、Apache 一类，不用 AGPL（如 PyMuPDF） |

插件目录只列 `kind: provider` **且**带 `converter` 或 `source` 段的包；两段都没有的 provider 包过不了静态检查。

## 3. 转换函数

```python
def convert(data: bytes, filename: str, limits: ConvertLimits) -> Conversion: ...
```

| 参数 | 内容 |
|---|---|
| `data` | 原文件全部字节（资料上传上限 20 MiB） |
| `filename` | 原文件名，如 `设备清单.kvtxt`；只用来判断或写说明，不是可读的路径 |
| `limits` | `ConvertLimits`，网关总是传默认值：`max_pages=1000`、`max_rows=2000`、`max_sheet_cells=100000`、`max_output_chars=4000000`、`max_expanded_bytes=100 MiB`、`max_zip_members=5000` |

返回值必须是 `Conversion` 对象（网关调用它的 `to_dict()`）。返回 dict、None 或别的对象一律记「转换出错」。

| 字段 | 类型 | 说明 |
|---|---|---|
| `state` | `"converted"` 或 `"unsupported"` | 其他值记「转换出错」 |
| `markdown` | str | converted 时的正文 |
| `locations` | list | 位置对照：用 `parse_locations(markdown)` 生成；没有位置标记时留空 |
| `reason` | str 或 None | unsupported 的原因，给用户看，一句话 |
| `note` | str 或 None | 转换说明，如「只转了前 2000 行（共 3500 行）」 |
| `stats` | dict | 计数，值必须能 JSON 序列化 |

三种结局：

| 情况 | 转换器的写法 | 资料状态 |
|---|---|---|
| 转成了 | `return Conversion(state="converted", markdown=..., note=..., stats=...)` | 已转成文本 |
| 文件没坏，但这份转不了（扫描件、加密、没内容） | `return Conversion(state="unsupported", reason="...")` | 暂不支持 |
| 坏文件、超限 | `raise ConvertError("corrupt")` 等 | 转换失败 |
| 其他异常、超时、内存超限、进程崩溃 | 不用处理，网关判定 | 转换失败 |

`ConvertError` 的 code 只能取下面这些，其他值按 `crashed` 处理：

| code | 用户看到的原因 |
|---|---|
| `corrupt` | 文件损坏，打不开 |
| `too_large` | 解压后内容超过上限 |
| `timeout` | 转换超时 |
| `memory` | 转换时内存超限 |
| `output_too_large` | 转换结果超过上限 |
| `crashed` | 转换出错 |

Markdown 的写法：

- 不写文件头。内核在第一行加 `<!-- onto:text source=… sha256=… converter=… -->`；正文里以 `<!-- onto:text ` 开头的行会被删掉。
- 有页的格式在每页前一行写 `page_marker(n)`（得到 `<!-- 第 3 页 -->`），有工作表的写 `sheet_marker(名)`（得到 `<!-- 工作表：客户 -->`），再用 `parse_locations(markdown)` 填 `locations`。助手引用出处时写「第 3 页」靠的就是这些标记。没有页的格式（Word、键值文本）不写标记，用 `#` 标题分节。
- 表格用 Markdown 表格：`md_table(rows)`，第一行是表头，单元格里的 `|` 和换行会被转义。
- 正文长度不超过 `limits.max_output_chars`（`cap_output(markdown, limits)` 返回截断后的文本和是否截断），截断了在 `note` 里说明。

## 4. 能 import 什么

| 来源 | 内容 |
|---|---|
| 标准库 | Python 3.11 |
| 平台协议 `app.evidence.convert`（只在网关里有） | `Conversion`、`ConvertError`、`ConvertLimits`、`Location`、`page_marker`、`sheet_marker`、`parse_locations`、`md_table`、`md_cell`、`cap_output`、`check_ooxml`（zip 成员数与解压大小检查）、`unsupported(reason, **stats)` |
| 网关镜像里已装的库 | `pdfplumber` 0.11（带 pdfminer.six、pypdfium2）、`python-docx` 1.x、`openpyxl` 3.1+、`pyyaml` 6+、`pyjwt` |

- 入口按文件路径导入，包目录不在 `sys.path` 上：包里第二个 `.py` 文件 import 不到，转换器写成单文件。
- 样例用 `try/except ImportError` 给 `Conversion`、`ConvertError`、`ConvertLimits` 定义同形状的替身，本地没有平台代码也能自测；网关里导入的是平台的真类（网关只认平台的 `ConvertError`）。保留这个写法，只用到替身里有的三个名字；要用 `md_table`、`page_marker` 这类工具时，在替身里补同样行为的实现。
- 需要别的第三方库：只能由运维写进网关依赖、重建网关镜像（第 9 节），清单 `converter.license` 写明库和许可证。

## 5. 网关怎样发现和调用转换器

1. **发现（plugins）**：网关读两处的清单，只读清单、不导入代码：镜像里的只读目录 `/opt/connector/converters/<名字>/`（官方包），和插件卷 `/var/lib/connector/plugins/<名字>/<版本>/`（管理员信任后平台交付的包）。同名取版本号最大的。`kind` 不是 provider、`converter` 和 `source` 不是二选一、`name` 不等于目录名、入口文件不存在、`types` 不合法的，跳过。信任、撤销信任当场生效，不用重启网关。
2. **调度（server → runner）**：内核为每次转换签一张一次性票据（写明转换器 ID、文件名、原文件 sha256），`POST /v1/convert` 发给网关。网关验票后为这个任务起一个子进程 `python -m app.connector_gateway.worker`，工作目录 `/app`，环境变量只有 `PATH`、`LANG=C.UTF-8`、`PYTHONDONTWRITEBYTECODE=1`。
3. **执行（worker）**：子进程从标准输入读一行 JSON 头（入口路径、文件名、limits、内存与 CPU 上限）和原文件字节，先给自己设资源上限，再按入口路径导入模块（模块名不是包名，`if __name__ == "__main__"` 不会执行），调用 `convert(data, filename, limits)`，把 `{"ok": true, "result": <to_dict()>}` 写到标准输出；`ConvertError` 写成 `{"ok": false, "error": {"code": …, "message": …}}`。
4. **落盘（内核）**：网关回 converted 后，内核去掉伪造的文件头、加上自己的文件头，经写入路径存到 `resource/text/<分组>/<原文件名>.md`，资料状态推给前端。

## 6. 运行限制

| 项 | 上限 | 超了会怎样 |
|---|---|---|
| 墙钟时间 | 120 秒（运维可用 `ONTO_CONNECTOR_JOB_TIMEOUT_S` 调，最大 300） | 杀整组进程，`timeout` |
| CPU 时间 | 墙钟上限 + 5 秒（`RLIMIT_CPU`） | 进程被杀，`crashed` |
| 内存 | 768 MiB 地址空间（`RLIMIT_AS`；`ONTO_CONNECTOR_JOB_MEMORY_MB`） | `memory` |
| 输出 | 标准输出（JSON）16 MiB；正文 400 万字符（`max_output_chars`） | `output_too_large` |
| 输入 | 网关收 24 MiB；资料上传上限 20 MiB | 拒收 |
| 写文件 | 根文件系统只读，只有 `/tmp`（tmpfs）可写，单个文件 16 MiB（`RLIMIT_FSIZE`） | 写入报错 |
| 身份 | 非 root（65534），没有任何 capability，`no-new-privileges` | |
| 网络 | 网关只接内部网络，没有出口：不能下载模型、不能调外部接口 | 连接失败 |
| 并发 | 整个网关同时 2 个转换、8 个排队，容器最多 64 个进程、1.5 GiB 内存 | 排不上的资料「等待转换」 |

`RLIMIT_AS` 限的是虚拟地址空间：一次把大文件全读进内存、或会预留大块内存的库容易撞上，按流式或分块处理。

## 7. 本地自测

```bash
printf '# 合成样例\n设备编号=EQ-001\n额定功率=15 kW\n这行没有等号\n' > /tmp/demo.kvtxt
python3 <名字>/converter.py /tmp/demo.kvtxt
```

期望输出 `{"ok": true, "result": {"state": "converted", "markdown": "| 键 | 值 |\n|---|---|\n| 设备编号 | EQ-001 |\n…", …}}`，`note` 里有「1 行不是「键=值」，没有转」。

按网关的条件再跑一次（最少环境变量、768 MiB、120 秒；Linux）：

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  sh -c 'ulimit -v 786432; ulimit -t 125; timeout 120 python3 <名字>/converter.py /tmp/demo.kvtxt'
```

自查：

- 正常文件、空文件、坏文件（非 UTF-8、截断）各试一次，分别得到 converted、unsupported、`ok: false`。
- 返回的是 `Conversion`；`stats` 能 JSON 序列化；正文不含 `<!-- onto:text ` 开头的行。
- 不联网，不写 `/tmp` 以外的地方，不按 `filename` 去读文件。
- 测试数据全部合成，不放进包里。

## 8. 上传导入后会怎样（转换器）

普通用户在插件中心上传 zip（`POST /api/v1/me/pack-uploads`），检查通过后安装。包只归上传者，`GET /api/v1/packs` 的目录里多一张卡片：

| 卡片字段 | 值 |
|---|---|
| `category` | `provider`（「多源连接器」分类） |
| `converter` | `{types: [".kvtxt"], default_for: [], license: "…"}` |
| `project_status` | `platform_installed`：平台级，不按项目启用 |
| `publisher` | `{kind: private, label: 仅自己可见}` |

卡片上还有 `trust`：`{state: untrusted, installed: false}`。在它运行之前：

- 私有包不进平台的转换器候选，`GET /api/v1/converters` 里没有它；这种类型的资料照旧，新类型显示「这种格式暂不支持」，已有类型继续用原来的转换器。
- 管理员把它发布到全平台，并在插件详情点「信任」（PL-28）后，平台把这个版本交给连接器网关；`trust` 变成 `{state: trusted, installed: true}`，
  `GET /api/v1/converters` 里它的 `loaded` 为 true，这种类型的新资料就用它转。撤销信任（PL-29）后网关删掉这个版本。

## 9. 随平台交付：打进连接器网关镜像

只有要把连接器作为官方包随平台交付时才这样做；第三方连接器走第 8 节的「信任」就够了。在平台源码的 `modeling/onto-modeling/` 下：

1. **放入官方包目录**：把包目录放到 `assets/packs/<名字>/`（目录名等于包名；`checksums.sha256` 带不带都行，入库时重新生成）。主服务启动时把它作为官方包入库，内核才知道有这个转换器。
2. **加 COPY**：`Dockerfile` 的 `connector-gateway` 段，在已有的几行转换器 COPY 后加一行：
   ```dockerfile
   COPY assets/packs/<名字> /opt/connector/converters/<名字>
   ```
3. **加依赖**（只用标准库和第 4 节已有的库时跳过）：写进 `backend/connector-requirements.txt`，固定主版本，在文件头的注释里记许可证；离线构建把 wheel 放进 `vendor/wheels/`。依赖只进网关镜像，不进主服务的 `requirements.txt`。
4. **放开新扩展名**（扩展名不在下表「能上传」里时）：加进 `backend/app/services/uploads.py` 的 `MATERIAL_TYPES`，新建项目页上传框的 `accept`（`frontend/src/pages/create/CreatePage.jsx`）同步加上；不加的话这种文件上传时回 415。
5. **重建两个镜像**：主服务（带新的 `assets/packs`）和网关都要重建。开发环境 `docker compose up --build`；本机部署 `deploy/redeploy-app.sh <版本>`（一起重建、替换网关）；离线交付 `./build.sh <版本>` 后 `deploy.sh`。
6. **核对**：网关日志「连接器网关监听 :8120，转换器 …」里有 `<名字>@<版本>`；`GET /api/v1/converters` 的 `items` 里它的 `loaded` 为 true；上传一份合成样例资料，状态变成「已转成文本」。

用户已经上传过同名私有包时，仓库仍按私有包处理，内核看不到内置的那份：先由管理员把私有包发布到全平台，或换一个包名再内置。

资料类型现状：

| 扩展名 | 现状 | 写转换器有没有用 |
|---|---|---|
| `.md` `.markdown` `.txt` `.csv` | 本身是文本，不经网关 | 没用，不会被调用 |
| `.pdf` `.docx` `.xlsx` `.xlsm` | 已有官方转换器 | 作为备选；管理员指定默认后才用 |
| `.doc` `.xls` `.html` `.htm` `.png` `.jpg` `.jpeg` | 能上传，没有转换器（暂不支持） | 有用 |
| `.drawio` | 流程图，不走转换 | 没用 |
| 其他（如 `.kvtxt`） | 上传接口拒收 | 要先做第 4 步 |

## 10. 同一类型有多个转换器时选哪个

规则（内核 `pick(类型)`），按顺序：

1. 管理员给这种类型指定的默认（`PUT /api/v1/admin/converters/defaults`），指定的转换器还在；
2. 否则官方（镜像内置）的优先；
3. 再按包名字母序取第一个。

- 真正转换时只在网关已加载的转换器里选；目录卡片的 `default_for` 不看网关。
- 只算公开包，私有包不参与。
- 换默认不重转已有资料，资料上有「重新转换」。

## 11. 交付时告诉用户

助手收尾时照下面的意思说，替换尖括号里的内容：

```
插件包 <名字>-<版本>.zip 已生成。在插件中心上传并安装后，「多源连接器」里能看到它。
它要平台管理员在插件详情里点「信任」才会运行：信任后平台把它装进连接器网关，不用重建镜像。
<转换器：之后上传的 .xxx 资料会用它转成文本；扩展名不在上传白名单里时，还要运维按第 9 节第 4 步放开。>
<数据源连接器：之后项目成员在资料页「接入数据源」就能选它；业务库请给只读账号。>
```

## 12. 数据源连接器

决策见 ADR-0104。骨架是 `references/examples/sample-source/`（从一个返回 JSON 表结构的 HTTP 服务读表和外键，只用标准库），
`pack_tool.py init <名字> --kind source` 复制它。

### 12.1 清单的 `source` 段

```yaml
source:
  entry: connector.py
  license: 只用 Python 标准库
  vendor: vendor            # 可选：随包带的纯 Python 依赖目录
  fields:
    - {key: host, label: 主机, type: host, required: true}
    - {key: port, label: 端口, type: port, default: 8080}
    - {key: token, label: 访问令牌, type: secret, required: true}
    - {key: tables, label: 表范围, type: text, placeholder: "orders, customer*"}
```

- `fields` 是资料页「接入数据源」的表单，界面按它生成，包里不带前端代码。字段 `type` 取 `text`、`number`、`host`、`port`、`secret`、`select`
  （`select` 要写 `options: [{value, label}]`）。
- `secret` 类字段只进连接器网关的凭据库，助手、内核都看不到；其余字段写进数据源资料，助手看得到，不要把密钥设计成非 `secret` 字段。
- `host`、`port` 各最多一个：网关只放行这一对主机端口，子进程连别处会被拦（`egress_denied`）。
- 键名 `tables` 有约定：逗号分隔的表范围（`*` 只在末尾），`ctx.in_scope(表名)` 按它判断。
- 依赖：只用标准库最好。要用第三方库时选纯 Python 的，把它解压进包里的 `vendor/`，在清单写 `vendor: vendor`；子进程把它放在
  `sys.path` 最前。不要带编译过的扩展。

### 12.2 两个函数

```python
from app.connector_gateway.source.sdk import SourceError

def test(ctx):      # 连接、自检；每一项用 ctx.check(名称, 是否通过, 说明) 报
    ...

def collect(ctx):   # 发表和事实
    ctx.table("orders", [{"name": "id", "type": "bigint", "key": "pk", "comment": "订单号"}, ...], rows, comment="订单")
    ctx.fact("foreign_key", table="orders", columns=["customer_id"], ref_table="customer", ref_columns=["id"], name="fk_c")
```

`ctx` 的全部成员见平台文档 `design/data-sources.md` 第 1 节，常用的：

| 成员 | 说明 |
|---|---|
| `ctx.config`、`ctx.secret` | 非密钥字段（已按类型转好、补了缺省值）、密钥字段 |
| `ctx.table(name, columns, rows, comment)` | 一张表；`columns` 里 `key` 取 `pk`、`fk`、`uk` 或空；`rows` 给原始取值，最多 200 行 |
| `ctx.fact(kind, …)` | `primary_key`、`foreign_key`、`reference`、`unique`、`check`、`values`、`view`、`routine`、`trigger` |
| `ctx.throttle()`、`ctx.query_timeout_s()` | 每次查业务系统前调；单次请求的超时 |
| `ctx.readonly(sql, dialect)` | 数据库类连接器：每条语句先过只读白名单 |
| `ctx.in_scope(name)`、`ctx.progress(done, total)`、`ctx.limitation(text)` | 范围、进度、局限 |
| `raise SourceError(code, message)` | 失败；常用 `connect_failed`、`auth_failed`、`account_not_readonly` |

- 网关负责遮蔽（手机号、证件号、姓名等按列名和取值识别，关联键加盐哈希）、只留 20 行样本、渲染成文本版，插件不要自己遮蔽或拼 Markdown。
- 数据库类连接器在 `test` 里自检账号权限：账号能写就报 `ctx.check("只读", False, …)`，`collect` 里 `raise SourceError("account_not_readonly")`。
- 异常消息里不要写主机、账号、密码、连接串。
- 插件的 `print` 进标准错误，不影响结果。

### 12.3 本地自测

```bash
cd <平台源码>/modeling/onto-modeling/backend
python - <<'PY'
from pathlib import Path
from app.connector_gateway.source import render, runner
out = runner.run(Path("<包目录>/connector.py"), None, "collect", {"host": "127.0.0.1", "port": 8080, "tables": ""},
                 {"token": "<测试令牌>"}, [{"host": "127.0.0.1", "port": 8080}], runner.SourceLimits(timeout_s=60))
print(out.ok, out.error_code)
print(render.render(out.collected, "salt")[0][:2000])
PY
```

测试数据全部合成；测试服务、测试库只放在本机，不放进包里。
