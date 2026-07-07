import { DIR_ICONS, DIR_LABELS } from './useKbBrowser';

// 应用侧边栏里的知识包目录树：挂在“知识包工作台”导航项下方展开。
// 顶层与知识包实际结构一致：目录（objects/ 等）+ 包根文件（manifest.yaml、index.md…）。
export default function KbSidebarTree({ kb }) {
  const { packages, pkgsLoading, pkgId, tree, treeLoading, sel, selectPkg, selectDir, selectFile, notice } = kb;

  return (
    <div className="kbtree">
      {notice && <div className="kbt-notice">{notice}</div>}
      {pkgsLoading && <div className="kbt-empty">加载中…</div>}
      {!pkgsLoading && !packages.length && <div className="kbt-empty">暂无知识包</div>}
      {packages.map((p) => (
        <div key={p.id}>
          <button className={`kbt-pkg ${p.id === pkgId ? 'on' : ''}`} onClick={() => selectPkg(p.id)}>
            <span className="caret">{p.id === pkgId ? '▾' : '▸'}</span>
            <span className="nm">📦 {p.name || p.id}</span>
          </button>
          {p.id === pkgId && (
            <div className="kbt-kids">
              {treeLoading && <div className="kbt-empty">读取目录…</div>}
              {!treeLoading && tree.map((e) => (
                e.type === 'dir' ? (
                  <button key={e.path} className={`kbt-row ${sel.dir === e.path && !sel.file ? 'on' : ''}`}
                    onClick={() => selectDir(e.path)} title={`${e.path}/`}>
                    <span>{DIR_ICONS[e.name] || '▸'} {e.name}/ <span className="lb">{DIR_LABELS[e.name] || ''}</span></span>
                    <span className="cnt">{(e.children || []).length}</span>
                  </button>
                ) : (
                  <button key={e.path} className={`kbt-row ${sel.file === e.path ? 'on' : ''}`}
                    onClick={() => selectFile(e.path)} title={e.path}>
                    <span>≡ {e.name}</span>
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
