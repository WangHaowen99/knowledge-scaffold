import { useEffect, useState } from 'react';
import Icon from '../ui/Icon.jsx';
import { actions, store } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { getRevisionDiff, listRevisions, restoreRevision } from '../lib/api.js';
import VersionPanel from '../../VersionPanel.jsx';
import { normalizeVersion, versionTitle } from '../../revision-state.js';
import { userFacingText } from '../lib/userFacingText.js';

const TRACE = [
  { id: 1, createdAt: '2026-01-01T14:01:07', at: '14:01:07', actor: '助手', summary: '正在核对当前进展', phase: 'completed' },
  { id: 2, createdAt: '2026-01-01T14:01:08', at: '14:01:08', actor: '助手', summary: '已找到 6 份项目资料', phase: 'completed' },
  { id: 3, createdAt: '2026-01-01T14:01:09', at: '14:01:09', actor: '助手', summary: '3 份资料正在并行分析', phase: 'started' },
  { id: 4, createdAt: '2026-01-01T14:01:22', at: '14:01:22', actor: '资料分析', summary: '找到 14 段与安全库存相关的内容', phase: 'completed' },
  { id: 5, createdAt: '2026-01-01T14:01:31', at: '14:01:31', actor: '资料分析', summary: '已读到《作业手册》的相关章节', phase: 'completed' },
  { id: 6, createdAt: '2026-01-01T14:01:44', at: '14:01:44', actor: '资料分析', summary: '已核对库存明细的字段与样例', phase: 'completed' },
  { id: 7, createdAt: '2026-01-01T14:02:35', at: '14:02:35', actor: '助手', summary: '3 份资料分析完成，正在汇总', phase: 'completed' },
  { id: 8, createdAt: '2026-01-01T14:02:40', at: '14:02:40', actor: '助手', summary: '已形成采购申请流程草案', phase: 'completed' },
  { id: 9, createdAt: '2026-01-01T14:02:41', at: '14:02:41', actor: '助手', summary: '找到 3 处相关知识引用', phase: 'completed' },
  { id: 10, createdAt: '2026-01-01T14:03:02', at: '14:03:02', actor: '助手', summary: '需要确认 D2 的可用量口径', phase: 'waiting' },
];

/* 可审计 ≠ 常驻界面。工具轨迹、子 agent、检查点全部收进抽屉。 */
export default function ProcessDrawer({ onClose }) {
  const demo = useStore(store, (state) => state.demo);
  const trace = useStore(store, (state) => state.trace);
  const run = useStore(store, (state) => state.run);
  const projectId = useStore(store, (state) => state.projectId);
  const [tab, setTab] = useState('trace');
  const [versions, setVersions] = useState({ currentRevision: '', versions: [] });
  const [selectedRevision, setSelectedRevision] = useState('');
  const [diff, setDiff] = useState(undefined);
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [loadingDiff, setLoadingDiff] = useState(false);
  const [versionError, setVersionError] = useState('');
  const canCancel = !demo && ['queued', 'running', 'suspended'].includes(run?.status);
  const traceRows = newestTraceFirst(demo ? TRACE : trace);
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const loadVersions = async () => {
    if (!projectId || demo) return;
    setLoadingVersions(true); setVersionError('');
    try { setVersions(await listRevisions(projectId)); }
    catch (error) { setVersionError(error instanceof Error ? error.message : '版本读取失败'); }
    finally { setLoadingVersions(false); }
  };

  useEffect(() => {
    if (tab === 'versions') void loadVersions();
  }, [tab, projectId]);

  const openRevision = async (id, item) => {
    setSelectedRevision(id);
    if (!item.parentRevision) {
      setDiff({ fromRevision: id, toRevision: id, files: [] });
      return;
    }
    setLoadingDiff(true); setVersionError('');
    try { setDiff(await getRevisionDiff(projectId, item.parentRevision, id)); }
    catch (error) { setVersionError(error instanceof Error ? error.message : '修改读取失败'); }
    finally { setLoadingDiff(false); }
  };

  const restore = async (payload) => {
    setVersionError('');
    const target = normalizeVersion(versions.versions.find((item) => normalizeVersion(item).id === payload.targetRevision));
    const restored = await restoreRevision(projectId, payload);
    setSelectedRevision(''); setDiff(undefined);
    await actions.refreshSnapshot();
    store.set({
      statusText: `${restoredVersionMessage(restored.packageVersion, target)} 助手保持空闲。`,
    });
    onClose();
  };

  return (
    <div style={S.mask} onClick={onClose}>
      <section style={S.panel} onClick={(e) => e.stopPropagation()} role="dialog" aria-label="过程">
        <div className="wb-head" style={{ height: 40 }}>
          <div className="seg" aria-label="过程内容">
            <button className={tab === 'trace' ? 'on' : ''} onClick={() => setTab('trace')}>活动记录</button>
            {!demo && <button className={tab === 'versions' ? 'on' : ''} onClick={() => setTab('versions')}>版本历史</button>}
          </div>
          {tab === 'trace' && <span className="t-xs">{demo ? '67 项处理 · 4 个并行任务 · 12 个保存点' : `${trace.length} 条处理记录`}</span>}
          <span className="spacer" />
          {tab === 'trace' && canCancel && (
            <button className="btn btn-danger btn-sm" onClick={() => void actions.cancel()}>
              <Icon name="x" size={12} />停止运行
            </button>
          )}
          <button className="btn btn-ghost" onClick={onClose} aria-label="关闭"><Icon name="x" size={13} /></button>
        </div>
        <div className="wb-scroll" style={{ background: 'var(--c-surface)' }}>
          {tab === 'trace' ? <>
            <div className="wb-trace-list" role="list" aria-label="活动记录，最新记录在前">
              {traceRows.map((item, index) => {
                const status = phasePresentation(item.phase);
                return (
                  <article className={`wb-trace-item is-${status.tone}`} role="listitem"
                    key={item.id ?? `${item.createdAt || item.at}-${index}`}>
                    <div className="wb-trace-rail" aria-hidden="true"><i /></div>
                    <div className="wb-trace-content">
                      <div className="wb-trace-meta">
                        <time dateTime={item.createdAt || undefined}>{item.at || formatTraceTime(item.createdAt)}</time>
                        <span className="wb-trace-actor">{item.actor || '助手'}</span>
                        <span className={`wb-trace-status is-${status.tone}`}>{status.label}</span>
                      </div>
                      <div className="wb-trace-summary">{traceSummary(item)}</div>
                    </div>
                  </article>
                );
              })}
              {!traceRows.length && <div className="wb-trace-empty">暂无处理记录</div>}
            </div>
            <div className="wb-trace-note">
              服务重启后会从最近保存点继续，不重复已经完成的步骤。
              思考过程已过滤内部配置、密钥、工具参数和完整资料正文。
            </div>
          </> : (
            <div style={{ padding: 'var(--s-5)' }}>
              <VersionPanel
                versions={versions.versions}
                currentVersionId={versions.currentRevision}
                selectedVersionId={selectedRevision || undefined}
                diff={diff}
                loading={loadingVersions}
                diffLoading={loadingDiff}
                error={versionError}
                onSelectVersion={openRevision}
                onRestore={restore}
                onOpenFile={(path) => { actions.selectPath(path); onClose(); }}
              />
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

const S = {
  mask: { position: 'fixed', inset: 0, background: 'rgba(24,24,22,.28)', zIndex: 40,
    display: 'flex', justifyContent: 'flex-end', animation: 'wb-fade 160ms both' },
  panel: { width: 'min(760px, 92vw)', height: '100%', background: 'var(--c-bg)',
    borderLeft: '1px solid var(--c-line)', boxShadow: 'var(--sh-3)', display: 'flex', flexDirection: 'column' },
};

function phasePresentation(phase) {
  return ({
    started: { label: '进行中', tone: 'active' },
    repairing: { label: '自动修正中', tone: 'active' },
    completed: { label: '已完成', tone: 'done' },
    failed: { label: '失败', tone: 'failed' },
    blocked: { label: '需要处理', tone: 'failed' },
    rejected: { label: '未执行', tone: 'muted' },
    waiting: { label: '等待决定', tone: 'waiting' },
  })[phase] || { label: '已记录', tone: 'muted' };
}

export function newestTraceFirst(items) {
  return (Array.isArray(items) ? items : [])
    .map((item, index) => ({ item, index }))
    .sort((left, right) => {
      const leftTime = traceTimestamp(left.item);
      const rightTime = traceTimestamp(right.item);
      if (leftTime !== rightTime) return rightTime - leftTime;
      const leftSeq = Number(left.item?.seq ?? left.item?.id);
      const rightSeq = Number(right.item?.seq ?? right.item?.id);
      if (Number.isFinite(leftSeq) && Number.isFinite(rightSeq) && leftSeq !== rightSeq) return rightSeq - leftSeq;
      return right.index - left.index;
    })
    .map(({ item }) => item);
}

function traceTimestamp(item) {
  const createdAt = Date.parse(String(item?.createdAt || ''));
  if (Number.isFinite(createdAt)) return createdAt;
  const parts = String(item?.at || '').match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/u);
  if (!parts) return Number.NEGATIVE_INFINITY;
  return Number(parts[1]) * 3_600 + Number(parts[2]) * 60 + Number(parts[3] || 0);
}

function formatTraceTime(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return '';
  return [date.getHours(), date.getMinutes(), date.getSeconds()]
    .map((part) => String(part).padStart(2, '0'))
    .join(':');
}

export function restoredVersionMessage(packageVersion, target) {
  const label = packageVersion
    ? `v${String(packageVersion).replace(/^v/i, '')}`
    : versionTitle(target);
  return `已从版本历史恢复到${label}，无需重复评测。`;
}

export function humanizeTraceSummary(summary, toolName = '') {
  const completion = toolName === 'task_tool'
    ? '一项子任务已完成，主任务继续处理中'
    : '当前步骤已完成';
  let text = String(summary || '已记录一次处理')
    .replace(/^正在读取项目状态$/u, '正在核对当前进展')
    .replace(/^正在保存项目上下文$/u, '正在保存当前进展')
    .replace(/^正在核对项目状态转换$/u, '正在核对下一阶段')
    .replace(/^正在检查门禁$/u, '正在核对进入下一阶段的条件')
    .replace(/^指定评测轮次不存在$/u, '没有找到对应的评测记录')
    .replace(/^处理完成$/u, completion)
    .replace(/^当前项目状态：(.+?)\s*\/\s*[a-z_]+$/iu, '当前阶段：$1')
    .replace(/项目状态已变化，正在重新对齐/gu, '当前进展已变化，正在重新核对')
    .replace(/项目状态已更新为/gu, '当前阶段已更新为');
  const gates = {
    ready_for_eval: '评测准备',
    evaluation_passed: '正式评测',
    releasable: '发布条件',
    materials_ready: '资料准备',
    goal_confirmed: '建模目标确认',
    process_confirmed: '业务流程确认',
    knowledge_confirmed: '知识内容确认',
    deterministic_confirmed: '确定性计算范围确认',
    modeling_complete: '建模完成',
  };
  for (const [internal, label] of Object.entries(gates)) text = text.replaceAll(internal, label);
  return userFacingText(text.replace(/\s*\/\s*(?:running|waiting|suspended|completed|failed)\b/giu, ''));
}

export function traceSummary(item = {}) {
  const summary = humanizeTraceSummary(item.summary, item.name);
  const count = Number(item.segmentCount) || 0;
  if (item.name === 'get_evaluation_results' && count > 1) {
    return `${summary} · 共 ${count} 次读取，已合并`;
  }
  return count > 1 ? `${summary} · ${count} 个连续分段` : summary;
}
