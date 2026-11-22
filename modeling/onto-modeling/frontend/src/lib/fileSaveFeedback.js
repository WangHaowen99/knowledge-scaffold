export function showFileSaveFeedback(result, messages) {
  const warnings = result.warnings || [];
  if (!warnings.length) {
    messages.success('已保存并记录迭代');
    return;
  }
  const locations = warnings.slice(0, 5).map(
    (warning) => `${warning.path} 第 ${warning.line} 行：${warning.message}`,
  );
  if (warnings.length > 5) locations.push(`另有 ${warnings.length - 5} 条告警`);
  messages.warning({ content: `已保存，存在告警：${locations.join('；')}`, duration: 10 });
}
