export function durationLabel(seconds) {
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
}

export function estimatedLabel(seconds) {
  if (seconds <= 0) return '已完成';
  if (seconds < 60) return '不到 1 分钟';
  return `约 ${Math.ceil(seconds / 60)} 分钟`;
}
