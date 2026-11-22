import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button } from '../../ui/index.jsx';
import { ReportModal, SuggestionsModal } from '../evaluation/ReleaseEvaluation.jsx';
import FlywheelConfirm from './FlywheelConfirm.jsx';
import FlywheelDialog from './FlywheelDialog.jsx';
import { RunDetail, SettingsSheet } from './FlywheelSheets.jsx';
import {
  CHANGES_WORKSPACE, NOTICE_ICONS, confirmPlan, delegationNotice, isFeedback, optimizeBlock, reauthorizeNotice, toggleNotice,
  transitions,
} from './flywheelModel.js';
import { useFlywheel } from './useFlywheel.js';
import '../../styles/flywheel.css';

const notify = (type, text) => toast(text, NOTICE_ICONS[type]);

// 发布页的数据飞轮：在 PublishPage 顶层调用，页面打开期间持续同步（自动执行依赖它）。
// entry 是「已发布版本」卡片头的入口按钮（角标为待处理数量），dialog 是弹窗与各层浮层
export function useFlywheelPanel(projectId, onWorkspaceChanged) {
  const { api, services } = useApp();
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

  // 两次同步之间的变化给出提示；执行中的事项结束时刷新发布页
  useEffect(() => {
    if (!state?.available) return;
    const before = previous.current;
    previous.current = state;
    transitions(before, state).forEach(([type, text]) => notify(type, text));
    if (before && before.active_id !== state.active_id) onWorkspaceChanged?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);

  const runOf = (id) => state?.runs?.find((run) => run.id === id);
  // 确认评测、修改设置要转交 refresh token：评测服务用它登记后台评测登录
  const action = (run, name, body) => (name === 'evaluate'
    ? api.flywheelEvaluate(projectId, run.id, body, services.tokens.refresh)
    : api.flywheelAction(projectId, run.id, name, body));
  const saveSettings = (changes) => api.flywheelSettings(projectId, changes, services.tokens.refresh);

  const act = async (call, notice) => {
    setBusy(true);
    try {
      const view = await call();
      apply(view);
      if (notice) notify(...notice);
      return view;
    } catch (e) {
      notify('error', errorText(e));
      refresh();
      return null;
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (key) => {
    const enabled = !state.settings[key];
    const wasActive = !!state.active_id;
    const view = await act(() => saveSettings({ [key]: enabled }));
    if (view) notify('info', toggleNotice(view, key, enabled, wasActive));
  };

  // 设置页的选择即时生效
  const changeSettings = async (patch) => {
    const view = await act(() => saveSettings(patch));
    if (!view) return;
    if (patch.delegation_days !== undefined) notify('info', delegationNotice(view, patch.delegation_days));
    else notify('success', `连续自动处理上限已改为${patch.max_rounds ? ` ${patch.max_rounds} 轮` : '无限次'}。`);
  };

  // 不改任何设置，只转交当前的 refresh token：评测服务重新登记并保存后台评测登录
  const reauthorize = async () => {
    const view = await act(() => saveSettings({}));
    if (view) notify(view.delegation?.login === 'active' ? 'success' : 'error', reauthorizeNotice(view));
  };

  const accept = async () => {
    const current = confirm;
    const { name, body, notice } = confirmPlan(current);
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
      notify('success', '检查通过；请确认是否发布本次候选内容。');
      setSheet(null);
    } else if (result) {
      notify('info', '仍有检查未通过，请查看检查明细。');
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
      rollback: () => (state.active_id ? notify('info', '请等待当前运行结束后再回退。')
        : state.assistant_busy ? notify('info', '建模助手正在对话中修改内容，请等本轮对话结束后再回退。')
          : setConfirm({ kind: 'rollback', run: target, scope: 'workspace' })),
      recheck: () => recheck(target),
      detail: () => setSheet({ kind: 'run', id: target.id, tab: 'timeline', run: target }),
      changes: () => setSheet({ kind: 'run', id: target.id, tab: 'changes', run: target }),
      'close-detail': () => setSheet(null),
      settings: () => setSheet({ kind: 'settings' }),
      suggestions: () => showArtifact('suggestions', target),
      report: () => showArtifact('report', target),
      'filter-pending': () => { setTab('runs'); setFilter('waiting'); showRecords(); },
      // 修订过程在工作台的建模助手对话里：离开发布页去看
      chat: () => {
        setSheet(null);
        setOpen(false);
        notify('info', '建模助手正在工作台对话中修订知识内容。');
        navigate(pagePath('wb-l2', projectId), target?.conversation_id ? { c: target.conversation_id } : undefined);
      },
    };
    handlers[kind]?.();
  };

  // 每层浮层都响应 Esc：上面还有浮层时下面的不关
  const closeDialog = () => { if (!sheet && !confirm && !artifact) setOpen(false); };
  const closeSheet = () => { if (!confirm && !artifact) setSheet(null); };

  const sheetRun = sheet?.kind === 'run' ? runOf(sheet.id) || sheet.run : null;
  const artifactRun = artifact ? runOf(artifact.runId) || artifact.run : null;
  const pending = state?.pending || 0;
  const entry = (
    <Button size="sm" icon="recycle" onClick={() => setOpen(true)}
      after={pending > 0 ? <span className="fw-badge" aria-label={`${pending} 项待处理`}>{pending}</span> : null}>
      数据飞轮
    </Button>
  );
  const evaluation = { pid: projectId, refresh };
  const dialog = (
    <>
      <FlywheelDialog open={open} state={state} error={error} onClose={closeDialog} onAction={onAction}
        records={{ ref: recordsRef, tab, setTab, filter, setFilter }} />
      {sheetRun ? (
        <RunDetail projectId={projectId} run={sheetRun} state={state} tab={sheet.tab}
          onTab={(next) => setSheet({ ...sheet, tab: next })} onAction={onAction} onClose={closeSheet} />
      ) : null}
      {sheet?.kind === 'settings' && state ? <SettingsSheet state={state} onChange={changeSettings} onClose={closeSheet} /> : null}
      <FlywheelConfirm confirm={confirm} state={state} busy={busy} onAccept={accept} onCancel={() => setConfirm(null)}
        onChange={(patch) => setConfirm({ ...confirm, ...patch })} />
      {artifact?.kind === 'report' ? <ReportModal ev={evaluation} target={artifact.target} onClose={() => setArtifact(null)} /> : null}
      {artifact?.kind === 'suggestions' ? (
        <SuggestionsModal ev={evaluation} target={artifact.target} onClose={() => setArtifact(null)}
          blockReason={optimizeBlock(state, artifactRun)}
          onOptimize={async () => {
            const view = await action(artifactRun, 'optimize');
            apply(view);
            onWorkspaceChanged?.();
            return view;
          }}
          onOptimized={() => { setArtifact(null); setSheet(null); }} />
      ) : null}
    </>
  );
  return { entry, dialog };
}
