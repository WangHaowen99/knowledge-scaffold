// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
const CANCELLED_STATUSES = new Set(['aborted', 'cancelled', 'canceled']);

const includesAny = (value, fragments) =>
  fragments.some((fragment) => value.includes(fragment));

// eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
export function getWorkflowErrorPresentation(rawError, options = {}) {
  const detail = String(rawError || '').trim();
  const normalized = detail.toLowerCase();
  const status = String(options.status || '')
    .trim()
    .toLowerCase();
  const errorCode = String(options.errorCode || '')
    .trim()
    .toUpperCase();
  const retriable = options.retriable === true;
  const cancelReason = String(options.cancelReason || '')
    .trim()
    .toLowerCase();

  if (CANCELLED_STATUSES.has(status) || cancelReason === 'user_stop') {
    return {
      title: '任务已停止',
      message: '任务已按你的操作停止，后续步骤没有继续执行。',
    };
  }

  if (
    errorCode === 'AUTHENTICATION_FAILED' ||
    includesAny(normalized, [
      'unauthorized',
      'forbidden',
      'authentication',
      'invalid api key',
      'http 401',
      'http 403',
    ])
  ) {
    return {
      title: '任务未完成',
      message: '模型服务认证失败，请检查模型配置后重试。',
    };
  }

  if (
    errorCode === 'MODEL_RATE_LIMITED' ||
    includesAny(normalized, ['rate limit', 'too many requests', 'http 429'])
  ) {
    return {
      title: '任务未完成',
      message: '模型服务请求过于频繁，请稍后继续执行。',
    };
  }

  if (
    errorCode === 'MODEL_TIMEOUT' ||
    includesAny(normalized, ['timed out', 'timeout', 'http 408', 'http 504'])
  ) {
    return {
      title: '任务未完成',
      message: '模型服务响应超时，请稍后继续执行。',
    };
  }

  if (
    errorCode === 'MODEL_CONNECTION_ERROR' ||
    includesAny(normalized, [
      'failed to fetch',
      'networkerror',
      'network error',
      'connection failed',
      'connection refused',
      'connection reset',
      'disconnect/reset before headers',
    ])
  ) {
    return {
      title: '任务未完成',
      message: '暂时无法连接模型服务，请稍后继续执行。',
    };
  }

  if (
    (errorCode === 'MODEL_PROVIDER_ERROR' && retriable) ||
    includesAny(normalized, [
      'http 409',
      'http 500',
      'http 502',
      'http 503',
      'http 504',
    ])
  ) {
    return {
      title: '任务未完成',
      message: '模型服务暂时不可用，请稍后继续执行。',
    };
  }

  if (errorCode === 'MODEL_PROVIDER_ERROR') {
    return {
      title: '任务未完成',
      message: '模型服务请求失败，请检查模型配置或输入内容。',
    };
  }

  if (/[㐀-鿿]/u.test(detail)) {
    return { title: '任务未完成', message: detail };
  }

  return { title: '任务未完成', message: '任务执行过程中出现异常，尚未完成。' };
}

export function normalizeWorkflowRecovery(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }

  const action = String(value.action || '')
    .trim()
    .toLowerCase();
  const message = String(value.message || '').trim();
  if (action !== 'continue' || !message) {
    return null;
  }

  return {
    action,
    label: (String(value.label || '').trim() || '继续执行').slice(0, 40),
    message: message.slice(0, 1000),
  };
}

export function findVisibleWorkflowRecoveryIndex(messages = []) {
  let latestRecoverableFailure = -1;
  let latestUserMessage = -1;

  messages.forEach((message, index) => {
    if (message?.role === 'user') {
      latestUserMessage = index;
    }
    if (
      message?.role === 'assistant' &&
      message?.status === 'error' &&
      message?.recovery
    ) {
      latestRecoverableFailure = index;
    }
  });

  if (
    latestRecoverableFailure < 0 ||
    latestUserMessage > latestRecoverableFailure
  ) {
    return -1;
  }
  return latestRecoverableFailure;
}
