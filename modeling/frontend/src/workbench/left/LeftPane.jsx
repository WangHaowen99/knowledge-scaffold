import { memo, useEffect, useMemo, useState } from 'react';
import { ChevronRight, Folder, FolderOpen } from 'lucide-react';
import {
  buildPackageFileTree,
  listDirectoryPaths,
} from '../../packageFileTree.js';
import {
  directoryLabel,
  displayDirectoryPathsForFile,
  presentPackageFileTree,
} from '../../packageFileTreePresentation.js';
import { decodeLegacyArchiveName, displayPackageEntryName } from '../../legacyArchiveFilename.js';
import { store, actions } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import * as F from '../lib/mock/fixtures.js';
import Icon from '../ui/Icon.jsx';

const selTab = (s) => s.leftTab;
const selScene = (s) => s.scene;
const selPath = (s) => s.selectedPath;
const selMat = (s) => s.selectedMaterial;
const selDemo = (s) => s.demo;
const selFiles = (s) => s.files;
const selMaterials = (s) => s.materials;
const selLoading = (s) => s.loading;
const selFocus = (s) => s.focus;

export default function LeftPane() {
  const tab = useStore(store, selTab);
  const demo = useStore(store, selDemo);
  const materials = useStore(store, selMaterials);
  return (
    <>
      <div className="tabs-fill" role="tablist">
        <button role="tab" aria-selected={tab === 'pkg'} className={tab === 'pkg' ? 'on' : ''}
          onClick={() => actions.setLeftTab('pkg')}>知识包</button>
        <button role="tab" aria-selected={tab === 'mat'} className={tab === 'mat' ? 'on' : ''}
          onClick={() => actions.setLeftTab('mat')}>资料 <span className="wb-tab-count">{demo ? F.materials.length : materials.length}</span></button>
      </div>
      {tab === 'pkg' ? <PackageTree /> : <MaterialList />}
    </>
  );
}

/* ---------------- 知识包目录树 ---------------- */
function PackageTree() {
  const scene = useStore(store, selScene);
  const path = useStore(store, selPath);
  const demo = useStore(store, selDemo);
  const files = useStore(store, selFiles);
  const loading = useStore(store, selLoading);
  const focus = useStore(store, selFocus);
  const packageFiles = useMemo(
    () => files.filter((item) => !item.startsWith('.modeling/')),
    [files],
  );
  const tree = useMemo(() => presentPackageFileTree(buildPackageFileTree(packageFiles)), [packageFiles]);
  const allDirectories = useMemo(() => listDirectoryPaths(tree), [tree]);
  const directoryKey = allDirectories.join('|');
  const heat = demo ? scene === 'increment'
    : focus.kind === 'impact' && Boolean(focus.data) && focus.data.scopeResolved !== false;
  const hitPaths = demo
    ? F.impact.hitPaths
    : [...new Set([
      ...(focus.data?.todo?.items || []).map((item) => item.t),
      ...(focus.data?.chains || []).map((item) => item.dstPath || item.dst),
    ].filter(Boolean))];
  const [expanded, setExpanded] = useState(() => new Set([
    ...displayDirectoryPathsForFile(path),
    ...(heat ? allDirectories : []),
  ]));
  const changes = scene === 'confirm';

  useEffect(() => {
    if (demo) return;
    const valid = new Set(allDirectories);
    const ancestors = displayDirectoryPathsForFile(path);
    setExpanded((current) => new Set([
      ...[...current].filter((directory) => valid.has(directory)),
      ...ancestors,
      ...(heat ? allDirectories : []),
    ]));
  }, [demo, directoryKey, heat, path]);

  const toggleDirectory = (directory) => setExpanded((current) => {
    const next = new Set(current);
    if (next.has(directory)) next.delete(directory);
    else next.add(directory);
    return next;
  });

  if (!demo) {
    return (
      <>
        <div className="wb-scroll">
          <div className="wb-tree" role="tree" aria-label="知识包文件目录">
            <TreeNodes nodes={tree} expanded={expanded} onToggle={toggleDirectory}
              selectedPath={path} heat={heat} hitPaths={hitPaths} />
            {!files.length && <div className="empty t-sm">{loading ? '正在加载项目…' : '助手写入候选后，文件会出现在这里'}</div>}
          </div>
        </div>
        <div className="wb-legend">{heat
          ? <i><span className="dot dot-stale" />受新资料影响，需复核</i>
          : <><i><span className="dot dot-ok" />当前可查看</i><i><span className="dot dot-draft" />候选修改会实时覆盖</i></>}</div>
      </>
    );
  }

  if (changes) return <ChangeScope />;

  return (
    <>
      <div className="wb-scroll">
        <div className="wb-tree">
          {F.tree.map((sec, i) => (
            <div key={sec.g ?? `s${i}`}>
              {sec.g && <div className="grp">{sec.g}</div>}
              {sec.items.map((it) => (
                <Row key={it.path} item={it} depth={sec.g ? 1 : 0}
                  active={path === it.path} heat={heat} hitPaths={hitPaths} />
              ))}
            </div>
          ))}
          {heat && (
            <>
              <div className="grp">新资料</div>
              <Row item={{ path: '安全库存管理办法 v3', st: 'new' }} depth={1} fresh />
              <Row item={{ path: '仓库分级表', st: 'new' }} depth={1} fresh />
            </>
          )}
        </div>
      </div>
      <div className="wb-legend">
        {heat ? <i><span className="dot dot-stale" />受新资料影响，需复核</i> : (
          <>
            <i><span className="dot dot-ok" />已确认</i>
            <i><span className="dot dot-draft" />草稿</i>
            <i><span className="dot dot-wait" />待确认</i>
            <i><span className="dot dot-stale" />已过时</i>
          </>
        )}
      </div>
    </>
  );
}

/** index.md 之类的同名文件必须带上父目录，否则树里三个「index.md」分不清。 */
export function displayName(path) {
  return displayPackageEntryName(path);
}

const Row = memo(function Row({ item, depth, ariaLevel, active, heat, hitPaths = [], fresh }) {
  const hit = heat && hitPaths.includes(item.path);
  const dim = heat && !hit && !fresh;
  const name = displayName(item.path);
  const cls = ['wb-row', `i${depth}`, active && 'on', hit && 'hit', dim && 'dim', fresh && 'fresh']
    .filter(Boolean).join(' ');
  return (
    <button className={cls} role="treeitem" aria-level={ariaLevel || depth + 1} aria-selected={active}
      onClick={() => actions.selectPath(item.path)} title={item.path}>
      <span className={`dot dot-${hit ? 'stale' : item.st}`} />
      <span className="nm">{name}</span>
      {item.cap && <span className="cap">{item.cap}</span>}
      {hit && <span className="cap">需复核</span>}
    </button>
  );
});

function TreeNodes({ nodes, expanded, onToggle, selectedPath, heat, hitPaths, level = 1 }) {
  return nodes.map((node) => node.type === 'directory' ? (
    <DirectoryNode key={node.path} node={node} expanded={expanded} onToggle={onToggle}
      selectedPath={selectedPath} heat={heat} hitPaths={hitPaths} level={level} />
  ) : (
    <Row key={node.path} item={{ path: node.path, st: 'ok' }} depth={0} ariaLevel={level}
      active={selectedPath === node.path} heat={heat} hitPaths={hitPaths} />
  ));
}

function DirectoryNode({ node, expanded, onToggle, selectedPath, heat, hitPaths, level }) {
  const open = expanded.has(node.path);
  const name = decodeLegacyArchiveName(node.name);
  const label = directoryLabel(name);
  const containsActive = displayDirectoryPathsForFile(selectedPath).includes(node.path);
  return (
    <div className="wb-tree-directory">
      <button className={`wb-tree-dir${containsActive ? ' contains-active' : ''}`}
        role="treeitem" aria-level={level} aria-expanded={open}
        onClick={() => onToggle(node.path)} title={`${open ? '收起' : '展开'} ${node.path}`}>
        <ChevronRight className="wb-tree-chevron" size={13} aria-hidden="true" />
        {open ? <FolderOpen size={14} aria-hidden="true" /> : <Folder size={14} aria-hidden="true" />}
        <span className="nm wb-dir-name">
          <span className="wb-dir-name-en">{name}</span>
          {label && <> <span className="wb-dir-name-zh">{label}</span></>}
        </span>
        <span className="count">{node.fileCount}</span>
      </button>
      {open && (
        <div className="wb-tree-children" role="group">
          <TreeNodes nodes={node.children} expanded={expanded} onToggle={onToggle}
            selectedPath={selectedPath} heat={heat} hitPaths={hitPaths} level={level + 1} />
        </div>
      )}
    </div>
  );
}

/* ---------------- 变更范围（确认场景） ---------------- */
function ChangeScope() {
  const n = (t) => F.changeGroups.filter((g) => g.tier === t).length;
  const groups = [
    { label: `需要你判断 · ${n('caliber')}`, tier: 'caliber', items: [
      { p: 'rules/安全库存判断.md', cap: '新增', st: 'wait' },
      { p: 'metrics/周转率.md', cap: '改口径', st: 'wait' },
    ]},
    { label: `结构变更 · ${n('structural')}`, tier: 'structural', items: [
      { p: 'objects/备件.md', cap: '+3 −0', st: 'draft' },
      { p: 'objects/库存批次.md', cap: '+5 −1', st: 'draft' },
      { p: 'operations/生成采购建议.md', cap: '+8', st: 'draft' },
      { p: `…还有 ${n('structural') - 3} 项`, muted: true },
    ]},
    { label: `索引同步 · ${n('index')}（已设为自动）`, tier: 'index', items: [
      { p: 'objects/index.md', st: 'draft', dim: true },
      { p: 'operations/index.md', st: 'draft', dim: true },
      { p: 'rules/index.md', st: 'draft', dim: true },
    ]},
  ];
  const path = useStore(store, selPath);
  return (
    <div className="wb-scroll">
      <div className="wb-tree">
        {groups.map((g) => (
          <div key={g.label}>
            <div className="grp">{g.label}</div>
            {g.items.map((it) => it.muted ? (
              <div key={it.p} className="wb-row i1 faint t-sm" style={{ pointerEvents: 'none' }}>{it.p}</div>
            ) : (
              <button key={it.p} className={`wb-row i1${path === it.p ? ' on' : ''}${it.dim ? ' dim' : ''}`}
                onClick={() => actions.selectPath(it.p)}>
                <span className={`dot dot-${it.st}`} />
                <span className="nm">{displayName(it.p)}</span>
                {it.cap && <span className="cap">{it.cap}</span>}
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------------- 资料列表 ---------------- */
function MaterialList() {
  const cur = useStore(store, selMat);
  const demo = useStore(store, selDemo);
  const realMaterials = useStore(store, selMaterials);
  const materials = demo ? F.materials : realMaterials.map((item) => ({
    id: item.id,
    name: item.filename,
    v: item.revisionNo,
    refs: item.refs || 0,
    st: item.parseStatus === 'parsed' ? 'ok' : item.parseStatus === 'failed' ? 'stale' : 'new',
    meta: item.parseError || `${item.chars || 0} 字符${item.tableCount ? ` · ${item.tableCount} 张表` : ''}`,
  }));
  return (
    <>
      <div className="wb-scroll">
        <div style={{ padding: 'var(--s-2) 0' }}>
          {materials.map((m) => {
            const ext = (m.name || '').split('.').pop();
            const iconName = ['xlsx', 'xls', 'csv'].includes(ext) ? 'sheet' : 'file';
            return (
              <button key={m.id} className={`wb-mat${cur === m.id ? ' on' : ''}`}
                onClick={() => actions.selectMaterial(m.id)} title={m.name} aria-pressed={cur === m.id}>
                <Icon name={iconName} size={17} className="mat-icon" />
                <span className="mat-body">
                  <span className="mn">{m.name}</span>
                  <span className="mc"><span className={`dot dot-${m.st}`} />{m.st === 'stale' ? '无法解析' : `v${m.v} · 引用 ${m.refs}`}</span>
                </span>
              </button>
            );
          })}
          {demo && <div style={{ padding: 'var(--s-4)', borderTop: '1px solid var(--c-line-soft)', marginTop: 'var(--s-2)' }}>
            <div className="t-sm"><b>口述证据 {F.spokenEvidence} 条</b></div>
            <div className="t-xs faint" style={{ marginTop: 2 }}>
              已作为业务依据保留，并与文档依据一同参与建模
            </div>
          </div>}
        </div>
      </div>
      <div className="wb-legend">
        <i><Icon name="alert" size={12} />「引用 N」＝有多少知识条目以它为证据，删除前会提示</i>
      </div>
    </>
  );
}
