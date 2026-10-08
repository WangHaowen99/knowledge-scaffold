---
name: extract-migrated-objects
description: Use when the user asks to 迁移提取业务对象, 迁移object, 迁移业务对象, 复用提取业务对象, 对象迁移, object迁移, 旧场景对象复用, or migrate/copy/reuse/extract business object markdown from old-scenario object docs into a new scenario. Requires user-specified OLD_OBJECT_PATHS and NEW_SOURCE_PATHS, compares old objects with new evidence, decides copy unchanged/copy modified/drop/add new, and writes ontology/object/*.md according to this toolset's object extraction rules.
---

# Extract Migrated Objects

## Intent

Use this skill when object extraction is a migration from an old scenario, not a blank extraction. Do not bulk copy every old object. First prove which old objects, attributes, rules, and relationships still apply to the new scenario, then decide what to copy, modify, drop, or add.

## Required Context

Before writing object documents:

- Read `CLAUDE.md`, `.claude/templates/object-template.md`, and `.claude/agents/extract-objects-agent.md`.
- Treat `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` as active context.
- When object boundary or split/merge decisions are unclear, read `.claude/reference/OBJECT_EXTRACTION_HEURISTICS.md`.
- When new evidence includes paired flowchart summaries and Excel supplements, follow `.claude/commands/ontology-extract-object.md` Step 1.5 to export `EXCEL_CONTEXT`.

## Inputs

- `OLD_OBJECT_PATHS`: required user-specified old-scenario object paths. Accept one or more directories and/or Markdown files.
- `NEW_SOURCE_PATHS`: required user-specified new-scenario evidence paths. Accept one or more research reports, flowchart summaries, Excel files, Markdown files, directories, or other user-provided documents.
- `TARGET_OBJECTS_DIR`: default to `ontology/object/`.

Do not assume or auto-discover old or new scene paths. Stop and ask when either `OLD_OBJECT_PATHS` or `NEW_SOURCE_PATHS` is not provided, empty, or cannot be located.

## Workflow

### 1. Inventory Old Objects

Read old object docs from every path in `OLD_OBJECT_PATHS`. For directories, recurse through nested folders for `*.md`; for files, read the file directly. For each old object, capture:

- object name, aliases, English name, and business meaning;
- primary key, attributes, types, enum values, formulas, units, constraints, and derived-property markers;
- related objects and whether each relationship points to another old object;
- old-scenario wording that looks process-specific, organization-specific, system-specific, or lifecycle-specific.

Treat old object docs as candidates, not as authority for the new scenario.

### 2. Analyze New Evidence

Read every new source document before deciding. For `.xlsx` inputs:

- if paired with selected flowchart summaries, export compact Markdown through `.claude/scripts/excel_context.py`;
- if explicitly supplied as standalone evidence, convert or inspect visible sheets into compact Markdown context and keep sheet names and source paths as provenance.

Extract candidate objects, fields, rules, and relationships from new evidence using the same standards as `.claude/agents/extract-objects-agent.md`.

### 3. Compare Deeply

For each old object, compare against new evidence on these dimensions:

- semantic identity: same real business thing, renamed concept, narrower/wider concept, or unrelated;
- lifecycle and ownership: whether creation, status changes, usage, and retirement still match;
- granularity: whether it should remain one object, split, merge, or become an attribute;
- attributes: which old attributes are supported, contradicted, irrelevant, or missing from new evidence;
- rules: enum values, formulas, units, required/unique constraints, and derived-property writers;
- relationships: whether linked objects will also exist in the new scenario;
- terminology: new names, aliases, abbreviations, and wording differences.

Keep an explicit migration decision table with columns:

```markdown
| 旧对象 | 决策 | 新对象名 | 依据 | 属性处理 | 关系处理 | 输出文件 |
| --- | --- | --- | --- | --- | --- | --- |
```

Use one of these decisions:

- `COPY_UNCHANGED`: same identity, lifecycle, attributes, rules, and relationships still apply. Copy the old Markdown without content edits.
- `COPY_MODIFIED`: same core object, but new evidence requires adding, deleting, renaming, or changing attributes, rules, aliases, descriptions, or relationships.
- `DROP`: the object is absent from the new scenario, too old-scenario-specific, contradicted by new evidence, or only meaningful through relationships that will not exist.
- `ADD_NEW`: new evidence describes an object not covered by old docs and it passes the current object extraction standards.

Preserve an old attribute only when it is explicitly present in new evidence, required by the current object template, or is stable identity metadata of an object that clearly applies to the new scenario. Mark inherited-but-not-explicit details as assumptions in the final summary.

### 4. Write Target Objects

Write only under `TARGET_OBJECTS_DIR`.

- For `COPY_UNCHANGED`, copy the old file to `ontology/object/{对象名}.md` without changing its content.
- For `COPY_MODIFIED`, start from the old file but edit only lines justified by new evidence or current object rules.
- For `ADD_NEW`, create the document from `.claude/templates/object-template.md`.
- Remove or rewrite `[[...]]` relationships that point to dropped objects unless the target object already exists in `ontology/object/`.
- Follow the required object format and attribute-rule rules from `.claude/agents/extract-objects-agent.md`; do not invent a parallel format in this skill.

Do not write migration notes into object files unless they are part of the business description or attribute rules.

### 5. Validate And Report

After writing, run a focused rule check when `ontology/` exists:

```bash
mkdir -p reports
VALIDATION_TS=$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S)
VALIDATION_REPORT_PATH="reports/${VALIDATION_TS}-objects-rule.md"
python3 .claude/scripts/ontology_checker.py ontology/ --files {written_object_stems} --include-mock --include-knowledge --output "$VALIDATION_REPORT_PATH"
```

Do not use `ontology_checker.py`'s default `report_YYYYMMDD_HHMMSS.md` output name, and do not use `--json` for this post-migration report. The focused validation report must be saved under `reports/` using the project-wide Markdown report naming convention: `reports/{YYYYMMDD}-{HHMMSS}-objects-rule.md`. The timestamp in the filename and any timestamp shown in the report use `Asia/Shanghai`.

If validation cannot run, state why. Return a concise migration report and include the saved validation report path:

```markdown
对象迁移提取完成。

原样复制（N 个）：
- ...

修改复制（N 个）：
- ...：修改了 ...

舍弃（N 个）：
- ...：原因 ...

新增（N 个）：
- ...

校验报告：
- reports/YYYYMMDD-HHMMSS-objects-rule.md

需要确认 / 假设：
- ...
```

Do not commit unless the caller command or user explicitly asks for a phase-end commit.
