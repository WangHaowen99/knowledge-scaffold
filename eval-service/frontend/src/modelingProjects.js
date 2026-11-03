import { useEffect, useState } from 'react';
import { api } from './api.js';
import { modelingProjectLabel, modelingProjectQueries, packageIds } from './domain.js';

// Display-only names from onto-modeling, fetched again only when the listed packages change.
export function useModelingProjects(runs) {
  const [projects, setProjects] = useState({ available: true, items: {} });
  const ids = packageIds(runs),
    key = ids.join(',');
  useEffect(() => {
    if (!ids.length) return undefined;
    let live = true;
    (async () => {
      const items = {};
      let available = true;
      for (const query of modelingProjectQueries(ids)) {
        const page = await api(`/modeling-projects?${query}`);
        available = available && page.available;
        for (const item of page.items) items[item.ontology_id] = item;
      }
      if (live) setProjects({ available, items });
    })().catch(() => live && setProjects({ available: false, items: {} }));
    return () => {
      live = false;
    };
  }, [key]);
  return (run) => {
    const entry = projects.items[run.package_ref?.ontology_id];
    const title = entry?.projects?.length
      ? entry.projects.join('、') + (entry.total > entry.projects.length ? ` 等 ${entry.total} 个` : '')
      : projects.available
        ? '当前账号没有关联该知识包的建模项目'
        : '建模服务暂不可用，无法显示项目名称';
    return { label: modelingProjectLabel(entry), title };
  };
}
