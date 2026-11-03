// 哈希路由：#/login、#/projects、#/projects/new、#/projects/import、#/p/<项目>/<页面>、#/<全局页>
import { useEffect, useState, useTransition } from 'react';

export function parseHash(hash) {
  const raw = (hash || '').replace(/^#/, '') || '/';
  const [path, qs = ''] = raw.split('?');
  const query = Object.fromEntries(new URLSearchParams(qs));
  const seg = path.split('/').filter(Boolean).map(decodeURIComponent);
  if (seg[0] === 'p' && seg[1]) return { name: 'project', projectId: seg[1], page: seg[2] || 'wb-l2', query };
  if (seg[0] === 'projects' && seg[1] === 'new') return { name: 'create', query };
  if (seg[0] === 'projects' && seg[1] === 'import') return { name: 'import', query };
  if (seg[0] === 'projects' || seg.length === 0) return { name: seg.length ? 'projects' : 'home', query };
  return { name: seg[0], query };
}

export function hrefFor(target, query) {
  const qs = query && Object.keys(query).length ? `?${new URLSearchParams(query)}` : '';
  return `#${target}${qs}`;
}

// 页面 id → 路径（项目内页面需要项目 id）
export function pagePath(page, projectId) {
  if (page === 'projects') return '/projects';
  if (page === 'create') return '/projects/new';
  if (page === 'import') return '/projects/import';
  if (['login', 'plugins', 'plugin-detail', 'plugin-upload', 'me', 'sys', 'ops', 'ops-obs', 'ops-ctl', 'ops-users'].includes(page)) return `/${page}`;
  return projectId ? `/p/${encodeURIComponent(projectId)}/${page}` : '/projects';
}

export function navigate(path, query) {
  window.location.hash = hrefFor(path, query);
}

// 换页放进 transition：新页面的代码块没下载完之前保留上一页（已显示的 Suspense 不退回 fallback），pending 为 true
export function useRouteState() {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  const [pending, startTransition] = useTransition();
  useEffect(() => {
    const h = () => startTransition(() => setRoute(parseHash(window.location.hash)));
    window.addEventListener('hashchange', h);
    return () => window.removeEventListener('hashchange', h);
  }, []);
  return [route, pending];
}

export function useRoute() {
  return useRouteState()[0];
}
