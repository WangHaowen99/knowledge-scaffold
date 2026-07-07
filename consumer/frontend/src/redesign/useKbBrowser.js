import { useCallback, useEffect, useState } from 'react';
import { authFetch } from '../utils/auth';
import { useAuthContext } from './useAuthContext';

// 知识包浏览的共享状态：侧边栏目录树（AppShell 内）与工作台内容区共用。
// 数据源：/agentic/ontologies + /tree（整包结构，后端已排除 origin_data/datasets）+ /raw-file。
export const DIR_LABELS = {
  objects: '业务对象', operations: '操作', scripts: '脚本', mappings: '数据映射',
};
export const DIR_ICONS = { objects: '▤', operations: '⚙', scripts: '𝑓', mappings: '⇄' };

export default function useKbBrowser() {
  const { can, workspaceId } = useAuthContext();
  const canImport = can('ws.kb.write');
  const [packages, setPackages] = useState([]);
  const [pkgsLoading, setPkgsLoading] = useState(true);
  const [pkgId, setPkgId] = useState(null);
  const [tree, setTree] = useState([]);
  const [treeLoading, setTreeLoading] = useState(false);
  // sel.dir = 当前目录相对路径（'' 为包根）；sel.file = 当前文件相对路径或 null
  const [sel, setSel] = useState({ dir: 'objects', file: null });
  const [importing, setImporting] = useState(false);
  const [notice, setNotice] = useState('');

  const loadPackages = useCallback(async () => {
    setPkgsLoading(true);
    try {
      const res = await authFetch('/agentic/ontologies');
      const data = res.ok ? await res.json() : { ontologies: [] };
      const list = data.ontologies || [];
      setPackages(list);
      setPkgId((prev) => (prev && list.some((p) => p.id === prev) ? prev : list[0]?.id || null));
    } catch {
      setPackages([]);
    } finally {
      setPkgsLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = setTimeout(loadPackages, 0);
    return () => clearTimeout(t);
  }, [loadPackages, workspaceId]);

  useEffect(() => {
    if (!pkgId) { setTree([]); return undefined; }
    let cancelled = false;
    const t = setTimeout(async () => {
      setTreeLoading(true);
      try {
        const res = await authFetch(`/agentic/ontologies/${encodeURIComponent(pkgId)}/tree`);
        const data = res.ok ? await res.json() : { entries: [] };
        if (cancelled) return;
        const entries = data.entries || [];
        setTree(entries);
        const firstDir = entries.find((e) => e.type === 'dir' && e.name === 'objects')
          || entries.find((e) => e.type === 'dir');
        setSel({ dir: firstDir ? firstDir.path : '', file: null });
      } finally {
        if (!cancelled) setTreeLoading(false);
      }
    }, 0);
    return () => { cancelled = true; clearTimeout(t); };
  }, [pkgId, workspaceId]);

  const selectPkg = useCallback((id) => { setPkgId(id); }, []);
  const selectDir = useCallback((dirPath) => { setSel({ dir: dirPath, file: null }); }, []);
  const selectFile = useCallback((filePath) => {
    const parent = filePath.includes('/') ? filePath.slice(0, filePath.lastIndexOf('/')) : '';
    setSel({ dir: parent, file: filePath });
  }, []);

  const importZip = useCallback(async (file) => {
    setImporting(true);
    setNotice('');
    try {
      const form = new FormData();
      form.append('file', file);
      let res = await authFetch('/agentic/ontologies/import-zip', { method: 'POST', body: form });
      let data = await res.json().catch(() => ({}));
      if (res.status === 409 && data.requires_confirmation) {
        const ok = window.confirm(`知识包「${data.ontology_name || data.ontology_id}」已存在，是否覆盖当前激活版本？`);
        if (!ok) return;
        const retry = new FormData();
        retry.append('file', file);
        retry.append('action', 'overwrite_active');
        res = await authFetch('/agentic/ontologies/import-zip', { method: 'POST', body: retry });
        data = await res.json().catch(() => ({}));
      }
      if (res.ok) {
        setNotice(`导入成功：${data.ontology_name || data.ontology_id}（对象 ${data.objects_count ?? '-'} · 操作 ${data.operations_count ?? '-'}）`);
        await loadPackages();
        setPkgId(data.ontology_id);
      } else {
        setNotice(`导入失败：${data.error || data.detail || res.status}`);
      }
    } finally {
      setImporting(false);
    }
  }, [loadPackages]);

  const pkg = packages.find((p) => p.id === pkgId) || null;
  return {
    canImport, packages, pkgsLoading, pkg, pkgId, tree, treeLoading,
    sel, selectPkg, selectDir, selectFile, importZip, importing, notice,
  };
}

// 在树中按路径找目录节点（'' 返回根）
export function findDirEntries(tree, dirPath) {
  if (!dirPath) return tree;
  let entries = tree;
  for (const part of dirPath.split('/')) {
    const node = entries.find((e) => e.type === 'dir' && e.name === part);
    if (!node) return [];
    entries = node.children || [];
  }
  return entries;
}
