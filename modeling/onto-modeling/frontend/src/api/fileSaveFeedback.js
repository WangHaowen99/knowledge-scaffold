// 保存文件（F-03）的反馈：脚本保存前只编译检查契约，硬伤回 check_rejected、没有保存；
// 存下来了但有提示（TODO/FIXME、输出未验证）随 warnings 回来，提示要带位置、不能只说一句「已保存」
const SHOWN = 5;

function listed(items, text, unit) {
  const lines = items.slice(0, SHOWN).map(text);
  if (items.length > SHOWN) lines.push(`另有 ${items.length - SHOWN} 条${unit}`);
  return lines;
}

// 保存成功：text、icon 给轻提示，note 留在编辑器上方（没有提示时为 null）
export function fileSaveFeedback(result, savedText) {
  const warnings = result?.warnings || [];
  if (!warnings.length) return { text: savedText, icon: 'check', note: null };
  return {
    text: `${savedText}，有 ${warnings.length} 条告警`,
    icon: 'alert',
    note: {
      tone: 'amber',
      title: '已保存，存在告警',
      lines: listed(warnings, (w) => `${w.path}${w.line ? ` 第 ${w.line} 行` : ''}：${w.message}`, '告警'),
    },
  };
}

// 保存被检查拒绝（422 check_rejected）：逐条写出错在哪；其他错误返回 null，照常用 errorText
export function fileSaveRejection(error) {
  if (error?.code !== 'check_rejected') return null;
  const errors = error.details?.errors || [];
  const lines = errors.length
    ? listed(errors, (e) => (e.path ? `${e.path}：${e.message}` : e.message), '错误')
    : [error.serverMessage].filter(Boolean);
  return { tone: 'red', title: '没有保存，修改还在编辑器里', lines };
}
