// 外部接口与路由的命名：`<方法> <地址>`，方法不知道就只写地址；查询串和片段去掉（evidence-collection.md §2 `ext`）。
const VERBS = {
  get: 'GET', post: 'POST', put: 'PUT', patch: 'PATCH', delete: 'DELETE', del: 'DELETE', head: 'HEAD', options: 'OPTIONS',
};

/** `call:axios.get`、`call:router.post` 这类调用名里的 HTTP 动词；`call:fetch` 看不出方法，返回 null。 */
export function httpMethod(mechanism) {
  const match = /[.:]([A-Za-z]+)$/.exec(String(mechanism ?? ''));
  return match ? VERBS[match[1].toLowerCase()] ?? null : null;
}

export function stripQuery(url) {
  return String(url).trim().split(/[?#]/, 1)[0];
}

export function endpointName(method, url) {
  return method ? `${method} ${url}` : url;
}

export const isHttpUrl = (text) => /^https?:\/\/[^\s/]+/i.test(String(text));
