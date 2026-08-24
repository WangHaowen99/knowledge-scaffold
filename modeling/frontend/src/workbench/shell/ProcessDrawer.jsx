import { useEffect, useState } from 'react';
import Icon from '../ui/Icon.jsx';
import { actions, store } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { getRevisionDiff, listRevisions, restoreRevision } from '../lib/api.js';
import VersionPanel from '../../VersionPanel.jsx';
import { normalizeVersion, versionTitle } from '../../revision-state.js';
import { userFacingText } from '../lib/userFacingText.js';

const TRACE = [
  ['14:01:07', '助手', '正在核对当前进展', '完成'],
  ['14:01:08', '助手', '已找到 6 份项目资料', '完成'],
  ['14:01:09', '助手', '3 份资料正在并行分析', '进行中'],
  ['14:01:22', '资料分析', '找到 14 段与安全库存相关的内容', '完成'],
  ['14:01:31', '资料分析', '已读到《作业手册》的相关章节', '完成'],
  ['14:01:44', '资料分析', '已核对库存明细的字段与样例', '完成'],
  ['14:02:35', '助手', '3 份资料分析完成，正在汇总', '完成'],
  ['14:02:40', '助手', '已形成采购申请流程草案', '完成'],
  ['14:02:41', '助手', '找到 3 处相关知识引用', '完成'],
  ['14:03:02', '助手', '需要确认 D2 的可用量口径', '等待决定'],
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
    onClose();
    await actions.send(restoredVersionMessage(restored.packageVersion, target));
  };

  return (
    <div style={S.mask} onClick={onClose}>
      <section style={S.panel} onClick={(e) => e.stopPropagation()} role="dialog" aria-label="过程">
        <div className="wb-head" style={{ height: 40 }}>
          <div className="seg" aria-label="过程内容">
            <button className={tab === 'trace' ? 'on' : ''} onClick={() => setTab('trace')}>运行轨迹</button>
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
            <table className="grid" style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-sm)' }}>
              <tbody>
                {(demo ? TRACE : trace.map((item) => [item.at, '助手', humanizeTraceSummary(item.summary), phaseLabel(item.phase)])).map((r, i) => (
                  <tr key={i} style={r[3].startsWith('等待') ? { background: 'var(--c-wait-bg)' } : undefined}>
                    <td className="faint" style={{ width: 66 }}>{r[0]}</td>
                    <td style={{ width: 108, color: 'var(--c-agent)' }}>{r[1]}</td>
                    <td className="ellip">{r[2]}</td>
                    <td className="muted" style={{ width: 96, textAlign: 'right' }}>{r[3]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="t-sm muted" style={{ padding: 'var(--s-5)' }}>
              服务重启后会从最近保存点继续，不重复已经完成的步骤。
              处理记录不展示模型内部推理、密钥和完整资料正文。
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

function phaseLabel(phase) {
  return ({ started: '进行中', completed: '完成', failed: '失败', rejected: '未执行' })[phase] || '已记录';
}

export function restoredVersionMessage(packageVersion, target) {
  const label = packageVersion
    ? `v${String(packageVersion).replace(/^v/i, '')}`
    : versionTitle(target);
  return `我已从版本历史恢复到${label}。请以恢复后的发布结果为准继续，无需重复评测。`;
}

export function humanizeTraceSummary(summary) {
  let text = String(summary || '已记录一次处理')
    .replace(/^正在读取项目状态$/u, '正在核对当前进展')
    .replace(/^正在保存项目上下文$/u, '正在保存当前进展')
    .replace(/^正在核对项目状态转换$/u, '正在核对下一阶段')
    .replace(/^正在检查门禁$/u, '正在核对进入下一阶段的条件')
    .replace(/^指定评测轮次不存在$/u, '没有找到对应的评测记录')
    .replace(/^处理完成$/u, '本次处理已完成')
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
    modeling_complete: '建模完成',
  };
  for (const [internal, label] of Object.entries(gates)) text = text.replaceAll(internal, label);
  return userFacingText(text.replace(/\s*\/\s*(?:running|waiting|suspended|completed|failed)\b/giu, ''));
}
