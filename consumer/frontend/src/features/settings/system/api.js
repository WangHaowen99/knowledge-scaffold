import { authFetch } from '../../../shared/api/auth.js';
import { assertSystemConfigSection } from './model.js';

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function responseError(response, payload, fallback) {
  const message =
    payload?.detail ||
    payload?.error ||
    payload?.message ||
    `${fallback}（HTTP ${response.status}）`;
  const error = new Error(message);
  error.status = response.status;
  error.code = payload?.code || '';
  return error;
}

export async function requestSystemConfigJson(
  request,
  path,
  options = {},
  fallback = '系统配置请求失败',
) {
  const response = await request(path, options);
  const payload = await readJson(response);
  if (!response.ok) {
    throw responseError(response, payload, fallback);
  }
  return payload;
}

export function loadSystemConfig(request = authFetch) {
  return requestSystemConfigJson(
    request,
    '/api/system-config',
    {},
    '加载系统配置失败',
  );
}

export function saveSystemConfigSection(section, data, request = authFetch) {
  const normalizedSection = assertSystemConfigSection(section);
  return requestSystemConfigJson(
    request,
    '/api/system-config',
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section: normalizedSection, data: data || {} }),
    },
    '保存系统配置失败',
  );
}

export function testSystemConfigSection(section, data, request = authFetch) {
  const normalizedSection = assertSystemConfigSection(section);
  return requestSystemConfigJson(
    request,
    '/api/system-config/test',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section: normalizedSection, data: data || {} }),
    },
    '测试系统配置失败',
  );
}

export function importSystemConfig(config, request = authFetch) {
  return requestSystemConfigJson(
    request,
    '/api/system-config/import',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: config || {} }),
    },
    '导入系统配置失败',
  );
}

export function systemConfigTemplateFileName(contentDisposition = '') {
  const extendedMatch = String(contentDisposition).match(
    /filename\*=UTF-8''(?<fileName>[^;]+)/i,
  );
  if (extendedMatch) {
    const encodedName = extendedMatch.groups.fileName;
    try {
      return decodeURIComponent(encodedName.trim().replace(/^"|"$/g, ''));
    } catch {
      // Fall through to the regular filename parser.
    }
  }
  const regularName =
    String(contentDisposition).match(/filename\s*=\s*"?(?<fileName>[^";]+)"?/i)
      ?.groups.fileName || '';
  return regularName.trim() || 'system_config.template.json';
}

export async function fetchSystemConfigTemplate(request = authFetch) {
  const response = await request('/api/system-config/template');
  if (!response.ok) {
    const payload = await readJson(response);
    throw responseError(response, payload, '下载系统配置模板失败');
  }
  return {
    blob: await response.blob(),
    fileName: systemConfigTemplateFileName(
      response.headers.get('content-disposition') || '',
    ),
  };
}
