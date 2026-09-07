const MAX_MESSAGE_LENGTH = 120;

const STATUS_MESSAGES = {
  400: '请求内容不正确，请检查后重试',
  401: '登录状态已失效，请重新登录',
  403: '没有权限执行此操作',
  404: '请求的内容不存在',
  406: '请求格式不符合接口要求',
  409: '内容已更新或操作冲突，请刷新后重试',
  413: '请求内容过大，请缩小后重试',
  415: '请求格式不受支持',
  422: '提交内容未通过校验，请检查后重试',
  429: '请求过于频繁，请稍后重试',
};

function compactMessage(value) {
  if (typeof value !== 'string') return '';
  const message = value.replace(/\s+/gu, ' ').trim();
  if (!message || /^internal server error$/iu.test(message)) return '';
  return message.length <= MAX_MESSAGE_LENGTH
    ? message
    : `${message.slice(0, MAX_MESSAGE_LENGTH - 3)}...`;
}

export function publicApiErrorMessage(data, status) {
  const statusCode = Number(status ?? data?.statusCode ?? data?.status);
  if (statusCode >= 500) return '服务暂时不可用，请稍后重试';
  const display = data?.display && typeof data.display === 'object'
    ? data.display.text
    : '';
  for (const candidate of [display, data?.error, data?.reason, data?.message]) {
    const message = compactMessage(candidate);
    if (message) return message;
  }
  return STATUS_MESSAGES[statusCode] || `请求失败（${statusCode || '未知状态'}）`;
}
