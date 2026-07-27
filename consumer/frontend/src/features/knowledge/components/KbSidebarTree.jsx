import { useCallback, useMemo, useState } from 'react';
import { AlertCircle, ChevronDown, ChevronRight, FileText, Folder, GitBranch, PackageOpen, RefreshCw } from 'lucide-react';
import { useAuthContext } from '../../auth';
import { DIR_LABELS } from '../hooks/useKbBrowser';
import {
  buildVisibleTreeRows,
  directoryExpansionKey,
  isVersionExpanded,
  packageExpansionKey,
  packageVersions,
  readKbTreeExpansion,
  toggleExpandedKey,
  versionCollapseKey,
  writeKbTreeExpansion,
} from './kbTreeState';
import './kbSidebarTree.css';

const browserStorage = () => {
  try { return window.localStorage; } catch { return null; }
};

export default function KbSidebarTree({ kb }) {
  const { workspaceId } = useAuthContext();
  const {
    packages, pkgsLoading, packagesError, retryPackages,
    pkgId, tree, treeLoading, treeError, retryTree,
    sel, selectPkg, selectDir, selectFile, selectPackageVersion, viewVersion,
  } = kb;
  const [workspaceExpansions, setWorkspaceExpansions] = useState({});
  const storedExpansion = useMemo(
    () => readKbTreeExpansion(browserStorage(), workspaceId),
    [workspaceId],
  );

  const expandedKeys = useMemo(
    () => {
      if (workspaceExpansions[workspaceId]) return workspaceExpansions[workspaceId];
      const initial = new Set(storedExpansion || []);
      if (storedExpansion === null && pkgId) initial.add(packageExpansionKey(pkgId));
      return initial;
    },
    [pkgId, storedExpansion, workspaceExpansions, workspaceId],
  );

  const persistExpansion = useCallback((updater) => {
    setWorkspaceExpansions((current) => {
      const currentKeys = current[workspaceId] || expandedKeys;
      const nextKeys = updater(currentKeys);
      writeKbTreeExpansion(browserStorage(), workspaceId, nextKeys);
      return { ...current, [workspaceId]: nextKeys };
    });
  }, [expandedKeys, workspaceId]);

  const togglePackage = (pkg) => {
    const key = packageExpansionKey(pkg.id);
    persistExpansion((current) => toggleExpandedKey(current, key));
  };

  const toggleDirectory = (packageId, version, path) => {
    const key = directoryExpansionKey(packageId, version, path);
    persistExpansion((current) => toggleExpandedKey(current, key));
  };

  const toggleVersion = (packageId, version, selectedVersion) => {
    const key = versionCollapseKey(packageId, version);
    if (!selectedVersion) {
      selectPackageVersion(packageId, version);
      persistExpansion((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
      return;
    }
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
        const activeVersion = pkg.active_version || pkg.version || '';
        const versions = packageVersions(pkg);
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
                {versions.map((version) => {
                  const selectedVersion = pkg.id === pkgId && version === viewVersion;
                  const versionExpanded = isVersionExpanded(
                    selectedVersion,
                    expandedKeys,
                    pkg.id,
                    version,
                  );
                  const rows = versionExpanded
                    ? buildVisibleTreeRows(tree, expandedKeys, pkg.id, version)
                    : [];
                  return (
                    <div key={version} className="kbt-version-group">
                      <div className={`kbt-version-head ${selectedVersion ? 'on' : ''}`}>
                        <button type="button" className="kbt-toggle" aria-expanded={versionExpanded}
                          aria-label={`${versionExpanded ? '折叠' : '展开'}版本 ${version}`}
                          onClick={() => toggleVersion(pkg.id, version, selectedVersion)}>
                          {versionExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                        </button>
                        <button type="button" className="kbt-version"
                          aria-current={selectedVersion ? 'true' : undefined}
                          onClick={() => selectPackageVersion(pkg.id, version)}>
                          <GitBranch size={13} />
                          <span className="kbt-version-name">{version}</span>
                          {version === activeVersion && <span className="kbt-active">激活</span>}
                        </button>
                      </div>
                      {versionExpanded && (
                        <div className="kbt-version-kids">
                          {treeLoading && <div className="kbt-empty">读取目录…</div>}
                          {!treeLoading && treeError && (
                            <div className="kbt-error"><AlertCircle size={13} /><span>{treeError}</span>
                              <button type="button" onClick={retryTree}><RefreshCw size={12} />重试</button>
                            </div>
                          )}
                          {!treeLoading && !treeError && rows.map((row) => {
                            const { entry } = row;
                            const selected = entry.type === 'dir'
                              ? sel.dir === entry.path && !sel.file
                              : sel.file === entry.path;
                            return (
                              <div key={entry.path} className={`kbt-row ${selected ? 'on' : ''}`}
                                style={{ paddingLeft: `${2 + row.depth * 14}px` }}>
                                {row.hasChildren ? (
                                  <button type="button" className="kbt-toggle" aria-expanded={row.expanded}
                                    aria-label={`${row.expanded ? '折叠' : '展开'}目录 ${entry.name}`}
                                    onClick={() => toggleDirectory(pkg.id, version, entry.path)}>
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
                        </div>
                      )}
                    </div>
                  );
                })}
                <div className="kbt-meta">
                  {pkg.enabled ? '已启用' : '已停用'}
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
