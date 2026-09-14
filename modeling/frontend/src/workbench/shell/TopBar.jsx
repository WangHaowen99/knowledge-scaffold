import { useState } from 'react';
import { Archive, GitBranch, Settings } from 'lucide-react';

import { store, actions, SCENES } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { project } from '../lib/mock/fixtures.js';
import Icon from '../ui/Icon.jsx';
import { downloadPackage } from '../lib/api.js';
import {
  downloadProjectTransfer,
  getProjectTransferJob,
  startProjectTransferExport,
} from '../../api.js';
import { responseDownloadFilename, waitForProjectTransferJob } from '../../projectTransfer.js';

const selScene = (s) => s.scene;
const selActivity = (s) => s.activity;
const selDemo = (s) => s.demo;
const selProject = (s) => s.project;
const selPackageVersion = (s) => s.packageVersion;
const selPackageStatus = (s) => s.packageStatus;
const selFiles = (s) => s.files;
const selInteraction = (s) => s.interaction;
const selRun = (s) => s.run;
const selWorkflow = (s) => s.workflow;
const selStudioPendingCount = (s) => s.studioPendingCount;

export default function TopBar({ onOpenMcpSettings, settingsLoading = false }) {
  const [transferProgress, setTransferProgress] = useState(null);
  const scene = useStore(store, selScene);
  const activity = useStore(store, selActivity);
  const demo = useStore(store, selDemo);
  const realProject = useStore(store, selProject);
  const packageVersion = useStore(store, selPackageVersion);
  const packageStatus = useStore(store, selPackageStatus);
  const files = useStore(store, selFiles);
  const interaction = useStore(store, selInteraction);
  const run = useStore(store, selRun);
  const workflow = useStore(store, selWorkflow);
  const confirmPolicy = useStore(store, (state) => state.confirmPolicy);
  const [policyOpen, setPolicyOpen] = useState(false);
  const [policyConfirming, setPolicyConfirming] = useState(false);
  const confirmationGroups = interaction?.kind === 'confirmation'
    ? (interaction.payload?.changeGroups || []).length
    : 0;
  const studioPendingCount = useStore(store, selStudioPendingCount);
  const counts = demo ? project.counts : {
    flows: files.filter((path) => path.startsWith('diagrams/') && path.endsWith('.mmd')).length,
    objects: files.filter((path) => path.startsWith('objects/') && path.endsWith('.md') && !path.endsWith('index.md')).length,
    scripts: files.filter((path) => path.startsWith('scripts/') && path.endsWith('.py')).length,
    pending: Math.max(interaction ? 1 : 0, Number(studioPendingCount) || 0),
    pendingGroups: confirmationGroups,
  };
  const incr = scene === 'increment';
  const running = !interaction && ['queued', 'running', 'stopping'].includes(run?.status);
  const showRunningStatus = demo ? Boolean(activity || running) : running;
  const canStartIncrement = packageStatus === 'released'
    && workflow?.phase !== 'increment'
    && !interaction
    && !running;
  const structuralPolicy = confirmPolicy.find((item) => item.tier === 'structural');
  const projectName = demo ? project.name : (realProject?.name || '正在加载项目');
  const openPending = () => {
    if (!counts.pending) return;
    if (demo) {
      actions.setScene('confirm');
      return;
    }
    if (interaction) {
      window.dispatchEvent(new CustomEvent('mw-pending-attention', {
        detail: { interactionId: interaction.id },
      }));
      return;
    }
    store.set({
      focus: { kind: 'studio', target: '知识包 Studio', view: 'table' },
      follow: false,
      pendingFocus: null,
    });
    window.dispatchEvent(new Event('mw-package-updated'));
  };
  const exportPackage = async () => {
    try {
      const response = await downloadPackage(store.get().projectId);
      const blob = await response.blob();
      const disposition = response.headers.get('content-disposition') || '';
      const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
      const plain = disposition.match(/filename="?([^";]+)"?/i)?.[1];
      const filename = encoded ? decodeURIComponent(encoded) : (plain || 'knowledge-package.zip');
      const href = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = href;
      link.download = filename;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(href), 1_000);
    } catch (error) {
      store.set({ error: error instanceof Error ? error.message : '知识包导出失败' });
    }
  };
  const exportProject = async () => {
    if (transferProgress !== null) return;
    setTransferProgress(0);
    try {
      const started = await startProjectTransferExport(store.get().projectId);
      const job = await waitForProjectTransferJob(getProjectTransferJob, started.job.id, {
        onProgress: (progress) => setTransferProgress(Number(progress?.percent) || 0),
      });
      const response = await downloadProjectTransfer(job.id);
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = href;
      link.download = responseDownloadFilename(response, 'project-transfer.zip');
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(href), 1_000);
    } catch (error) {
      store.set({ error: error instanceof Error ? error.message : '项目导出失败' });
    } finally {
      setTransferProgress(null);
    }
  };

  return (
    <header className="wb-top">
      {!demo && <a className="btn btn-ghost btn-sm" href="/" title="返回项目列表"><Icon name="arrow" size={12} />项目</a>}
      <span className="goal ellip" title={projectName}>{projectName}</span>
      <span className="sep">·</span>
      <span className="k">
        {(() => { if (demo) return ((() => { if (incr) return '1.2.0 · 已发布'; return project.revision; })()); return packageVersion
            ? `v${String(packageVersion).replace(/^v/i, '')} · ${packageStatusLabel(packageStatus)}`
            : '准备中'; })()}
      </span>

      {(() => { if (incr) return (
        <>
          <span className="pill pill-ok">11 项保持有效</span>
          <span className="pill pill-stale">4 项需处理</span>
        </>
      ); return (
        <button type="button" className="pill pill-wait wb-pending-pill"
          disabled={!counts.pending} onClick={openPending}
          title={(() => { if (interaction) return '查看建模助手中的待确认事项'; return counts.pending ? '查看 Studio 中待检查并应用的工作草稿' : '当前没有待处理内容'; })()}>
          {(() => { if (counts.pending) return interaction
              ? (() => { if (counts.pendingGroups) return `${counts.pending} 个决定 · ${counts.pendingGroups} 组变更`; return `${counts.pending} 个决定待处理`; })()
              : `${counts.pending} 项 Studio 修改待应用`; return '没有待处理决定'; })()}
        </button>
      ); })()}

      {!interaction && showRunningStatus && (
        <span className="pill pill-agent wb-priority-status"><i className="dot-pulse" />{run?.status === 'stopping' ? '正在停止' : '助手运行中'}</span>
      )}

      <span className="spacer" />

      <span className="pill pill-plain wb-counts-pill" title={`${counts.flows} 流程 · ${counts.objects} 对象 · ${counts.scripts} 脚本`}>
        <b>{counts.flows + counts.objects + counts.scripts}</b> 产物
      </span>

      {/* demo 场景切换：接后端后由真实状态驱动，这里保留为开发入口 */}
      {demo && <label className="t-sm faint" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Icon name="layers" size={13} />
        <select
          className="input"
          style={{ width: 'auto', padding: '3px 6px', fontSize: 'var(--t-sm)', borderRadius: 5 }}
          value={scene}
          onChange={(e) => actions.setScene(e.target.value)}
        >
          {SCENES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
        </select>
      </label>}

      {!demo && (
        <>
          {canStartIncrement && <button className="btn btn-ghost" type="button"
            onClick={() => void actions.startIncrementRound()}
            title="基于当前正式版本开始一轮增量建模">
            <GitBranch size={13} />增量
          </button>}
          <button
            className="btn btn-ghost"
            disabled={settingsLoading}
            onClick={onOpenMcpSettings}
            title="打开当前项目的 MCP 接入配置"
          >
            <Settings size={13} />MCP
          </button>
          {structuralPolicy && <div className="wb-policy-menu">
            <button className="btn btn-ghost" type="button" aria-expanded={policyOpen}
              onClick={() => { setPolicyOpen((open) => !open); setPolicyConfirming(false); }}
              title="设置当前项目的确认策略">
              <Icon name="check" size={13} />确认策略
            </button>
            {policyOpen && <section className="wb-policy-pop" role="dialog" aria-label="当前项目确认策略">
              <div className="wb-policy-pop-head"><b>当前项目确认策略</b><button className="btn btn-ghost btn-sm" type="button" aria-label="关闭确认策略" onClick={() => setPolicyOpen(false)}><Icon name="x" size={12} /></button></div>
              <p>只影响当前项目的对象、操作和指标结构变更。业务口径与脚本始终需要人工确认，可随时在这里撤销。</p>
              <div className="wb-policy-pop-state"><span>结构变更</span><b>{structuralPolicy.mode === 'auto' ? '自动接受' : '每次询问'}</b></div>
              {policyConfirming ? <div className="wb-policy-warning">
                <b>{structuralPolicy.mode === 'auto' ? '确认恢复每次询问？' : '确认开启自动接受？'}</b>
                <span>{structuralPolicy.mode === 'auto'
                  ? '撤销后，下一次结构变更会重新等待你的确认。'
                  : '开启后，当前项目的结构变更可能跳过人工审阅；业务口径和脚本不受影响。'}</span>
                <div className="btn-row"><button className="btn btn-sm" type="button" onClick={() => setPolicyConfirming(false)}>取消</button><button className="btn btn-p btn-sm" type="button" onClick={async () => { await actions.setConfirmPolicy('structural', structuralPolicy.mode === 'auto' ? 'ask' : 'auto'); setPolicyConfirming(false); }}>确认更改</button></div>
              </div> : <button className="btn btn-sm" type="button" onClick={() => setPolicyConfirming(true)}>{structuralPolicy.mode === 'auto' ? '撤销自动接受' : '更改为自动接受'}</button>}
            </section>}
          </div>}
          <button className="btn btn-ghost" onClick={() => void exportPackage()} title="导出当前已确认知识包">
            <Icon name="download" size={13} />知识包
          </button>
          <button className="btn btn-ghost" disabled={transferProgress !== null}
            onClick={() => void exportProject()} title="导出完整项目迁移包">
            <Archive size={13} />{transferProgress === null ? '迁移包' : `${transferProgress}%`}
          </button>
          <button className="btn btn-ghost" onClick={actions.openEvaluation} title="打开正式评测">
            <Icon name="check" size={13} />评测
          </button>
        </>
      )}

      <button className="btn btn-ghost" onClick={actions.toggleDrawer}>
        <Icon name="history" size={13} />过程
      </button>
    </header>
  );
}

function packageStatusLabel(status) {
  return ({ released: '已发布', review: '候选', draft: '草稿', deprecated: '已停用' })[status] || '进行中';
}
