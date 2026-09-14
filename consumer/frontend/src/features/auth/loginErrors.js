// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
const LOGIN_ERROR_MESSAGES = Object.freeze({
  invalidCredentials: '账号或密码错误，请重新输入。',
  rateLimited: '登录尝试过于频繁，请稍后再试。',
  unavailable: '认证服务暂不可用，请稍后重试。',
  timeout: '登录请求超时，请稍后重试。',
  network: '无法连接认证服务，请检查网络后重试。',
  generic: '登录失败，请稍后重试。',
  server: '登录失败，请稍后重试；如持续出现，请联系管理员。',
});

/**
 * Convert login transport/server failures to stable, non-sensitive UI copy.
 * The server deliberately returns one generic 401 response; never surface its
 * raw error text because it may reveal account or authentication details.
 */
export const getLoginErrorMessage = ({ status, error, name, code } = {}) => {
  const rawMessage = typeof error === 'string' ? error : '';
  if (status === 401 || status === 403) {
    return LOGIN_ERROR_MESSAGES.invalidCredentials;
  }
  if (status === 429) {
    return LOGIN_ERROR_MESSAGES.rateLimited;
  }
  if (status === 503) {
    return LOGIN_ERROR_MESSAGES.unavailable;
  }
  if (
    name === 'AuthRequestTimeoutError' ||
    code === 'AUTH_REQUEST_TIMEOUT' ||
    /超时|timeout/i.test(rawMessage)
  ) {
    return LOGIN_ERROR_MESSAGES.timeout;
  }
  if (name === 'TypeError' || code === 'ERR_NETWORK' || code === 'ECONNRESET') {
    return LOGIN_ERROR_MESSAGES.network;
  }
  if (Number.isInteger(status) && status >= 500) {
    return LOGIN_ERROR_MESSAGES.server;
  }
  return LOGIN_ERROR_MESSAGES.generic;
};

export default getLoginErrorMessage;
