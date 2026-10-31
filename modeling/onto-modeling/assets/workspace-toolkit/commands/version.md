Version management for non-technical users. All output in Chinese. Subcommand: $ARGUMENTS

## Parse subcommand

If $ARGUMENTS is empty or "历史": go to **History**
If $ARGUMENTS starts with "回退": go to **Rollback**
If $ARGUMENTS starts with "对比": go to **Diff**
If $ARGUMENTS starts with "标记": go to **Tag**

---

## History

Show the last 10 commits in a user-friendly format:

```bash
git log --oneline -10
```

Parse each line and display as:

```
版本历史（最近 10 个版本）

版本 {N}:   {描述}  ({相对时间})
版本 {N-1}: {描述}  ({相对时间})
...
```

Where:
- {N} = total commit count (compute with `git rev-list --count {hash}` for each commit)
- {描述} = commit message with prefix translated:
  - `research:` → "资料分析："
  - `flowchart:` → "流程图："
  - `extract:` → "提取："
  - `fix:` → "修复："
  - `mock:` → "测试数据："
  - `codegen:` → "代码生成："
  - Others → use as-is
- {相对时间} = use `git log --format='%ar'` and translate to Chinese (e.g., "2 hours ago" → "2 小时前")

After display:
"输入 `/version 回退` 回退到上一版本
输入 `/version 对比 N M` 对比两个版本的差异"

---

## Rollback

Parse $ARGUMENTS for an optional version number. If none specified, rollback the last commit.

1. Gather info:
   ```bash
   git log --oneline -1
   git diff HEAD~1 --stat
   N=$(git rev-list --count HEAD)
   ```

2. Display:
   "即将撤销版本 {N} 的更改：{commit message}

   回退后内容将恢复到版本 {N-1} 的状态。

   ⚠️ 说明：回退不会删除已有版本记录，而是生成一个新版本（版本 {N+1}），把内容"倒回去"。所有历史依然可以在 `/version 历史` 中查到。

   受影响的文件：
   - {file list from diff stat}

   **下一步：**
   - 回复 **确认** → 执行回退
"

3. If confirmed:
   ```bash
   git revert HEAD --no-edit
   ```

   After success:
   ```bash
   NEW_N=$(git rev-list --count HEAD)
   ```
   Display: "已回退。当前为版本 {NEW_N}，内容恢复到版本 {N-1} 的状态。如需再次回退，重复此命令。"

4. If cancelled: "已取消回退。"

---

## Diff

Parse $ARGUMENTS for version numbers: "对比 N" or "对比 N M".

**对比 N** (compare version N with current):
1. Resolve version N to a commit hash: count backwards from HEAD
2. Run diff:
   ```bash
   git diff {hash}..HEAD --stat
   git diff {hash}..HEAD --name-status
   ```

**对比 N M** (compare version N with version M):
1. Resolve both to commit hashes
2. Run diff between them

Display in simplified format:
"版本 {N} 与版本 {M} 的差异：

新增文件：
- {path}

修改文件：
- {path}

删除文件：
- {path}

如需查看具体修改内容，请指定文件名：`/version 对比 {N} {M} 文件名`"

---

## Tag

Parse $ARGUMENTS for description after "标记": e.g., "标记 初版完成"

```bash
N=$(git rev-list --count HEAD)
git tag -a "v${N}-{description}" -m "{description}"
```

Display: "已标记当前版本为 v{N}-{description}"
