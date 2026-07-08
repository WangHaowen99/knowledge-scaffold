import { useCallback, useEffect, useState } from 'react';
import { authFetch } from '../utils/auth';
import { useAuthContext } from './useAuthContext';

// 知识包浏览的共享状态：侧边栏目录树（AppShell 内）与工作台内容区共用。
// 数据源：/agentic/ontologies + /tree（整包结构，后端已排除 origin_data/datasets）+ /raw-file。
// 管理操作走既有本体端点：启用/禁用 PUT /status、激活版本 PUT /active-version、
// 删除版本 DELETE /versions/<v>、下载 GET /download（均支持 version 参数按版本操作）。
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
  // versionOverride = null 表示跟随激活版本；字符串表示浏览指定历史版本
  const [versionOverride, setVersionOverride] = useState(null);
  const [opBusy, setOpBusy] = useState('');
  const [opNotice, setOpNotice] = useState('');

  const pkg = packages.find((p) => p.id === pkgId) || null;
  const activeVersion = pkg?.active_version || pkg?.version || '';
  const viewVersion = versionOverride ?? activeVersion;
  const viewingActive = versionOverride === null || versionOverride === activeVersion;

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

  // 浏览中的版本从包里消失时（如刚被删除）回落到激活版本
  useEffect(() => {
    if (versionOverride !== null && pkg && !(pkg.versions || []).includes(versionOverride)) {
      setVersionOverride(null);
    }
  }, [pkg, versionOverride]);

  useEffect(() => {
    if (!pkgId) { setTree([]); return undefined; }
    let cancelled = false;
    const t = setTimeout(async () => {
      setTreeLoading(true);
      try {
        const q = versionOverride !== null ? `?version=${encodeURIComponent(versionOverride)}` : '';
        const res = await authFetch(`/agentic/ontologies/${encodeURIComponent(pkgId)}/tree${q}`);
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
  }, [pkgId, workspaceId, versionOverride]);

  const selectPkg = useCallback((id) => { setPkgId(id); setVersionOverride(null); setOpNotice(''); }, []);
  const selectDir = useCallback((dirPath) => { setSel({ dir: dirPath, file: null }); }, []);
  const selectFile = useCallback((filePath) => {
    const parent = filePath.includes('/') ? filePath.slice(0, filePath.lastIndexOf('/')) : '';
    setSel({ dir: parent, file: filePath });
  }, []);
  const selectVersion = useCallback((version) => {
    setVersionOverride(version === activeVersion ? null : version);
  }, [activeVersion]);

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

  const setPkgEnabled = useCallback(async (enabled) => {
    if (!pkgId) return;
    setOpBusy('status');
    setOpNotice('');
    try {
      const res = await authFetch(`/agentic/ontologies/${encodeURIComponent(pkgId)}/status`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        setOpNotice(enabled ? '知识包已启用' : '知识包已禁用，Agent 将不再使用该知识包');
        await loadPackages();
      } else {
        setOpNotice(`操作失败：${data.error || data.detail || res.status}`);
      }
    } finally {
      setOpBusy('');
    }
  }, [pkgId, loadPackages]);

  const activateVersion = useCallback(async () => {
    if (!pkgId || !viewVersion) return;
    setOpBusy('activate');
    setOpNotice('');
    try {
      const res = await authFetch(`/agentic/ontologies/${encodeURIComponent(pkgId)}/active-version`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: viewVersion }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        setOpNotice(`已激活版本 ${viewVersion}`);
        setVersionOverride(null);
        await loadPackages();
      } else {
        setOpNotice(`激活失败：${data.error || data.detail || res.status}`);
      }
    } finally {
      setOpBusy('');
    }
  }, [pkgId, viewVersion, loadPackages]);

  const deleteVersion = useCallback(async () => {
    if (!pkgId || !viewVersion) return;
    setOpBusy('deleteVersion');
    setOpNotice('');
    try {
      const res = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(pkgId)}/versions/${encodeURIComponent(viewVersion)}`,
        { method: 'DELETE' },
      );
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        setOpNotice(`已删除版本 ${viewVersion}`);
        setVersionOverride(null);
        await loadPackages();
      } else {
        setOpNotice(`删除失败：${data.error || data.detail || res.status}`);
      }
    } finally {
      setOpBusy('');
    }
  }, [pkgId, viewVersion, loadPackages]);

  const downloadPkg = useCallback(async () => {
    if (!pkgId) return;
    setOpBusy('download');
    setOpNotice('');
    try {
      const q = viewVersion ? `?version=${encodeURIComponent(viewVersion)}` : '';
      const res = await authFetch(`/agentic/ontologies/${encodeURIComponent(pkgId)}/download${q}`);
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setOpNotice(`下载失败：${data.error || data.detail || res.status}`);
        return;
      }
      const blob = await res.blob();
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
      const fileName = match
        ? decodeURIComponent(match[1])
        : `${pkgId}${viewVersion ? `-${viewVersion}` : ''}.zip`;
      const objectUrl = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(objectUrl);
      setOpNotice(`已下载：${fileName}`);
    } finally {
      setOpBusy('');
    }
  }, [pkgId, viewVersion]);

  return {
    canImport, canManage: canImport, packages, pkgsLoading, pkg, pkgId, tree, treeLoading,
    sel, selectPkg, selectDir, selectFile, importZip, importing, notice,
    viewVersion, viewingActive, versionOverride, selectVersion,
    setPkgEnabled, activateVersion, deleteVersion, downloadPkg, opBusy, opNotice,
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
