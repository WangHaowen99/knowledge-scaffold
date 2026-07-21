import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ChevronDown, ChevronRight, FileText, Folder, PackageOpen, RefreshCw } from 'lucide-react';
import { authFetch } from '../../../shared/api/auth';
import { friendlyRequestError } from '../../../shared/api/errors';
import { useAuthContext } from '../../auth';
import { DIR_LABELS } from '../hooks/useKbBrowser';
import {
  buildVisibleTreeRows,
  directoryExpansionKey,
  packageExpansionKey,
  readKbTreeExpansion,
  toggleExpandedKey,
  writeKbTreeExpansion,
} from './kbTreeState';
import './kbSidebarTree.css';

const emptyTreeState = Object.freeze({ entries: [], loading: false, error: '', version: '' });

const browserStorage = () => {
  try { return window.localStorage; } catch { return null; }
};

export default function KbSidebarTree({ kb }) {
  const { workspaceId } = useAuthContext();
  const {
    packages, pkgsLoading, packagesError, retryPackages,
    pkgId, tree, treeLoading, treeError, retryTree,
    sel, selectPkg, selectDir, selectFile, viewVersion,
  } = kb;
  const [expansion, setExpansion] = useState({ workspaceId: '', keys: new Set(), initialized: false });
  const [treeCache, setTreeCache] = useState({});
  const treeCacheRef = useRef(treeCache);
  const requestsRef = useRef(new Map());
  const workspaceRef = useRef(workspaceId);
  treeCacheRef.current = treeCache;
  workspaceRef.current = workspaceId;

  const expandedKeys = useMemo(
    () => (expansion.workspaceId === workspaceId ? expansion.keys : new Set()),
    [expansion.keys, expansion.workspaceId, workspaceId],
  );

  const persistExpansion = useCallback((updater) => {
    setExpansion((current) => {
      const currentKeys = current.workspaceId === workspaceId ? current.keys : new Set();
      const nextKeys = updater(currentKeys);
      writeKbTreeExpansion(browserStorage(), workspaceId, nextKeys);
      return { workspaceId, keys: nextKeys, initialized: true };
    });
  }, [workspaceId]);

  const loadPackageTree = useCallback(async (pkg, { force = false } = {}) => {
    const packageId = String(pkg?.id || '');
    if (!packageId || packageId === pkgId || !workspaceId) return;
    const version = String(pkg.active_version || pkg.version || '');
    const cached = treeCacheRef.current[packageId];
    if (!force && cached?.version === version && (cached.loading || cached.loaded)) return;

    requestsRef.current.get(packageId)?.abort();
    const controller = new AbortController();
    requestsRef.current.set(packageId, controller);
    const requestWorkspace = workspaceId;
    setTreeCache((current) => ({
      ...current,
      [packageId]: { entries: [], loading: true, error: '', loaded: false, version },
    }));
    try {
      const query = version ? `?version=${encodeURIComponent(version)}` : '';
      const response = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(packageId)}/tree${query}`,
        { signal: controller.signal },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || payload.detail || `知识包目录加载失败（${response.status}）`);
      }
      if (controller.signal.aborted || requestWorkspace !== workspaceRef.current) return;
      setTreeCache((current) => ({
        ...current,
        [packageId]: {
          entries: payload.entries || [],
          loading: false,
          error: '',
          loaded: true,
          version,
        },
      }));
    } catch (error) {
      if (controller.signal.aborted || requestWorkspace !== workspaceRef.current) return;
      setTreeCache((current) => ({
        ...current,
        [packageId]: {
          entries: [],
          loading: false,
          error: friendlyRequestError(error, '知识包目录加载失败'),
          loaded: false,
          version,
        },
      }));
    } finally {
      if (requestsRef.current.get(packageId) === controller) {
        requestsRef.current.delete(packageId);
      }
    }
  }, [pkgId, workspaceId]);

  useEffect(() => {
    for (const controller of requestsRef.current.values()) controller.abort();
    requestsRef.current.clear();
    setTreeCache({});
    const stored = readKbTreeExpansion(browserStorage(), workspaceId);
    setExpansion({
      workspaceId,
      keys: stored || new Set(),
      initialized: stored !== null,
    });
  }, [workspaceId]);

  useEffect(() => () => {
    for (const controller of requestsRef.current.values()) controller.abort();
    requestsRef.current.clear();
  }, []);

  useEffect(() => {
    if (expansion.workspaceId !== workspaceId || expansion.initialized || !pkgId) return;
    const nextKeys = new Set(expansion.keys);
    nextKeys.add(packageExpansionKey(pkgId));
    writeKbTreeExpansion(browserStorage(), workspaceId, nextKeys);
    setExpansion({ workspaceId, keys: nextKeys, initialized: true });
  }, [expansion, pkgId, workspaceId]);

  useEffect(() => {
    if (expansion.workspaceId !== workspaceId) return;
    for (const pkg of packages) {
      if (pkg.id === pkgId || !expandedKeys.has(packageExpansionKey(pkg.id))) continue;
      void loadPackageTree(pkg);
    }
  }, [expandedKeys, expansion.workspaceId, loadPackageTree, packages, pkgId, workspaceId]);

  const togglePackage = (pkg) => {
    const key = packageExpansionKey(pkg.id);
    const opening = !expandedKeys.has(key);
    persistExpansion((current) => toggleExpandedKey(current, key));
    if (opening && pkg.id !== pkgId) void loadPackageTree(pkg);
  };

  const toggleDirectory = (packageId, path) => {
    const key = directoryExpansionKey(packageId, path);
    persistExpansion((current) => toggleExpandedKey(current, key));
  };

  const selectPackageEntry = (packageId, entry) => {
    if (packageId !== pkgId) selectPkg(packageId);
    if (entry.type === 'dir') selectDir(entry.path);
    else selectFile(entry.path);
  };

  return (
    <div className="kbtree" aria-label="知识包目录">
      {pkgsLoading && <div className="kbt-empty">加载中…</div>}
      {!pkgsLoading && packagesError && <div className="kbt-error"><AlertCircle size={13} /><span>{packagesError}</span><button type="button" onClick={retryPackages}><RefreshCw size={12} />重试</button></div>}
      {!pkgsLoading && !packagesError && !packages.length && <div className="kbt-empty">暂无知识包</div>}
      {packages.map((pkg) => {
        const packageExpanded = expandedKeys.has(packageExpansionKey(pkg.id));
        const packageTree = pkg.id === pkgId
          ? { entries: tree, loading: treeLoading, error: treeError, loaded: true, version: viewVersion }
          : treeCache[pkg.id] || emptyTreeState;
        const rows = buildVisibleTreeRows(packageTree.entries, expandedKeys, pkg.id);
        return (
          <div key={pkg.id} className="kbt-package">
            <div className={`kbt-pkg ${pkg.id === pkgId ? 'on' : ''}`}>
              <button type="button" className="kbt-toggle" aria-expanded={packageExpanded}
                aria-label={`${packageExpanded ? '折叠' : '展开'}知识包 ${pkg.name || pkg.id}`}
                onClick={() => togglePackage(pkg)}>
                {packageExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              </button>
              <button type="button" className="kbt-pkg-select" onClick={() => selectPkg(pkg.id)}>
                <PackageOpen className="kbt-icon" size={14} />
                <span className="nm">{pkg.name || pkg.id}</span>
              </button>
            </div>
            {packageExpanded && (
              <div className="kbt-kids">
                {packageTree.loading && <div className="kbt-empty">读取目录…</div>}
                {!packageTree.loading && packageTree.error && (
                  <div className="kbt-error"><AlertCircle size={13} /><span>{packageTree.error}</span>
                    <button type="button" onClick={() => (pkg.id === pkgId ? retryTree() : loadPackageTree(pkg, { force: true }))}><RefreshCw size={12} />重试</button>
                  </div>
                )}
                {!packageTree.loading && !packageTree.error && rows.map((row) => {
                  const { entry } = row;
                  const selected = pkg.id === pkgId && (
                    entry.type === 'dir'
                      ? sel.dir === entry.path && !sel.file
                      : sel.file === entry.path
                  );
                  return (
                    <div key={entry.path} className={`kbt-row ${selected ? 'on' : ''}`}
                      style={{ paddingLeft: `${2 + row.depth * 14}px` }}>
                      {row.hasChildren ? (
                        <button type="button" className="kbt-toggle" aria-expanded={row.expanded}
                          aria-label={`${row.expanded ? '折叠' : '展开'}目录 ${entry.name}`}
                          onClick={() => toggleDirectory(pkg.id, entry.path)}>
                          {row.expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                        </button>
                      ) : <span className="kbt-toggle-spacer" />}
                      <button type="button" className="kbt-entry-select"
                        onClick={() => selectPackageEntry(pkg.id, entry)} title={entry.path}>
                        <span className="kbt-label">
                          {entry.type === 'dir' ? <Folder size={13} /> : <FileText size={13} />}
                          {entry.name}{entry.type === 'dir' ? '/' : ''}
                          {entry.type === 'dir' && <span className="lb">{DIR_LABELS[entry.name] || ''}</span>}
                        </span>
                      </button>
                      {entry.type === 'dir' && <span className="cnt">{(entry.children || []).length}</span>}
                    </div>
                  );
                })}
                <div className="kbt-meta">
                  v{pkg.active_version || pkg.version} · {pkg.enabled ? '已启用' : '已停用'}
                  {pkg.business_data?.configured
                    ? ` · 数据源 ${pkg.business_data.provider || '已配置'}`
                    : ' · 未配置数据源'}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
