import { store, actions, SCENES } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { project } from '../lib/mock/fixtures.js';
import Icon from '../ui/Icon.jsx';
import { downloadPackage } from '../lib/api.js';

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

export default function TopBar() {
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
  const counts = demo ? project.counts : {
    flows: files.filter((path) => path.startsWith('diagrams/') && path.endsWith('.mmd')).length,
    objects: files.filter((path) => path.startsWith('objects/') && path.endsWith('.md') && !path.endsWith('index.md')).length,
    scripts: files.filter((path) => path.startsWith('scripts/') && path.endsWith('.py')).length,
    pending: interaction ? 1 : 0,
  };
  const incr = scene === 'increment';
  const running = !interaction && ['queued', 'running'].includes(run?.status);
  const projectName = demo ? project.name : (realProject?.name || '正在加载项目');
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

  return (
    <header className="wb-top">
      {!demo && <a className="btn btn-ghost btn-sm" href="/" title="返回项目列表"><Icon name="arrow" size={12} />项目</a>}
      <span className="goal ellip" title={projectName}>{projectName}</span>
      <span className="sep">·</span>
      <span className="k">
        {demo
          ? (incr ? '1.2.0 · 已发布' : project.revision)
          : packageVersion
            ? `v${String(packageVersion).replace(/^v/i, '')} · ${packageStatusLabel(packageStatus)}`
            : '准备中'}
      </span>

      {incr ? (
        <>
          <span className="pill pill-ok">11 项保持有效</span>
          <span className="pill pill-stale">4 项需处理</span>
        </>
      ) : (
        <span className="pill pill-wait">待确认 {counts.pending}</span>
      )}

      {!demo && workflow?.phaseLabel && (
        <span className="pill" title={`当前阶段：${workflow.phaseLabel}`}>{workflow.phaseLabel}</span>
      )}

      {!interaction && (activity || running) && (
        <span className="pill pill-agent"><i className="dot-pulse" />助手运行中</span>
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
          <button className="btn btn-ghost" onClick={() => void exportPackage()} title="导出当前已确认知识包">
            <Icon name="download" size={13} />导出
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
