import { useEffect, useMemo, useRef, useState } from 'react';
import { actions, store } from '../lib/appStore.js';
import { listPackageSources } from '../lib/api.js';
import { useStore } from '../lib/store.js';
import Icon from '../ui/Icon.jsx';
import {
  compileFusionPlan,
  createFusionPlan,
  listFusionPlans,
  updateFusionPlan,
} from './fusion-api.js';
import {
  defaultFusionOutput,
  dispositionFor,
  availableFusionScopes,
  duplicateFusionPackageIds,
  FUSION_SCOPE_META,
  fusionConceptGroups,
  fusionScopeCount,
  fusionScopeProblems,
  KIND_META,
  ledgerEntries,
  planStage,
  qualityRows,
  sourceLabel,
  unresolvedEntries,
} from './fusion-model.js';
import './fusion.css';

const selProjectId = (state) => state.projectId;
const selProject = (state) => state.project;
const selRevision = (state) => state.revision;
const selPackageVersion = (state) => state.packageVersion;
const selCompletedPackageUploads = (state) => (state.messages || []).flatMap((message) => (
  (message.blocks || []).flatMap((block) => block.type === 'upload' ? block.files || [] : [])
)).filter((file) => file.kind === 'package' && ['parsed', 'failed'].includes(file.status))
  .map((file) => `${file.id}:${file.status}`).join('|');

export default function FusionView() {
  const projectId = useStore(store, selProjectId);
  const project = useStore(store, selProject);
  const revision = useStore(store, selRevision);
  const packageVersion = useStore(store, selPackageVersion);
  const completedPackageUploads = useStore(store, selCompletedPackageUploads);
  const previousPackageUploads = useRef(completedPackageUploads);
  const defaults = useMemo(() => defaultFusionOutput({
    projectId,
    projectName: project?.name,
    packageVersion,
  }), [projectId, project?.name, packageVersion]);
  const [sources, setSources] = useState([]);
  const [plans, setPlans] = useState([]);
  const [activeId, setActiveId] = useState('');
  const [mode, setMode] = useState('loading');
  const [selected, setSelected] = useState([]);
  const [sourceScopes, setSourceScopes] = useState({});
  const [output, setOutput] = useState(defaults);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [failedQuality, setFailedQuality] = useState(null);

  const load = async (preferredId = '') => {
    if (!projectId) return;
    setError('');
    try {
      const [sourceResult, planResult] = await Promise.all([
        listPackageSources(projectId),
        listFusionPlans(projectId),
      ]);
      const nextSources = sourceResult.sources || [];
      const nextPlans = Array.isArray(planResult) ? planResult : [];
      const nextActive = preferredId && nextPlans.some((plan) => plan.id === preferredId)
        ? preferredId
        : nextPlans[0]?.id || '';
      setSources(nextSources);
      setPlans(nextPlans);
      setActiveId(nextActive);
      setSelected((current) => {
        const available = new Set(nextSources.map((source) => source.id));
        const previousAvailable = new Set(sources.map((source) => source.id));
        const kept = current.filter((id) => available.has(id));
        const added = nextSources.map((source) => source.id).filter((id) => !previousAvailable.has(id));
        if (!sources.length && !current.length) return nextSources.map((source) => source.id);
        return [...new Set([...kept, ...added])];
      });
      setSourceScopes((current) => Object.fromEntries(nextSources.map((source) => [
        source.id,
        (current[source.id] || availableFusionScopes(source)).filter((scope) => availableFusionScopes(source).includes(scope)),
      ])));
      setMode(nextActive ? 'plan' : 'new');
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '融合信息读取失败');
      setMode('error');
    }
  };

  useEffect(() => { void load(); }, [projectId]);
  useEffect(() => { setOutput(defaults); }, [defaults]);
  useEffect(() => {
    if (completedPackageUploads === previousPackageUploads.current) return;
    previousPackageUploads.current = completedPackageUploads;
    void load(activeId);
  }, [completedPackageUploads]);

  const active = plans.find((plan) => plan.id === activeId) || null;
  const replacePlan = (nextPlan) => {
    setPlans((current) => current.some((plan) => plan.id === nextPlan.id)
      ? current.map((plan) => plan.id === nextPlan.id ? nextPlan : plan)
      : [nextPlan, ...current]);
    setActiveId(nextPlan.id);
    setMode('plan');
  };

  const create = async () => {
    if (selected.length < 2 || !revision || !output.name.trim() || !output.description.trim()) return;
    setBusy('create');
    setError('');
    setFailedQuality(null);
    try {
      const plan = await createFusionPlan(projectId, {
        expectedRevision: revision,
        sourceSelections: selected.map((sourceId) => ({
          sourceId,
          scopes: sourceScopes[sourceId] || [],
        })),
        output: { ...output, name: output.name.trim(), description: output.description.trim() },
      });
      replacePlan(plan);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '融合方案建立失败');
    } finally {
      setBusy('');
    }
  };

  const resolve = async (entries, action = 'preserve', details = {}) => {
    if (!active || !entries.length) return;
    setBusy('resolve');
    setError('');
    try {
      replacePlan(await updateFusionPlan(projectId, active.id, {
        dispositions: entries.map((entry) => dispositionFor(entry, action, details)),
      }));
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '元素去向保存失败');
    } finally {
      setBusy('');
    }
  };

  const compile = async () => {
    if (!active || active.unresolvedCount || active.status !== 'ready') return;
    setBusy('compile');
    setError('');
    setFailedQuality(null);
    try {
      const plan = await compileFusionPlan(projectId, active.id);
      replacePlan(plan);
      void actions.refreshSnapshot().catch(() => {
        setError('候选已经生成，变更详情正在重新连接；你仍可在消息流里继续。');
      });
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '融合结果检查失败');
      setFailedQuality(nextError?.details?.quality || null);
      void load(active.id);
    } finally {
      setBusy('');
    }
  };

  if (mode === 'loading') return <FusionLoading />;
  if (mode === 'error') return <FusionError message={error} onRetry={() => { setMode('loading'); void load(); }} />;
  if (mode === 'new') {
    return (
      <FusionBuilder
        sources={sources}
        selected={selected}
        setSelected={setSelected}
        sourceScopes={sourceScopes}
        setSourceScopes={setSourceScopes}
        output={output}
        setOutput={setOutput}
        plans={plans}
        baselineReady={Boolean(revision)}
        busy={busy}
        error={error}
        onCreate={create}
        onOpenPlan={(id) => { setActiveId(id); setMode('plan'); setError(''); }}
      />
    );
  }
  return (
    <FusionPlan
      plan={active}
      plans={plans}
      sources={sources}
      busy={busy}
      error={error}
      failedQuality={failedQuality}
      onResolve={resolve}
      onCompile={compile}
      onSelectPlan={(id) => { setActiveId(id); setError(''); setFailedQuality(null); }}
      onNew={() => { setMode('new'); setError(''); setFailedQuality(null); }}
      onRetry={() => void load(active?.id)}
    />
  );
}

export function FusionBuilder({ sources, selected, setSelected, sourceScopes, setSourceScopes, output, setOutput, plans, baselineReady, busy, error, onCreate, onOpenPlan }) {
  const selectedSet = new Set(selected);
  const scopesReady = selected.every((sourceId) => (sourceScopes[sourceId] || []).length > 0);
  const duplicatePackageIds = duplicateFusionPackageIds(sources.filter((source) => selectedSet.has(source.id)));
  const scopeProblems = sources.filter((source) => selectedSet.has(source.id)).flatMap((source) => (
    fusionScopeProblems(source, sourceScopes[source.id]).map((problem) => ({ ...problem, source }))
  ));
  const toggle = (id) => setSelected(selectedSet.has(id)
    ? selected.filter((sourceId) => sourceId !== id)
    : [...selected, id]);
  const toggleScope = (source, scope) => {
    const current = sourceScopes[source.id] || availableFusionScopes(source);
    const next = current.includes(scope) ? current.filter((item) => item !== scope) : [...current, scope];
    if (!next.length) return;
    setSourceScopes({ ...sourceScopes, [source.id]: next });
  };
  return (
    <div className="fusion" aria-busy={Boolean(busy)}>
      <header className="fusion-intro">
        <div>
          <h2>多来源知识包融合</h2>
          <p>逐份选择纳入内容，再明确每项去向，最后只生成一个整体候选。</p>
        </div>
        {plans.length > 0 && <button className="btn" onClick={() => onOpenPlan(plans[0].id)}>返回最近方案</button>}
      </header>

      <section className="fusion-section" aria-labelledby="fusion-source-title">
        <div className="fusion-section-head">
          <div><h3 id="fusion-source-title">1. 选择来源包</h3><p>已选 {selected.length}/{sources.length}，至少选择两份。</p></div>
          {sources.length > 0 && (
            <button className="btn btn-sm" onClick={() => setSelected(selected.length === sources.length ? [] : sources.map((source) => source.id))}>
              {selected.length === sources.length ? '清空' : '全选'}
            </button>
          )}
        </div>
        {sources.length ? (
          <div className="fusion-sources">
            {sources.map((source) => (
              <div className={`fusion-source${selectedSet.has(source.id) ? ' on' : ''}`} key={source.id}>
                <input aria-label={`选择${source.name || source.filename}`} type="checkbox" checked={selectedSet.has(source.id)} onChange={() => toggle(source.id)} />
                <span><b>{source.name || source.filename}</b><small>{source.version} · {source.fileCount} 个文件 · package id: {source.packageId}</small>
                  {duplicatePackageIds.has(source.packageId) && <small className="fusion-source-warning">与另一来源使用相同 package id，系统会按来源指纹分别处理</small>}
                </span>
                {selectedSet.has(source.id) && <div className="fusion-source-scopes" aria-label={`${source.name || source.filename}纳入范围`}>
                  {availableFusionScopes(source).map((scope) => {
                    const count = fusionScopeCount(source, scope);
                    const included = (sourceScopes[source.id] || []).includes(scope);
                    return <label key={scope}>
                    <input type="checkbox" checked={(sourceScopes[source.id] || []).includes(scope)} onChange={() => toggleScope(source, scope)} />
                    <span>{FUSION_SCOPE_META[scope].label}{count === null ? '' : ` ${included ? count : 0}/${count}`}</span>
                    </label>;
                  })}
                </div>}
              </div>
            ))}
          </div>
        ) : (
          <div className="fusion-empty">
            <Icon name="upload" size={18} />
            <b>还没有可用的来源包</b>
            <span>把知识包 ZIP 拖到工作台任意位置；解析结果会回到消息流，你仍可继续输入。</span>
          </div>
        )}
      </section>

      {scopeProblems.length > 0 && <section className="fusion-scope-warning" role="alert">
        <b>当前范围会留下 {scopeProblems.length} 处依赖缺口</b>
        <p>请补选依赖内容，或取消勾选引用它的内容；修复前不会建立方案。</p>
        {scopeProblems.slice(0, 5).map((problem) => <span key={`${problem.source.id}:${problem.from}:${problem.requires}`}>
          {problem.source.name || problem.source.filename}：{problem.from} 需要 {problem.requires}
        </span>)}
      </section>}

      <section className="fusion-section" aria-labelledby="fusion-output-title">
        <div className="fusion-section-head"><div><h3 id="fusion-output-title">2. 设置融合结果</h3><p>名称和说明可以手动调整，来源包保持只读。</p></div></div>
        <div className="fusion-fields">
          <label><span>知识包名称</span><input className="input" value={output.name} onChange={(event) => setOutput({ ...output, name: event.target.value })} /></label>
          <label className="wide"><span>融合说明</span><textarea className="input" rows="3" value={output.description} onChange={(event) => setOutput({ ...output, description: event.target.value })} /></label>
        </div>
      </section>

      {error && <InlineError message={error} />}
      <div className="fusion-actions">
        <span>{!baselineReady ? '正在读取当前知识包，请稍候'
          : selected.length < 2 ? '再选择至少一份来源包'
            : scopeProblems.length ? '先处理上方依赖缺口' : '下一步会列出每个元素的去向，不会直接修改当前知识包'}</span>
        <button className="btn btn-p" disabled={!baselineReady || selected.length < 2 || !scopesReady || scopeProblems.length > 0 || !output.name.trim() || !output.description.trim() || Boolean(busy)} onClick={onCreate}>
          {busy === 'create' ? '正在建立…' : '建立融合方案'}
        </button>
      </div>
    </div>
  );
}

export function FusionPlan({ plan, plans, sources, busy, error, failedQuality, onResolve, onCompile, onSelectPlan, onNew, onRetry }) {
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('');
  const [pendingOnly, setPendingOnly] = useState(false);
  const [limit, setLimit] = useState(80);
  const sourceMap = useMemo(() => new Map([
    ...sources.map((source) => [source.id, source]),
    ...(plan?.sources || []).map((source) => [source.sourceId, source]),
  ]), [sources, plan]);
  const entries = ledgerEntries(plan);
  const unresolved = unresolvedEntries(plan);
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN');
  const filtered = entries.filter((entry) => (
    (!kind || entry.kind === kind)
    && (!pendingOnly || !entry.decision)
    && (!normalizedQuery || `${entry.sourceKey} ${entry.fusedKey} ${sourceLabel(sourceMap.get(entry.sourceId))}`
      .toLocaleLowerCase('zh-CN').includes(normalizedQuery))
  ));
  const stage = planStage(plan);
  const rows = qualityRows(plan, failedQuality);
  const mutable = ['awaiting_decisions', 'ready'].includes(plan?.status);
  const conceptGroups = fusionConceptGroups(plan);
  const conceptMemberIds = new Set([...conceptGroups.common, ...conceptGroups.parameterized].flatMap((concept) => (
    (concept.members || []).map((member) => `${member.sourceId}\0${member.kind}\0${member.key}`)
  )));
  const scenarioCount = entries.filter((entry) => !conceptMemberIds.has(`${entry.sourceId}\0${entry.kind}\0${entry.sourceKey}`)).length;

  useEffect(() => { setLimit(80); }, [query, kind, pendingOnly, plan?.id]);

  return (
    <div className="fusion" aria-busy={Boolean(busy)}>
      <header className="fusion-intro">
        <div>
          <div className={`pill pill-${stage.tone}`}>{stage.label}</div>
          <h2>{plan?.output?.name || '融合方案'}</h2>
          <p>{stage.detail}</p>
        </div>
        <div className="fusion-plan-nav">
          {plans.length > 1 && (
            <label><span>查看方案</span><select className="input" value={Math.max(0, plans.findIndex((item) => item.id === plan?.id))} onChange={(event) => onSelectPlan(plans[Number(event.target.value)]?.id)}>
              {plans.map((item, index) => <option value={index} key={item.id}>{item.output?.name || '融合方案'} · {planStage(item).label}</option>)}
            </select></label>
          )}
          <button className="btn" onClick={onNew}>新建方案</button>
        </div>
      </header>

      <section className="fusion-section">
        <div className="fusion-section-head"><div><h3>来源与融合原则</h3><p>{(plan?.sources || []).length} 份只读来源，一次生成一个候选。</p></div></div>
        <div className="fusion-source-summary">
          {(plan?.sources || []).map((source) => <span key={source.sourceId}>{sourceLabel(source)}</span>)}
        </div>
        <p className="fusion-principle">同名不等于同义。每项都必须明确独立保留、经业务依据归并，或说明原因后不纳入；未确定项不会静默消失。</p>
      </section>

      <section className="fusion-section" aria-labelledby="fusion-concepts-title">
        <div className="fusion-section-head">
          <div><h3 id="fusion-concepts-title">跨包业务概念</h3><p>公共层 {conceptGroups.common.length} 组 · 参数化层 {conceptGroups.parameterized.length} 组 · 场景层 {scenarioCount} 项。</p></div>
          {mutable && <button className="btn btn-sm" onClick={() => void actions.send(
            '请读取全部来源定义和证据，按公共层、参数化层整理跨包业务概念；对仍需判断的关系逐项给我业务选项，并把确认结果登记到当前融合方案。',
          )}>让助手分析业务关系</button>}
        </div>
        {conceptGroups.common.length + conceptGroups.parameterized.length ? (
          <div className="fusion-concept-groups">
            {[
              ['common', '公共层', conceptGroups.common],
              ['parameterized', '参数化层', conceptGroups.parameterized],
            ].map(([id, label, concepts]) => concepts.length > 0 && <div className="fusion-concept-group" key={id}>
              <h4>{label} · {concepts.length}</h4>
              {concepts.map((concept) => <article className="fusion-concept" key={concept.id}>
                <header><b>{concept.title}</b><span className="pill pill-ok">保留场景视图</span></header>
                <p>{concept.rationale}</p>
                <div>{(concept.members || []).map((member) => <span key={`${member.sourceId}:${member.kind}:${member.key}`}>
                  {sourceLabel(sourceMap.get(member.sourceId))} · {KIND_META[member.kind]?.label || member.kind} · <code className="k">{member.key}</code> · {relationLabel(member.relation)}
                </span>)}</div>
              </article>)}
            </div>)}
          </div>
        ) : <div className="fusion-concept-empty">助手尚未登记跨包业务概念。逐元素去向仍可继续处理，但生成候选前应先核对同名、粒度和部分重叠关系。</div>}
      </section>

      <section className="fusion-section" aria-labelledby="fusion-ledger-title">
        <div className="fusion-section-head">
          <div><h3 id="fusion-ledger-title">逐元素去向账本</h3><p>已确定 {entries.length - unresolved.length}/{entries.length}，未确定 {unresolved.length}。</p></div>
          {mutable && unresolved.length > 0 && (
            <button className="btn btn-p" disabled={Boolean(busy)} onClick={() => onResolve(unresolved)}>
              {busy === 'resolve' ? '正在保存…' : `全部保留来源视图（${unresolved.length}）`}
            </button>
          )}
        </div>
        <div className="fusion-ledger-tools">
          <label className="fusion-search"><Icon name="search" size={13} /><input className="input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索元素或来源包" /></label>
          <select className="input" aria-label="按元素类型筛选" value={kind} onChange={(event) => setKind(event.target.value)}>
            <option value="">全部类型</option>
            {Object.entries(KIND_META).map(([value, meta]) => <option value={value} key={value}>{meta.label}</option>)}
          </select>
          <label className="fusion-toggle"><input type="checkbox" checked={pendingOnly} onChange={(event) => setPendingOnly(event.target.checked)} />只看未确定</label>
        </div>
        <div className="fusion-ledger" role="region" aria-label="逐元素去向账本" tabIndex="0">
          <table className="grid">
            <thead><tr><th>来源包</th><th>类型</th><th>来源元素</th><th>融合后去向</th><th>裁决</th></tr></thead>
            <tbody>
              {filtered.slice(0, limit).map((entry) => {
                const meta = KIND_META[entry.kind] || { label: '条目' };
                return (
                  <tr key={`${entry.sourceId}:${entry.kind}:${entry.sourceKey}`}>
                    <td>{sourceLabel(sourceMap.get(entry.sourceId))}</td>
                    <td><span className="pill pill-plain">{meta.label}</span></td>
                    <td><code className="k">{entry.sourceKey}</code></td>
                    <td>{entry.fusedKey ? <code className="k">{entry.fusedKey}</code>
                      : entry.decision === 'canonicalize' ? <span>归并到 <code className="k">{entry.targetKey}</code></span>
                        : ['exclude', 'exclude_scope'].includes(entry.decision) ? <span className="muted">不生成此项</span>
                          : entry.decision === 'preserve' ? <span className="muted">编译时确定名称</span>
                            : <span className="muted">待确定</span>}</td>
                    <td><DecisionControl
                      entry={entry}
                      entries={entries}
                      sourceMap={sourceMap}
                      mutable={mutable}
                      busy={Boolean(busy)}
                      onResolve={onResolve}
                    /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!filtered.length && <div className="fusion-table-empty">没有符合当前筛选的元素</div>}
        </div>
        {filtered.length > limit && <button className="btn btn-ghost fusion-more" onClick={() => setLimit(limit + 80)}>再显示 {Math.min(80, filtered.length - limit)} 项</button>}
      </section>

      <section className="fusion-section" aria-labelledby="fusion-quality-title">
        <div className="fusion-section-head"><div><h3 id="fusion-quality-title">质量门禁</h3><p>未通过时不会生成候选，也不会修改当前知识包。</p></div></div>
        <div className="fusion-gates">
          {rows.map((row) => (
            <div className="fusion-gate" key={row.id}>
              <span className={`fusion-gate-icon ${row.passed === null ? 'pending' : row.passed ? 'pass' : 'fail'}`} aria-hidden="true">
                {row.passed === null ? '·' : row.passed ? '✓' : '×'}
              </span>
              <span><b>{row.label}</b><small>{row.detail}</small></span>
            </div>
          ))}
        </div>
      </section>

      {error && <InlineError message={error} onRetry={onRetry} />}
      <div className="fusion-actions">
        <span>{plan?.status === 'candidate'
          ? '候选已经生成；确认前当前知识包保持不变'
          : unresolved.length ? `还需确定 ${unresolved.length} 个元素的去向` : '将从只读来源重新编译并执行全部质量检查'}</span>
        {plan?.status === 'candidate' ? (
          <button className="btn" onClick={() => void actions.refreshSnapshot()}>刷新整体变更</button>
        ) : plan?.status === 'failed' ? (
          <button className="btn" onClick={onNew}>调整后重新建立</button>
        ) : (
          <button className="btn btn-p" disabled={Boolean(busy) || unresolved.length > 0 || plan?.status !== 'ready'} onClick={onCompile}>
            {busy === 'compile' ? '正在检查…' : '生成融合候选'}
          </button>
        )}
      </div>
    </div>
  );
}

export function DecisionControl({ entry, entries, sourceMap, mutable, busy, onResolve }) {
  const currentAction = ['preserve', 'canonicalize', 'exclude'].includes(entry.decision) ? entry.decision : '';
  const [action, setAction] = useState(currentAction);
  const [target, setTarget] = useState(entry.targetSourceId && entry.targetKey
    ? `${entry.targetSourceId}\0${entry.targetKey}` : '');
  const [reason, setReason] = useState(entry.reason || '');
  useEffect(() => {
    setAction(currentAction);
    setTarget(entry.targetSourceId && entry.targetKey ? `${entry.targetSourceId}\0${entry.targetKey}` : '');
    setReason(entry.reason || '');
  }, [entry.decision, entry.targetSourceId, entry.targetKey, entry.reason]);

  if (!entry.included) return <span className="fusion-decision muted">本次范围不纳入</span>;
  if (!mutable) return <span className="fusion-decision"><Icon name="check" size={13} />{decisionLabel(entry.decision)}</span>;
  const targets = entries.filter((candidate) => candidate.included
    && candidate.kind === entry.kind && candidate.sourceId !== entry.sourceId
    && !['exclude', 'canonicalize'].includes(candidate.decision));
  const choose = (event) => {
    const next = event.target.value;
    setAction(next);
    if (next === 'preserve') void onResolve([entry], 'preserve');
  };
  const save = () => {
    if (action === 'canonicalize') {
      const [targetSourceId, targetKey] = target.split('\0');
      void onResolve([entry], action, { targetSourceId, targetKey, reason: reason.trim() });
    } else if (action === 'exclude') {
      void onResolve([entry], action, { reason: reason.trim() });
    }
  };
  return (
    <div className="fusion-decision-control">
      <select className="input" aria-label={`${entry.sourceKey}的去向`} value={action} disabled={busy} onChange={choose}>
        <option value="">选择去向</option>
        <option value="preserve">独立保留</option>
        <option value="canonicalize">归并为同一项</option>
        <option value="exclude">不纳入结果</option>
      </select>
      {action === 'canonicalize' && <div className="fusion-decision-detail">
        <select className="input" aria-label="选择归并目标" value={target} disabled={busy} onChange={(event) => setTarget(event.target.value)}>
          <option value="">选择另一来源中的同类项</option>
          {targets.map((candidate) => <option key={`${candidate.sourceId}:${candidate.sourceKey}`} value={`${candidate.sourceId}\0${candidate.sourceKey}`}>
            {sourceLabel(sourceMap.get(candidate.sourceId))} · {candidate.sourceKey}
          </option>)}
        </select>
        <input className="input" aria-label="归并依据" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="填写确认等价的业务依据" />
        <button className="btn btn-sm" disabled={busy || !target || reason.trim().length < 2} onClick={save}>保存归并</button>
      </div>}
      {action === 'exclude' && <div className="fusion-decision-detail">
        <input className="input" aria-label="排除原因" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="填写不纳入本次结果的原因" />
        <button className="btn btn-sm" disabled={busy || reason.trim().length < 2} onClick={save}>确认排除</button>
      </div>}
    </div>
  );
}

function decisionLabel(decision) {
  if (decision === 'canonicalize') return '已归并为同一项';
  if (decision === 'exclude') return '不纳入结果';
  if (decision === 'exclude_scope') return '本次范围不纳入';
  return '独立保留';
}

function relationLabel(relation) {
  return ({ equivalent: '业务等价', broader: '范围更宽', narrower: '范围更窄', overlap: '部分重叠' })[relation] || '存在关系';
}

function FusionLoading() {
  return <div className="fusion-loading" aria-busy="true"><span className="dot-pulse" />正在读取来源包和融合方案…</div>;
}

function FusionError({ message, onRetry }) {
  return <div className="fusion-empty"><Icon name="alert" size={18} /><b>融合信息暂时无法读取</b><span>{message}</span><button className="btn" onClick={onRetry}>重新加载</button></div>;
}

function InlineError({ message, onRetry }) {
  return <div className="fusion-error" role="alert"><Icon name="alert" size={15} /><span>{message}</span>{onRetry && <button className="btn btn-sm" onClick={onRetry}>刷新</button>}</div>;
}
