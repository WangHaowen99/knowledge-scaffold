// 3.13 待处理（IN-01～IN-04）
export function register({ on, json, F }) {
  on('GET', '/projects/([^/]+)/inbox', () => json(200, F.inboxCounts())); // IN-01
}
