import { AlertCircle, ChevronDown, ChevronRight, FileText, Folder, PackageOpen, RefreshCw } from 'lucide-react';
import { DIR_LABELS } from './useKbBrowser';

// 应用侧边栏里的知识包目录树：挂在“知识包工作台”导航项下方展开。
// 顶层与知识包实际结构一致：目录（objects/ 等）+ 包根文件（manifest.yaml、index.md…）。
export default function KbSidebarTree({ kb }) {
  const { packages, pkgsLoading, packagesError, retryPackages, pkgId, tree, treeLoading, treeError, retryTree, sel, selectPkg, selectDir, selectFile } = kb;

  return (
    <div className="kbtree">
      {pkgsLoading && <div className="kbt-empty">加载中…</div>}
      {!pkgsLoading && packagesError && <div className="kbt-error"><AlertCircle size={13} /><span>{packagesError}</span><button type="button" onClick={retryPackages}><RefreshCw size={12} />重试</button></div>}
      {!pkgsLoading && !packagesError && !packages.length && <div className="kbt-empty">暂无知识包</div>}
      {packages.map((p) => (
        <div key={p.id}>
          <button className={`kbt-pkg ${p.id === pkgId ? 'on' : ''}`} onClick={() => selectPkg(p.id)}>
            <span className="caret">{p.id === pkgId ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</span>
            <PackageOpen className="kbt-icon" size={14} />
            <span className="nm">{p.name || p.id}</span>
          </button>
          {p.id === pkgId && (
            <div className="kbt-kids">
              {treeLoading && <div className="kbt-empty">读取目录…</div>}
              {!treeLoading && treeError && <div className="kbt-error"><AlertCircle size={13} /><span>{treeError}</span><button type="button" onClick={retryTree}><RefreshCw size={12} />重试</button></div>}
              {!treeLoading && !treeError && tree.map((e) => (
                e.type === 'dir' ? (
                  <button key={e.path} className={`kbt-row ${sel.dir === e.path && !sel.file ? 'on' : ''}`}
                    onClick={() => selectDir(e.path)} title={`${e.path}/`}>
                    <span className="kbt-label"><Folder size={13} />{e.name}/ <span className="lb">{DIR_LABELS[e.name] || ''}</span></span>
                    <span className="cnt">{(e.children || []).length}</span>
                  </button>
                ) : (
                  <button key={e.path} className={`kbt-row ${sel.file === e.path ? 'on' : ''}`}
                    onClick={() => selectFile(e.path)} title={e.path}>
                    <span className="kbt-label"><FileText size={13} />{e.name}</span>
                  </button>
                )
              ))}
              <div className="kbt-meta">
                v{p.active_version || p.version} · {p.enabled ? '已启用' : '已停用'}
                {p.business_data?.connection_name ? ` · 绑定 ${p.business_data.connection_name}` : ' · 未绑定数据'}
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
