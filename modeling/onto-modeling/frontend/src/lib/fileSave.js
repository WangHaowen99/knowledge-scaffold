/**
 * 文件视图的保存流程（方案 7.4 双写合并），与界面状态解耦。
 *
 * 保存成功后 after 收到实际写入的 { text, revision }。界面必须用它更新，不能用点击保存时
 * 捕获的草稿与修订：冲突后选「采用对方版本」时写入的是对方内容，捕获的修订也已过期，
 * 用捕获值会让预览停在放弃的旧草稿上、下次保存又误报冲突（Issue #404）。
 */

/** 保存一段文本；与他人或助手的写入冲突（409）时读最新内容，返回冲突交给用户二选一。 */
export async function saveText({ write, read, text, baseRevision, after }) {
  try {
    const result = await write(text, baseRevision);
    after?.({ text, revision: result.revision });
    return { result };
  } catch (error) {
    if (error.status !== 409) throw error;
    const theirs = await read().catch(() => null);
    return { conflict: theirs ? { mine: text, theirs, after } : null };
  }
}

/** 冲突框里选定内容（对方版本或我的版本）后保存，以对方的最新修订为基线。 */
export async function saveResolved(conflict, text, write) {
  const result = await write(text, conflict.theirs.revision);
  conflict.after?.({ text, revision: result.revision, resolved: true });
  return result;
}

/** 文件视图的内容换成实际保存的文本与修订，其余字段保留。 */
export function withSavedText(content, saved) {
  return content ? { ...content, content: saved.text, revision: saved.revision } : content;
}
