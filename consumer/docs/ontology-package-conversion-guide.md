# 本体包转换脚本使用指南

## 1. 背景

后端只接受 v0.1 本体包，不再自动转换 legacy 格式。导入前包必须满足：

- `manifest.yaml` 中 `kind: ontology-package`、`schema_version: 0.1`
- 必选结构：`index.md`、`objects/index.md`、`operations/index.md`
- `objects/`、`operations/` 下每个明细文件带 YAML frontmatter
- `mappings/object-data-mappings.json`（如存在）通过字段级校验

`consumer/scripts/` 下的转换脚本 `legacy_package_converter.py` 用于离线完成"校验 → 探测 → 转换 → 再校验"一键管线，是 legacy 包进入系统的前置步骤。脚本只读源包，转换产物写入独立的输出目录或 zip，源包永不修改。

## 2. 快速开始

```bash
cd consumer/scripts

# 最常用：输入 zip，默认只输出转换后的 zip（生成于输入同级，<name>_converted.zip）
python legacy_package_converter.py ontology.zip

# 同时保留转换后的目录
python legacy_package_converter.py ontology.zip --output-dir /tmp/my_output

# 指定 zip 输出路径
python legacy_package_converter.py ontology.zip --zip-output /tmp/ontology_v01.zip

# 也可以输入目录（输出目录，不产生 zip）
python legacy_package_converter.py /path/to/ontology_dir
```

## 3. 命令说明

```
python legacy_package_converter.py <package_dir_or_zip> [--output-dir <dir>] [--zip-output <zip>]
```

| 参数 | 说明 |
| --- | --- |
| `<package_dir_or_zip>` | 输入本体包，可以是 `.zip` 文件或目录 |
| `--output-dir <dir>` | 目录输入时指定转换产物输出目录（省略默认生成 `<name>_converted` 兄弟目录）；zip 输入时同时把转换目录保留到该路径 |
| `--zip-output <zip>` | 仅 zip 输入时有效，覆盖默认的 zip 输出路径 |

**zip 输入时默认只输出 zip**：转换目录保存在临时目录内（退出即清理），zip 生成于输入 zip 同级 `<name>_converted.zip`；显式传 `--output-dir` 才会把转换目录保留到磁盘。

**目录输入时只输出目录**（默认 `<name>_converted` 兄弟目录），不产生 zip。

退出码：`0` 转换成功、`1` 转换后仍不合规（或输入错误）、`2` 参数错误。

## 4. 转换管线

脚本内部依次执行：

1. **校验**：先用 `validate_package` 检查输入包，如果已合规则跳过转换直接输出。
2. **探测**：调用 `detect_package_format` 分析格式信号，生成转换动作清单。
3. **转换**：按动作清单逐项转换（见下节），产物写入输出目录。
4. **再校验**：对转换后的目录重新校验，确认 0 错误。

CLI 只输出一行纯文本结果：成功时打印 `知识包转换成功: <产物名>`，失败时打印 `知识包转换失败: <原因>`；退出码 0/1/2 对应成功、转换后仍不合规（或输入错误）、参数错误。

脚本内部以 `ImportResult` 携带结构化结果，供 Python 调用（`import_package` / `import_package_from_zip` 的返回值）使用：

- `imported: true` + `required_conversion: false` — 包原本就合规
- `imported: true` + `required_conversion: true` — 转换后合规，`zip_output` 指向 zip；`import_path` 仅在使用 `--output-dir` 时指向保留的目录（zip 输入省略时为 `""`）
- `imported: false` — 转换后仍不合规，`errors` 列出剩余问题

## 5. 转换规则

| 规则 | 行为 |
| --- | --- |
| `path.flatten` | 内容在 `ontology/` 子目录下时平铺到根目录 |
| `manifest` | `kind` 修正为 `ontology-package`；补 `schema_version: 0.1`、`status: draft`；额外字段加 `x_` 前缀 |
| `root_index` | `ONTOLOGY.md` 改名为 `index.md`；缺失时按 manifest 生成；补 frontmatter |
| `objects` | `objects-list.md` 改名为 `objects/index.md`；明细文件补 frontmatter |
| `operations` | `operations-list.md` 改名为 `operations/index.md`；明细文件补 frontmatter（含 `uses.objects: []`） |
| `scripts` | `scripts-list.md` 改名为 `scripts/index.md` 并补 frontmatter |
| `relations` | 删除 `relations/` 目录和 `relations-list.md`（v0.1 格式无此目录） |
| `mappings` | `primary_key` 物理名反查重写为业务 key；删除 Provider 元数据字段；`float`/`int`/`double` 等类型归一化为 `number` |

## 6. 内置能力

格式探测（`detect_package_format`）与合规校验（`validate_package`）已内置为 `legacy_package_converter.py` 的内部实现函数，转换管线自动调用，不再提供独立脚本。如需单独排查格式信号或校验问题，可编写简短的 Python 片段直接 import 这些函数：

```python
from legacy_package_converter import detect_package_format, validate_package
```

## 7. 示例

```bash
# 转换 legacy zip 包（最常用，默认只输出 zip）
python legacy_package_converter.py ontology.zip

# 转换并保留目录
python legacy_package_converter.py ontology.zip --output-dir /tmp/ontology_out

# 转换目录格式的包
python legacy_package_converter.py /path/to/your_ontology_dir

# legacy_package_converter.py 会自动完成"校验→探测→转换→再校验"，无需手动分步
python legacy_package_converter.py /path/to/your_ontology_dir --output-dir /tmp/ontology_out
```

## 8. 验证

```bash
cd consumer/scripts
python -m pytest test_import_pipeline.py test_detect_package_format.py
```

当前测试全部通过。
