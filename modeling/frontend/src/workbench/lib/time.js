/* 会话与轨迹时间统一带上年月日。
   助手在服务端跨天继续推进，只给时分会让隔天的记录看起来像同一轮。 */
const pad = (part) => String(part).padStart(2, '0');

function localParts(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return null;
  return {
    day: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    minute: `${pad(date.getHours())}:${pad(date.getMinutes())}`,
    second: pad(date.getSeconds()),
  };
}

export function formatMessageTime(value) {
  const parts = localParts(value);
  return parts ? `${parts.day} ${parts.minute}` : '';
}

/* 轨迹比对话多给一位秒：同一分钟内会落多条工具记录，秒是唯一能分先后的信息。 */
export function formatTraceTime(value) {
  const parts = localParts(value);
  return parts ? `${parts.day} ${parts.minute}:${parts.second}` : '';
}
