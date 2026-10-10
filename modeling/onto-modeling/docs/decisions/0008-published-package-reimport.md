# 0008 已发布知识包的回导

## Context

「导入已有知识包」（弹窗标题「导入建模任务」）此前只认一种压缩包：
`export_zip(scope="ontology")` 导出的**工作区原样产物**。摘要按 `ontology/object`、
`ontology/operation` 两个单数目录数数，正式导入由 `import_zip` 把成员原样铺到项目根。

而 `export_package_zip` 发布的 ontology-package v0.1 用的是另一套布局：
`<包根>/objects`、`<包根>/operations`、`<包根>/rules`、`<包根>/metrics`、
`<包根>/scripts`（复数，且带一层包根目录，文档带包契约 frontmatter）。两套布局之间
没有任何转换，于是从这个入口导入发布包时：

- 预览数出 0 对象 0 操作；
- 文件整体嵌在 `<包根>/` 里，项目自己的 `ontology/` 仍为空；
- 项目概览的 `objects`/`operations` 计数与阶段探针同样只读单数目录，也数不到；
- 全程没有任何一步校验"是否真的读出了内容"，任务照常创建成功。

结果是一个"导入成功但是空"的任务（Issue #359）。按钮名叫「导入已有知识包」，但它
认不出真正的知识包，而且认不出时不报错。

## Decision

1. **判别收敛到一处，预览与正式导入共用。** `package_archive.inspect_archive()`
   是唯一的判别与计数入口，`dry_run` 预览与正式导入前的门禁拿同一份结果。预览和导入
   各算一次，必然漂移成"预览数出 0、导入照样建个空任务"——这正是本缺陷的形态。

2. **按四种格式互不重叠的必需文件判别，顺序固定。** `.onto-web.json`（工作区迁移包）
   → 最浅 `manifest.yaml` + `objects/`（已发布知识包）→ 顶层含
   `EXPORT_ONTOLOGY_DIRS`（工作区内容）→ 无法识别。顺序不能换：迁移包里同样有
   `ontology/` 目录，不先认它的工作区元数据就会被当成工作区内容原样铺开、丢掉版本历史。
   包根取**最浅**的 `manifest.yaml` 所在目录，所以 `<包id>/` 包根与无外层目录两种
   形态都能吃。

3. **知识包按工作区布局物化，派生物不搬。** 五元组进 `ontology/object|operation|rule|metric|scripts`
   单数目录，`.md` 剥掉包契约 frontmatter；各级 `index.md` 是 `_refresh_indexes` 的
   派生物，不从包里搬，导入后按工作区规则重建。脚本按描述符的 `entrypoint` 反查源码
   并还原原始 key——打包时非 ASCII 脚本名会被哈希成 `script_<hash>.py`
   （`_package_script_filename`），按包内文件名直接落地会把脚本 key 永久变成那串哈希。

4. **原包整份留底到 `.imported-package/`。** 对象↔数据表的绑定记在对象文档的
   `mapping:`/`sources:` frontmatter 与 `mappings/object-data-mappings.json` 里，
   而新工作区没有数据源文件、推导不出这份绑定
   （`package_mapping.resolve_object_tables` 依赖已上传的表格资料）。工作区文档剥掉
   frontmatter 后，只有留底区还留着它。留底区随 git 版本化（跨环境迁移要带走），
   以点开头因此不进文件树，并列入 `WRITE_FORBIDDEN_TOP` 不可编辑。

5. **门禁放在建项目之前。** 判别为知识包却解析出 0 对象、格式无法识别、或压缩包是
   迁移包时，一律在 `create_project` 之前拒绝并说明下一步，不留半成品任务。门禁同时
   落在 `import_package_zip`（服务层，任何调用方都绕不过）与 `/api/projects/import`
   （路由层，覆盖走 `import_zip` 的分支）。

6. **工作区原样产物那条路径不动。** 仍判为 `workspace_ontology`、仍走 `import_zip`，
   也**不**给它加实体数门禁——只有 `research/` 的早期项目导出是合法的。

## Alternatives considered

- **只把摘要的目录前缀改成同时认复数目录**：预览数字会好看，但 `import_zip` 仍原样
  铺文件，概览与阶段探针读的是工作区单数目录，任务依旧是空的。治的是症状。
- **不留底，只搬五元组**：`mappings/object-data-mappings.json` 与对象 frontmatter 里的
  绑定会在导入时永久消失，正是 Issue 要求避免的"只移动目录造成链接丢失"。
- **留底只存未物化的成员**：对象文档的 `mapping:`/`sources:` frontmatter 恰好长在
  已物化的那些文件上，剥掉之后哪里都找不回来。
- **按产品契约直接拒绝发布包、引导用户换入口**：Issue 的验收标准允许这个口径，改动也
  更小，但用户仍然无法把已发布的知识包拿回建模端继续维护，问题只是被挡住了。
- **把转换逻辑加进 `WorkspaceManager`**：`workspace.py` 已近 1400 行，包格式知识与
  工作区管理是两件事；只在 `workspace.py` 留一个薄方法（建项目、物化、重建索引、提交），
  格式知识全部放 `package_archive.py`。

## Consequences

- **已发布知识包导入后阶段进度仍为 0。** 第一个必选阶段探针是 `research/`，而发布包
  按定义只有交付物、没有过程资产。阶段链按 ADR-0004 §6 不动（改了会让所有历史项目的
  进度条变化），导入向导改为明确告知这一点，避免被当成新缺陷。跨环境搬运进行中的项目
  应当用「导出迁移包（完整工作区）」。
- **留底区让项目体积约为包内容的两倍。** 发布包是文本（md / json / py），代价可接受；
  换来的是任何时候都能回到收到的原始字节。留底区只写不读，不参与发布渲染，用户删掉
  一个对象后不会被它复活。
- 已经产生的 0 对象任务不做静默迁移或覆盖：恢复路径是重新导入新建任务，旧任务由用户
  自行删除。
- `import_summary()` 的返回体新增 `kind`、`kind_label`、`package_root`、`package_name`、
  `package_version`、`rules`、`metrics`、`scripts`、`blockers` 字段；原有字段语义不变，
  存量前端即便不更新也不会坏。
- 判别只看压缩包里的文件清单，不读业务模型、不连数据库，也不依赖评测结果。
