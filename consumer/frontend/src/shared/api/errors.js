// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
export function friendlyRequestError(error, fallback = '请求失败') {
  const message = String(error?.message || '').trim();
  if (!message || /failed to fetch|networkerror|load failed|network request failed/i.test(message)) {
    return `${fallback}：网络异常或后端服务不可用`;
  }
  return message;
}
