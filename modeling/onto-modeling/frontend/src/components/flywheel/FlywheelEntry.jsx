import { Button as AntButton, Drawer, message } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { api } from '../../lib/api.js';
import { delegationNotice, isFeedback, optimizeBlock, reauthorizeNotice, toggleNotice, transitions } from '../../lib/flywheel.js';
import { EvaluationReportModal, EvaluationSuggestionsModal } from '../PackageEvaluationModals.jsx';
import FlywheelConfirm from './FlywheelConfirm.jsx';
import FlywheelDialog, { MODAL_WIDTH } from './FlywheelDialog.jsx';
import { RunDetail, SettingsSheet } from './FlywheelSheets.jsx';
import { useFlywheel } from './useFlywheel.js';
import './flywheel.css';

const toast = (type, text) => ({ success: message.success, info: message.info, error: message.error })[type](text);

const CONFIRMED = {
  optimize: ['optimize', ['success', '已开始评测优化，修订过程可在右侧对话中查看。']],
  publish: ['publish', ['info', '已开始发布到 Hub，完成后在此显示结果。']],
  retry: ['publish', ['info', '已重新提交发布请求。']],
  stop: ['stop', ['info', '本次优化已中止，执行前内容已恢复。']],
  ignore: ['ignore', ['info', '已忽略本次建议，记录已保留。']],
  evaluate: ['evaluate', ['success', '已发起新版本评测，评测结果到达后在此显示。']],
  'skip-evaluation': ['skip-evaluation', ['info', '已跳过本次评测，记录已保留。']],
};
// 右侧抽屉填满飞轮弹框右侧的空白，左边界贴近弹框右边界（留 12px）；
// 屏幕较窄、空白不足 580px 时仍取 580px，此时会盖住弹框右侧一部分
const SHEET_WIDTH = `max(580px, calc((100vw - ${MODAL_WIDTH}) / 2 - 12px))`;
// 抽屉打开时弹框不可操作：遮罩比默认浅，弹框内容仍看得清
const SHEET_MASK = { mask: { background: 'rgba(17, 24, 39, 0.18)' } };
// 这些操作会改写工作区：完成后刷新左栏产物树与迭代
const CHANGES_WORKSPACE = ['optimize', 'stop', 'rollback'];

// 左栏的数据飞轮：在 LeftPane 顶层调用，切换左栏页签时同步也不中断（自动执行依赖它）。
// entry 是「进度」页签里的入口按钮（角标为待处理数量），dialog 是弹窗与各层浮层
export function useFlywheelPanel(projectId, onWorkspaceChanged) {
  const [open, setOpen] = useState(false);
  const { state, error, apply, refresh } = useFlywheel(projectId, open);
  // { kind: 'run', id, tab, run } | { kind: 'settings' }；run 是点开时的记录，
  // 滚动加载出的较早记录不在轮询返回的最新 50 条里，详情靠它显示
  const [sheet, setSheet] = useState(null);
  const [confirm, setConfirm] = useState(null); // { kind, run, scope, replaceGraph }
  const [artifact, setArtifact] = useState(null); // { kind, runId, run, target }
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState('runs');
  const [filter, setFilter] = useState('all');
  const recordsRef = useRef(null);
  const previous = useRef(null);

  useEffect(() => {
    setOpen(false); setSheet(null); setConfirm(null); setArtifact(null); setTab('runs'); setFilter('all');
    previous.current = null;
  }, [projectId]);

  // 两次同步之间的变化给出提示；执行中的事项结束时刷新工作区
  useEffect(() => {
    if (!state?.available) return;
    const before = previous.current;
    previous.current = state;
    transitions(before, state).forEach(([type, text]) => toast(type, text));
    if (before && before.active_id !== state.active_id) onWorkspaceChanged?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);

  const runOf = (id) => state?.runs?.find((run) => run.id === id);
  // 确认评测要转交 refresh token，走单独的接口
  const action = (run, name, body) => (name === 'evaluate' ? api.flywheelEvaluate(projectId, run.id, body)
    : api.flywheelAction(projectId, run.id, name, body));

  const act = async (call, notice) => {
    setBusy(true);
    try {
      const view = await call();
      apply(view);
      if (notice) toast(...notice);
      return view;
    } catch (e) {
      toast('error', e.message);
      refresh();
      return null;
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (key) => {
    const enabled = !state.settings[key];
    const wasActive = !!state.active_id;
    const view = await act(() => api.flywheelSettings(projectId, { [key]: enabled }));
    if (view) toast('info', toggleNotice(view, key, enabled, wasActive));
  };

  // 设置页的选择即时生效
  const changeSettings = async (patch) => {
    const view = await act(() => api.flywheelSettings(projectId, patch));
    if (!view) return;
    if (patch.delegation_days !== undefined) toast('info', delegationNotice(view, patch.delegation_days));
    else toast('success', `连续自动处理上限已改为${patch.max_rounds ? ` ${patch.max_rounds} 轮` : '无限次'}。`);
  };

  // 不改任何设置，只转交当前的 refresh token：评测服务重新登记并保存后台评测登录
  const reauthorize = async () => {
    const view = await act(() => api.flywheelSettings(projectId, {}));
    if (view) toast(view.delegation?.login === 'active' ? 'success' : 'error', reauthorizeNotice(view));
  };

  const accept = async () => {
    const current = confirm;
    const waiting = current.kind === 'skip-evaluation' && current.run.status === 'evaluating';
    const [name, notice] = current.kind === 'rollback'
      ? ['rollback', current.scope === 'hub' ? ['info', '建模内容已恢复，正在检查并发布新的恢复版本。']
        : ['success', '建模内容已恢复，已新增回退记录；两个自动化开关已暂停。']]
      : waiting ? ['skip-evaluation', ['info', '已不再等待本次评测，评测服务中的评测照常进行。']]
        : CONFIRMED[current.kind];
    const graph = { replace_graph: !!current.replaceGraph, graph_confirmation_code: current.replaceGraph?.code || '' };
    const body = { rollback: { scope: current.scope }, evaluate: graph }[current.kind] || {};
    const view = await act(() => action(current.run, name, body), notice);
    // 评测发起失败时留在确认框：图数据需要确认删除时，刷新后的运行会在框里给出选项
    if (!view && current.kind === 'evaluate') return;
    setConfirm(null);
    if (view) setSheet(null);
    if (view && CHANGES_WORKSPACE.includes(name)) onWorkspaceChanged?.();
  };

  const recheck = async (run) => {
    const view = await act(() => action(run, 'recheck'));
    const result = view?.runs.find((item) => item.id === run.id);
    if (result?.status === 'waiting_publish') {
      toast('success', '检查通过；请确认是否发布本次候选内容。');
      setSheet(null);
    } else if (result) {
      toast('info', '仍有检查未通过，请查看检查明细。');
    }
  };

  const showRecords = () => setTimeout(() => recordsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0);
  const showArtifact = (kind, run) => {
    if (!run.eval_run_id) return;
    setArtifact({ kind, runId: run.id, run, target: { runId: run.eval_run_id, version: run.version, releaseId: run.release_id,
      feedback: isFeedback(run) } });
  };

  const onAction = (kind, target) => {
    const handlers = {
      toggle: () => toggle(target),
      'confirm-optimize': () => setConfirm({ kind: 'optimize', run: target }),
      'confirm-publish': () => setConfirm({ kind: 'publish', run: target }),
      'retry-publish': () => setConfirm({ kind: 'retry', run: target }),
      'confirm-evaluate': () => setConfirm({ kind: 'evaluate', run: target }),
      'skip-evaluation': () => setConfirm({ kind: 'skip-evaluation', run: target }),
      reauthorize: () => reauthorize(),
      stop: () => setConfirm({ kind: 'stop', run: target }),
      ignore: () => setConfirm({ kind: 'ignore', run: target }),
      rollback: () => (state.active_id ? toast('info', '请等待当前运行结束后再回退。')
        : state.assistant_busy ? toast('info', '建模助手正在对话中修改内容，请等本轮对话结束后再回退。')
          : setConfirm({ kind: 'rollback', run: target, scope: 'workspace' })),
      recheck: () => recheck(target),
      detail: () => setSheet({ kind: 'run', id: target.id, tab: 'timeline', run: target }),
      changes: () => setSheet({ kind: 'run', id: target.id, tab: 'changes', run: target }),
      'close-detail': () => setSheet(null),
      settings: () => setSheet({ kind: 'settings' }),
      suggestions: () => showArtifact('suggestions', target),
      report: () => showArtifact('report', target),
      'filter-pending': () => { setTab('runs'); setFilter('waiting'); showRecords(); },
      chat: () => { setSheet(null); setOpen(false); toast('info', '建模助手正在右侧对话中修订知识内容。'); },
    };
    handlers[kind]?.();
  };

  const sheetRun = sheet?.kind === 'run' ? runOf(sheet.id) || sheet.run : null;
  const artifactRun = artifact ? runOf(artifact.runId) || artifact.run : null;
  const entry = (
    <AntButton block className="fw-entry" style={{ marginBottom: 8 }} onClick={() => setOpen(true)}>
      <span className="fw-entry-label">知识包数据飞轮</span>
      {state?.pending > 0 && <span className="fw-entry-badge" aria-label={`${state.pending} 项待处理`}>{state.pending}</span>}
    </AntButton>
  );
  const dialog = (
    <>
      <FlywheelDialog open={open} state={state} error={error} onClose={() => setOpen(false)} onAction={onAction}
        records={{ ref: recordsRef, tab, setTab, filter, setFilter }} />
      <Drawer open={!!sheet && (sheet.kind !== 'run' || !!sheetRun)} onClose={() => setSheet(null)} placement="right"
        width={SHEET_WIDTH} styles={SHEET_MASK} closable={false} title={null} footer={null} zIndex={1010}
        destroyOnHidden
        rootClassName="fw-scope fw-drawer-root">
        {sheetRun && (
          <RunDetail projectId={projectId} run={sheetRun} state={state} tab={sheet.tab}
            onTab={(next) => setSheet({ ...sheet, tab: next })} onAction={onAction} />
        )}
        {sheet?.kind === 'settings' && state && <SettingsSheet state={state} onChange={changeSettings} onClose={() => setSheet(null)} />}
      </Drawer>
      <FlywheelConfirm confirm={confirm} state={state} busy={busy} onAccept={accept} onCancel={() => setConfirm(null)}
        onChange={(patch) => setConfirm({ ...confirm, ...patch })} />
      <EvaluationReportModal projectId={projectId} zIndex={1020}
        target={artifact?.kind === 'report' ? artifact.target : null} onClose={() => setArtifact(null)} />
      <EvaluationSuggestionsModal projectId={projectId} zIndex={1020}
        target={artifact?.kind === 'suggestions' ? artifact.target : null}
        blockReason={artifactRun ? optimizeBlock(state, artifactRun) : ''}
        onClose={() => setArtifact(null)}
        onRegenerated={() => { setArtifact(null); refresh(); }}
        onOptimize={async () => {
          const view = await action(artifactRun, 'optimize');
          apply(view);
          onWorkspaceChanged?.();
          return view;
        }}
        onOptimized={() => { setArtifact(null); setSheet(null); }} />
    </>
  );
  return { entry, dialog };
}
