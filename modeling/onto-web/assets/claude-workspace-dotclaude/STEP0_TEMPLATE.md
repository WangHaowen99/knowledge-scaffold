# Step 0 — 通用前置检查模板

所有修改 `ontology/` 的命令（research / flowchart / extract / validate / mock / trace / codegen）在进入主流程前必须跑一遍本模板；只读命令（overview / impact / query / version）跳过锁步骤但仍做阶段检查以便展示上下文。

## 约定来源（authoritative）

执行本模板任何一步之前，确认下列约定文件已在 context（CLAUDE.md 已强制要求，但命令体内重复一次以防漏读）：

- `.claude/USER_VOCABULARY.md` — 所有面向用户输出文本统一词汇
- `.claude/INTERACTION_CONVENTIONS.md` — 错误恢复 / 跳步守卫 / 等价词 / 编号列表等交互话术
- `.claude/VALIDATION_BOUNDARIES.md` — 校验归口与「权威严重性矩阵」（validate/rule-check/semantic-check 必读）
- `.claude/BUSINESS_KNOWLEDGE_SNIPPET.md` — 业务知识识别与应用守则

下面所有提示语（错误恢复、跳步守卫等）一律是对 INTERACTION_CONVENTIONS.md 中相应话术段的引用，不是另立标准；如果两边出现冲突，**改 STEP0_TEMPLATE 让它对齐 INTERACTION_CONVENTIONS，不要反过来**。

## 输入

- `COMMAND_NAME`：命令短名（如 `extract-object`）
- `PHASE`（可选）：进入主流程时的初始阶段标签，用于写入锁文件，默认 `start`

## 执行步骤

### 1) 检查是否有未完成的同名命令

```bash
python3 .claude/scripts/state_helper.py check-lock {COMMAND_NAME}
```

- 输出 `ok` → 继续下一步。
- 输出 `stale:{started_at}:{phase}` → 一天以上的锁视为上次已崩溃，**自动调用 `force-release` 并继续下一步**，同时告知用户："清理了上次（{started_at}）的残留记录。"
- 输出 `locked:{started_at}:{phase}` → 按 `.claude/INTERACTION_CONVENTIONS.md` 的 **错误恢复类** 话术提示：

  ```
  ⚠️ 发现上次未完成的 {COMMAND_NAME}：
  - 起始时间：{started_at}
  - 中断阶段：{phase}

  - 回复 **续执** → 从中断处继续
  - 回复 **清理重来** → 清理残留并重新开始
  ```

  - **续执**：读取 `.ontology-state/lock-{COMMAND_NAME}.json` 的 phase 字段，跳转到命令中对应阶段继续执行；跳过 Step 0 其余内容。
  - **清理重来**：
    ```bash
    python3 .claude/scripts/state_helper.py force-release {COMMAND_NAME}
    ```
    继续第 2 步。
  - **取消**：立即 stop。

### 2) 检查阶段前置

```bash
python3 .claude/scripts/state_helper.py check-prereq {COMMAND_NAME}
```

- 输出 `ok` → 继续第 3 步。
- 输出 `missing:{阶段1,阶段2,…}` → 按 `.claude/INTERACTION_CONVENTIONS.md` 的 **跳步守卫类** 话术提示：

  ```
  ⚠️ 跳步提醒：当前缺少 {阶段列表}。
  继续会导致：{后果描述}。

  - 回复 **继续** → 忽略提醒继续
  - 回复 **先做 <关键词>**（/<斜杠命令>）→ 补齐后再继续
  ```

  后果描述参照：
  - 缺少 `调研完成`（且也无流程图）→ 对象提取无源，结果可能凭空编造
  - 缺少 `对象已提取` → 引用对象名无从对齐
  - 缺少 `逻辑已提取` → 无法进行逻辑校验、追踪或代码生成

  > 注：流程图阶段是可选的。`extract-object` 的前置只要求有研究报告或流程图其一；`extract-logic` 的前置要求对象已提取。

  - **继续**：跳过守卫继续第 3 步。
  - **先做 …**：读取对应命令文件并按照它执行，结束后再询问是否回来跑本命令。
  - **取消**：立即 stop。

### 3) 写入执行锁

```bash
python3 .claude/scripts/state_helper.py acquire {COMMAND_NAME} {PHASE 或 'start'}
```

`acquire` 总是成功写入，会覆盖已有的同名锁（第 1 步已经处理过"上次未完成"的情况）。

### 4) 在阶段切换处更新锁

每当命令进入下一阶段（如 extract 从 OBJECTS 进入 LOGIC），再调用一次：

```bash
python3 .claude/scripts/state_helper.py acquire {COMMAND_NAME} {新阶段标签}
```

即 `acquire` 幂等——重复调用直接覆盖 phase 字段，不需要先 force-release。

### 5) 命令结束前释放锁（任何形式的结束都要 release）

```bash
python3 .claude/scripts/state_helper.py release {COMMAND_NAME}
```

**强制规则**：本命令任何路径上的结束都必须先 release，包括但不限于：
- 主流程正常跑完
- 用户表达取消、完成、暂停或类似停止意图
- 前置检查失败导致 early-stop（如目录为空、文档缺失）
- 跳转到其他命令（如 extract 完成后 read `.claude/commands/ontology-validate.md`），在 read 之前必须 release 本命令；下一个命令会在它自己的 Step 0 重新 acquire
- 任何写入文档前抛错或被用户中断的场景

> 工程提示：把每个 `Stop` 写法视为隐含的 `release {COMMAND_NAME}; Stop`。如果新增 Stop 路径，务必在 Stop 之前显式调用 `release`。

`release` 会自动在 `progress.json` 中记录 `last_command={COMMAND_NAME}` 与当前 `stage`，所以多数命令不再需要额外 `update-progress`。如需写入自定义键值对（例如 validate 的 `validate_last_run`），仍可手动调用：

```bash
python3 .claude/scripts/state_helper.py update-progress validate_last_run {timestamp}
```

### 6) 加载已沉淀业务知识

```bash
python3 .claude/scripts/knowledge_helper.py load --scope global \
    {如已知 SCOPE_HINTS, 添加 --scope object:订单 等}
```

将 stdout 内容塞入接下来的 agent prompt 顶部（标题 `# 已沉淀业务知识（自动加载）`）。
若输出 `（暂无相关业务知识）` 则跳过注入。

> SCOPE_HINTS 由各命令决定：例如 extract 会传所有已存在的对象/逻辑名作为 scope；trace 用当前逻辑名作为 scope。

## 注意

- 只读命令（overview/impact/query/version/codegen-scan）：跳过第 1、3、4、5 步，**仅做第 2 步（阶段前置）**；若希望记录"上次查询时间"可调用 `update-progress <key> <timestamp>`，无需 release/acquire。
- `force-release` 不保证安全性，只是清理残留文件；非 stale 场景下调用前必须确认用户已同意"清理重来"。
- lock 文件存在超过一天会自动判定为 stale，`check-lock` 输出 `stale:...` 时命令可直接清理继续，无需再问用户。
- 业务知识加载（步骤 6）只读命令也可调用——通常 `query` 不加载，`overview` 加载用于统计。
