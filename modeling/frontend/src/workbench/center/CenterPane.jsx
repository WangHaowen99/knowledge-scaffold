import { lazy, Suspense } from 'react';
import { store, actions } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import FlowView from './FlowView.jsx';
import DiffView from './DiffView.jsx';
import ImpactView from './ImpactView.jsx';
import ConflictView from './ConflictView.jsx';
import GateView from './GateView.jsx';
import EvaluationView from './EvaluationView.jsx';
import DocView from './DocView.jsx';
import Icon from '../ui/Icon.jsx';

/* Markdown 渲染要拖 react-markdown + remark-gfm（约 120KB），
   而首屏不一定看文件，所以单独切块、按需加载。 */
const FileView = lazy(() => import('./FileView.jsx'));
const StudioView = lazy(() => import('../studio/StudioView.jsx'));

const selFocus = (s) => s.focus;
const selFollow = (s) => s.follow;
const selPending = (s) => s.pendingFocus;
const selDemo = (s) => s.demo;
const selLoading = (s) => s.loading;
const selMaterials = (s) => s.materials;

const NODE_LABEL = { D2: '安全库存判断', B: '查询可用库存', E: '生成采购建议', A: '校验备件编码' };

const TITLES = {
  flow: (f) => f.target.split('/').pop(),
  diff: (f) => f.target.split('/').pop() || '候选修改',
  doc: (f) => f.target,
  impact: () => '影响分析',
  conflict: (f) => f.target || '多来源融合',
  gates: () => '正式发布门禁',
  evaluation: () => '正式评测',
  file: (f) => f.target.split('/').pop(),
  studio: () => '知识包 Studio',
};
const SUBS = {
  flow: '渲染 / 源码 / 历史', diff: '1 / 2 需要你判断的', doc: 'v2 · 42 页 · 替换 / 历史 / 删除',
  impact: '证据链 / 清单', conflict: '5 项待裁决 · 23 项可自动', gates: '纯谓词 · 每次实时重算',
  file: '内容 / 历史',
};

export default function CenterPane() {
  const focus = useStore(store, selFocus);
  const follow = useStore(store, selFollow);
  const pending = useStore(store, selPending);
  const demo = useStore(store, selDemo);
  const loading = useStore(store, selLoading);
  const materials = useStore(store, selMaterials);
  const title = loading && focus.kind === 'empty'
    ? '正在加载项目'
    : (TITLES[focus.kind] || (() => focus.target))(focus);
  const sub = demo ? SUBS[focus.kind] : focus.kind === 'conflict' && focus.target === '多来源融合'
    ? '来源 / 去向账本 / 质量门禁'
    : {
    flow: '渲染 / 源码', diff: '待确认的修改前后对照', doc: '资料原文与引用',
    impact: '保持有效 / 需要处理', conflict: '范围 / 冲突处置', gates: '确定性实时结果',
    evaluation: '准备 / 真实用例 / 运行结果 / 失败诊断 / 优化建议',
    file: '渲染 / 源码',
    studio: '图 / 表 / 条目',
  }[focus.kind];
  const focusReference = focus.kind === 'studio'
    ? focus.elementId
    : focus.node;
  const pendingMaterial = pending?.kind === 'doc'
    ? materials.find((item) => item.id === pending.target || item.filename === pending.target)
    : null;
  const pendingReference = pending?.kind === 'studio'
    ? pending.elementId || '知识包 Studio'
    : pendingMaterial?.filename || pending?.node || pending?.target?.split('/').pop() || '新位置';

  return (
    <>
      <div className="wb-head">
        <b className="ellip">{title}</b>
        <span className="spacer" />
        {!demo && (
          <button className={`btn btn-sm${focus.kind === 'conflict' ? ' btn-p' : ''}`}
            onClick={() => store.set({
              focus: { kind: 'conflict', target: '多来源融合' },
              follow: false,
              pendingFocus: null,
            })}>
            <Icon name="layers" size={13} />融合
          </button>
        )}
        {!demo && (
          <button className={`btn btn-sm${focus.kind === 'studio' ? ' btn-p' : ''}`}
            onClick={() => store.set({
              focus: { kind: 'studio', target: '知识包 Studio', view: 'graph' },
              follow: false,
              pendingFocus: null,
            })}>
            <Icon name="layers" size={13} />Studio
          </button>
        )}
        <span className="t-xs">{sub}</span>
      </div>
      <div className={`wb-scroll${focus.kind === 'studio' ? ' wb-studio-scroll' : ''}`}>
        <div className={`wb-canvas fade-in${focus.kind === 'studio' ? ' wb-studio-canvas' : ''}`} key={focus.kind + focus.target}>
          {follow && focusReference && (
            <div className="wb-focus">
              <Icon name="arrow" size={13} />
              <span>
                {focus.kind === 'studio' ? <>
                  <b>助手正在定位</b> <code className="k">{focus.elementId}</code>
                </> : <>
                  <b>助手正在讨论</b> {focus.target.split('/').pop().replace(/\.mmd$/, '')} ·
                  节点 <code className="k">{focus.node} {NODE_LABEL[focus.node] || ''}</code>
                </>}
              </span>
              <span className="spacer" />
              <button className="btn btn-sm" onClick={() => actions.setFollow(false)}>取消跟随</button>
            </div>
          )}
          {!follow && pending && (
            <div className="wb-focus" style={{ background: 'var(--c-wait-bg)', borderColor: 'var(--c-wait-line)', color: 'var(--c-wait)' }}>
              <Icon name="alert" size={13} />
              <span>
                你正在自己浏览，助手已经移到 <code className="k">{pendingReference}</code>
              </span>
              <span className="spacer" />
              <button className="btn btn-sm" onClick={() => actions.setFollow(true)}>跟上</button>
            </div>
          )}
          {loading && focus.kind === 'empty'
            ? <PaneSkeleton />
            : <Body kind={focus.kind} focus={focus} demo={demo} />}
        </div>
      </div>
    </>
  );
}

function Body({ kind, focus, demo }) {
  switch (kind) {
    case 'flow': return <FlowView />;
    case 'diff': return <DiffView />;
    case 'impact': return <ImpactView data={focus.data} demo={demo} />;
    case 'conflict': return <ConflictView data={demo ? undefined : focus.data} />;
    case 'gates': return <GateView data={demo ? undefined : focus.data} />;
    case 'evaluation': return <EvaluationView />;
    case 'studio':
      return (
        <Suspense fallback={<PaneSkeleton />}>
          <StudioView focus={focus} />
        </Suspense>
      );
    case 'doc': return <DocView />;
    case 'file':
      return (
        <Suspense fallback={<PaneSkeleton />}>
          <FileView path={focus.target} />
        </Suspense>
      );
    default: return <div className="empty">选择左侧任一项查看</div>;
  }
}

export function PaneSkeleton() {
  return (
    <div className="skel-pane" aria-busy="true" aria-label="加载中">
      <div className="skel" /><div className="skel" /><div className="skel" /><div className="skel" />
    </div>
  );
}
