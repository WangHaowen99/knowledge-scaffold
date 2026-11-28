// 代码生成与求解（插件页，设计稿 scripts）：脚本、可选的确定性执行契约与求解契约三个页签。
// 插件页只发命令、不跑助手（S-20）：扫描、生成脚本、修复、重新生成、按模板重新生成、生成评估器都发到工作台对话，
// 页面显示进度条和「去对话里看」，跑完列表刷新（S-19 恢复、EP-10 进度、EP-06 刷新）。
// 求解契约在助手写入范围里（CS-06 / EP-03）时只读到这一轮结束，不再有覆盖保存的冲突框。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { Button, Tabs } from '../../ui/index.jsx';
import { createLatestRequest } from '../../lib/latestRequest.js';
import {
  CommandRunBar, ExtraActions, PluginEyebrow, PluginOff, SendButton, useCommandTasks, usePageEnabled, useProjectEvent,
} from '../plugin/parts.jsx';
import '../../styles/scripts.css';
import ScriptsTab from './ScriptsTab.jsx';
import SolverTab from './SolverTab.jsx';
import DeterministicTab from './DeterministicTab.jsx';
import {
  SOLVER_DIR, SOLVER_TYPE, availableScriptsTab, pageTab, scanCommand, scanLock,
} from './scriptsModel.js';

const LABEL = '代码生成与求解';

export default function ScriptsPage({ route }) {
  const { api } = useApp();
  const { pid, scope } = useProject();
  const enabled = usePageEnabled('scripts');
  const [tab, setTab] = useState(pageTab(route.query));
  useEffect(() => { if (route.query?.tab || route.query?.node) setTab(pageTab(route.query)); }, [route.query?.tab, route.query?.node]);

  const [scripts, setScripts] = useState(null);
  const [sandbox, setSandbox] = useState(null);
  const [scriptsTick, setScriptsTick] = useState(0);
  const [solver, setSolver] = useState(null);
  const [deterministic, setDeterministic] = useState(null);
  const [deterministicError, setDeterministicError] = useState('');
  // null 表示插件状态仍在加载；只有明确关闭后，才允许把直接链接的 dt 页签回退到脚本页。
  const [deterministicEnabled, setDeterministicEnabled] = useState(null);
  const deterministicRequest = useRef(null);
  if (deterministicRequest.current === null) deterministicRequest.current = createLatestRequest();

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
      setSolver(await api.solverContract(pid));
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  }, [api, pid]);
  const loadDeterministic = useCallback((callbacks = {}) => deterministicRequest.current.run(
    async () => {
      const packs = await api.projectPacks(pid);
      const enabledPack = (packs.items || []).some(
        (item) => item.name === 'deterministic-execution' && ['enabled', 'required'].includes(item.status),
      );
      return {
        enabled: enabledPack,
        view: enabledPack ? await api.deterministicContract(pid) : null,
      };
    },
    {
      success: ({ enabled: nextEnabled, view }) => {
        callbacks.beforeApply?.({ enabled: nextEnabled, view });
        setDeterministicError('');
        setDeterministicEnabled(nextEnabled);
        setDeterministic(view);
        callbacks.applied?.({ enabled: nextEnabled, view });
      },
      // 刷新失败不是插件关闭：保留最后成功状态和编辑器，避免卸载时丢失本地草稿。
      failure: (e) => {
        const message = errorText(e);
        setDeterministicError(message);
        callbacks.failed?.(e);
        toast(message, 'alert');
      },
      settled: () => {},
    },
  ), [api, pid]);
  useEffect(() => {
    loadScripts();
    loadSolver();
    loadDeterministic();
    return () => deterministicRequest.current.invalidate();
  }, [loadScripts, loadSolver, loadDeterministic]);
  useEffect(() => {
    const available = availableScriptsTab(tab, deterministicEnabled);
    if (available !== tab) setTab(available);
  }, [tab, deterministicEnabled]);

  // 命令跑完：列表刷新；没做完的提示去对话里看
  const tasks = useCommandTasks('scripts', {
    onFinish: (task) => {
      loadScripts();
      loadSolver();
      loadDeterministic();
      if (task.state !== 'done') toast(`${String(task.title || '这一步').replace(/^助手正在/, '')}没做完，去对话里看看原因`, 'alert');
    },
  });
  // 结果回到这里：产物更新（脚本、扫描报告、求解契约）
  useProjectEvent('artifacts.updated', (d) => {
    if (!String(d.type || '').startsWith('codegen.')) return;
    if (d.type === 'codegen.solver_contract') loadSolver();
    else {
      loadScripts();
      loadDeterministic();
    }
  });

  // 别处改了脚本或求解契约（Studio、助手、MCP）：跟着刷新
  useProjectEvent('workspace.changed', (d) => {
    const paths = d.paths || [];
    const executionDependencyChanged = (d.entries || []).some((e) => ['operation', 'script'].includes(e.kind))
      || paths.some((p) => /(^|\/)(operation|logic|scripts)\//.test(p));
    if ((d.entries || []).some((e) => e.kind === 'script') || paths.some((p) => /(^|\/)scripts\//.test(p))) loadScripts();
    if ((d.artifacts || []).some((a) => a.type === SOLVER_TYPE) || paths.some((p) => p.startsWith(SOLVER_DIR))) loadSolver();
    if (executionDependencyChanged || paths.some((p) => p.startsWith('ext/deterministic-execution/contracts/'))) loadDeterministic();
  });

  // 保存成功：响应就是新的 SolverContractView
  const onSaved = useCallback((view) => {
    setSolver(view);
  }, []);
  const onDeterministicSaved = useCallback((view) => {
    // 保存响应比此前发起的刷新更新；使在飞刷新失效，避免其晚到后覆盖新修订。
    deterministicRequest.current.invalidate();
    setDeterministic(view);
  }, []);

  const lock = scope || solver?.lock || null;

  if (enabled === false) {
    return (
      <ShellP active="scripts">
        <PageHead title={LABEL} eyebrow={<PluginEyebrow pack="codegen" label={LABEL} />} />
        <PluginOff label={LABEL} what="脚本和求解契约" />
      </ShellP>
    );
  }

  const scriptCount = scripts?.items?.length ?? 0;
  const enabledCount = (solver?.operations || []).filter((op) => op.enabled).length;
  const deterministicCount = (deterministic?.operations || []).filter((op) => op.enabled).length;
  const tabItems = [['sc', '脚本', scriptCount, 'code']];
  if (deterministicEnabled === true || (deterministicEnabled === null && tab === 'dt')) {
    tabItems.push(['dt', '确定性执行', deterministicCount, 'cpu']);
  }
  tabItems.push(['sv', '求解契约', enabledCount, 'cpu']);

  return (
    <ShellP active="scripts">
      <PageHead
        eyebrow={<PluginEyebrow pack="codegen" label={LABEL} />}
        title={LABEL}
        actions={(
          <>
            <ExtraActions page="scripts" tasks={tasks} />
            <SendButton tasks={tasks} {...scanCommand()} icon="scan" disabled={!!scanLock(tasks.active)} title={scanLock(tasks.active) || undefined}>
              扫描可代码化的操作
            </SendButton>
          </>
        )}
      />
      {tasks.active ? (
        <div className="mr-runrow" style={{ margin: '0 0 14px' }}>
          <CommandRunBar task={tasks.active} />
        </div>
      ) : null}
      <Tabs
        items={tabItems}
        value={tab}
        onChange={setTab}
      />
      <div className={tab === 'sc' ? 'mt16' : 'mt16 hide'}>
        <ScriptsTab data={scripts} sandbox={sandbox} tick={scriptsTick} tasks={tasks} />
      </div>
      <div className={tab === 'sv' ? 'mt16' : 'mt16 hide'}>
        <SolverTab view={solver} lock={lock} tasks={tasks} onSaved={onSaved} reload={loadSolver} />
      </div>
      {deterministicEnabled ? (
        <div className={tab === 'dt' ? 'mt16' : 'mt16 hide'}>
          <DeterministicTab view={deterministic} lock={lock} tasks={tasks} onSaved={onDeterministicSaved} reload={loadDeterministic} />
        </div>
      ) : tab === 'dt' && deterministicEnabled === null ? (
        <div className="card mt16">
          <div className="card-b">
            {deterministicError ? (
              <div className="row g8">
                <span className="grow" style={{ color: 'var(--red)' }}>确定性执行契约读取失败：{deterministicError}</span>
                <Button size="sm" kind="ghost" icon="refresh" onClick={() => loadDeterministic()}>重新加载</Button>
              </div>
            ) : <div className="muted sm">正在读取确定性执行契约…</div>}
          </div>
        </div>
      ) : null}
    </ShellP>
  );
}
