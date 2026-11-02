// 代码生成与求解（插件页，设计稿 scripts）：脚本（SC-01～SC-03、ST-04 / F-02 只读预览）与求解契约（SV-01～SV-04）两个页签。
// 插件页只发命令、不跑助手（S-20）：扫描、生成脚本、修复、重新生成、按模板重新生成、生成评估器都发到工作台对话，
// 页面显示进度条和「去对话里看」，跑完列表刷新（S-19 恢复、EP-10 进度、EP-06 刷新）。
// 求解契约在助手写入范围里（CS-06 / EP-03）时只读到确认生效或撤回，不再有覆盖保存的冲突框。
import { useCallback, useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { Tabs } from '../../ui/index.jsx';
import {
  CommandRunBar, ExtraActions, PluginEyebrow, PluginOff, SendButton, useCommandTasks, usePageEnabled, useProjectEvent,
} from '../plugin/parts.jsx';
import '../../styles/scripts.css';
import ScriptsTab from './ScriptsTab.jsx';
import SolverTab from './SolverTab.jsx';
import {
  SOLVER_DIR, SOLVER_TYPE, scanCommand,
} from './scriptsModel.js';

const LABEL = '代码生成与求解';

export default function ScriptsPage({ route }) {
  const { api } = useApp();
  const { pid, scope } = useProject();
  const enabled = usePageEnabled('scripts');
  const [tab, setTab] = useState(route.query?.tab === 'sv' ? 'sv' : 'sc');
  useEffect(() => { if (route.query?.tab) setTab(route.query.tab === 'sv' ? 'sv' : 'sc'); }, [route.query?.tab]);

  const [scripts, setScripts] = useState(null);
  const [sandbox, setSandbox] = useState(null);
  const [scriptsTick, setScriptsTick] = useState(0);
  const [solver, setSolver] = useState(null);
  const [receipts, setReceipts] = useState(null);

  const loadScripts = useCallback(async () => {
    try {
      const [r, s] = await Promise.all([api.scripts(pid), api.sandboxStatus().catch(() => null)]);
      setScripts(r);
      setSandbox(s || r.sandbox || null);
      setScriptsTick((t) => t + 1);
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  }, [api, pid]);
  const loadSolver = useCallback(async () => {
    try {
      const [v, rc] = await Promise.all([api.solverContract(pid), api.solverReceipts(pid).catch(() => null)]);
      setSolver(v);
      setReceipts(rc);
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  }, [api, pid]);
  useEffect(() => { loadScripts(); loadSolver(); }, [loadScripts, loadSolver]);

  // 命令跑完：列表刷新；没做完的提示去对话里看
  const tasks = useCommandTasks('scripts', {
    onFinish: (task) => {
      loadScripts();
      loadSolver();
      if (task.state !== 'done') toast(`${String(task.title || '这一步').replace(/^助手正在/, '')}没做完，去对话里看看原因`, 'alert');
    },
  });
  // 结果回到这里：产物更新（脚本、扫描报告、求解契约）
  useProjectEvent('artifacts.updated', (d) => {
    if (!String(d.type || '').startsWith('codegen.')) return;
    if (d.type === 'codegen.solver_contract') loadSolver();
    else loadScripts();
  });

  // 别处改了脚本或求解契约（Studio、助手、MCP）：跟着刷新
  useProjectEvent('workspace.changed', (d) => {
    const paths = d.paths || [];
    if ((d.entries || []).some((e) => e.kind === 'script') || paths.some((p) => /(^|\/)scripts\//.test(p))) loadScripts();
    if ((d.artifacts || []).some((a) => a.type === SOLVER_TYPE) || paths.some((p) => p.startsWith(SOLVER_DIR))) loadSolver();
  });

  // 保存成功：响应就是新的 SolverContractView
  const onSaved = useCallback((view) => {
    setSolver(view);
    api.solverReceipts(pid).then(setReceipts).catch(() => {});
  }, [api, pid]);

  const lock = scope || solver?.lock || null;

  if (enabled === false) {
    return (
      <ShellP active="scripts">
        <PageHead title={LABEL} eyebrow={<PluginEyebrow pack="solver" label={LABEL} />} />
        <PluginOff label={LABEL} what="脚本和求解契约" />
      </ShellP>
    );
  }

  const scriptCount = scripts?.items?.length ?? 0;
  const enabledCount = (solver?.operations || []).filter((op) => op.enabled).length;

  return (
    <ShellP active="scripts">
      <PageHead
        eyebrow={<PluginEyebrow pack="solver" label={LABEL} />}
        title={LABEL}
        actions={(
          <>
            <ExtraActions page="scripts" tasks={tasks} />
            <SendButton tasks={tasks} {...scanCommand()} icon="scan">扫描可代码化的操作</SendButton>
          </>
        )}
      />
      {tasks.active ? (
        <div className="mr-runrow" style={{ margin: '0 0 14px' }}>
          <CommandRunBar task={tasks.active} />
        </div>
      ) : null}
      <Tabs
        items={[['sc', '脚本', scriptCount, 'code'], ['sv', '求解契约', enabledCount, 'cpu']]}
        value={tab}
        onChange={setTab}
      />
      <div className={tab === 'sc' ? 'mt16' : 'mt16 hide'}>
        <ScriptsTab data={scripts} sandbox={sandbox} tick={scriptsTick} tasks={tasks} />
      </div>
      <div className={tab === 'sv' ? 'mt16' : 'mt16 hide'}>
        <SolverTab view={solver} receipts={receipts} lock={lock} tasks={tasks} onSaved={onSaved} reload={loadSolver} />
      </div>
    </ShellP>
  );
}
