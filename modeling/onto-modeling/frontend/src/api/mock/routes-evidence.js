// mock · 3.19 证据源（EV-01～EV-30）：数据源、采集与快照、证据浏览器
// EV-26 被谁引用在 routes-studio.js（Studio 证据卡先用），EV-29 锚点解析在 index.js（所有页面的出处悬停）
export function register({ on, json, F }) {
  on('GET', '/evidence/sources', () => json(200, F.evidenceSources()));
}
