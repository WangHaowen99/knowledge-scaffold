import React, { useEffect, useRef, useState } from 'react';
import { allPages, api, requestId } from './api.js';
import {
  ACTIVE,
  DEFAULT_CONCURRENCY,
  MAX_CONCURRENCY,
  judgeMissingReason,
  modelOptionLabel,
  packageReference,
  sourceSummary,
} from './domain.js';
import { Alert, Badge, Button, Confirm, Empty, Field, Heading, Icon, Skeleton, useAction } from './ui.jsx';
import { CaseEditor } from './CaseEditor.jsx';
import { GraphScopeStatus, MappingPreview, SourceEditor } from './SourceEditor.jsx';

const steps = [
  ['知识包版本', '固定本轮版本'],
  ['选择数据源', '复用已存配置'],
  ['选择评测集', '可编辑后执行'],
  ['准备与确认', '预览映射并启动'],
];
export function Wizard({ sets, sources, runs, session, refreshDefinitions, navigate, from, registerDirty }) {
  const query = new URLSearchParams(location.search);
  const [step, setStep] = useState(0),
    [packages, setPackages] = useState([]),
    [versions, setVersions] = useState([]);
  const [packageId, setPackageId] = useState(query.get('hub_package_id') || ''),
    [releaseId, setReleaseId] = useState(query.get('release_id') || '');
  const [sourceId, setSourceId] = useState(''),
    [setId, setSetId] = useState(''),
    [evalSet, setEvalSet] = useState(null);
  const [prep, setPrep] = useState(null),
    [loading, setLoading] = useState(true),
    [versionLoading, setVersionLoading] = useState(false),
    [caseLoading, setCaseLoading] = useState(false);
  const [sourceEditor, setSourceEditor] = useState(null),
    [sourceDirty, setSourceDirty] = useState(false),
    [caseDirty, setCaseDirty] = useState(false),
    [changeSet, setChangeSet] = useState(null),
    [graphCheck, setGraphCheck] = useState(null),
    [replaceGraph, setReplaceGraph] = useState(null),
    [preview, setPreview] = useState(null);
  const [concurrency, setConcurrency] = useState(DEFAULT_CONCURRENCY),
    [modelId, setModelId] = useState(''),
    [models, setModels] = useState(null),
    [judgeMissing, setJudgeMissing] = useState(false);
  const { busy, error, setError, act } = useAction(),
    generation = useRef(0),
    sourceGeneration = useRef(0),
    pendingRun = useRef(null),
    init = useRef(false),
    pageRef = useRef(null);
  const release = versions.find((r) => r.release_id === releaseId),
    source = sources.find((s) => s.id === sourceId);
  const currentRun = runs.find((r) => ACTIVE.has(r.status));
  const registerDirtyRef = useRef(registerDirty);
  registerDirtyRef.current = registerDirty;
  useEffect(() => {
    registerDirtyRef.current(caseDirty || sourceDirty);
    return () => registerDirtyRef.current(false);
  }, [caseDirty, sourceDirty]);
  useEffect(() => {
    pageRef.current?.scrollIntoView({ block: 'start' });
  }, [!!sourceEditor]);
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const items = await allPages('/packages');
        if (!live) return;
        setPackages(items);
        if (from) {
          const run = await api(`/runs/${encodeURIComponent(from)}`);
          if (!live) return;
          setPackageId(run.package_ref.hub_package_id);
          setReleaseId(run.package_ref.release_id);
          setSourceId(
            sources.some((s) => s.id === run.summary.inputs.data_source_id)
              ? run.summary.inputs.data_source_id
              : '',
          );
          setSetId(sets.some((s) => s.id === run.eval_set_id) ? run.eval_set_id : '');
          setConcurrency(run.concurrency || DEFAULT_CONCURRENCY);
          setModelId(run.model_profile_id || '');
        } else {
          setSetId(sets[0]?.id || '');
        }
        init.current = true;
      } catch (e) {
        if (live) setError(e.message);
      } finally {
        if (live) setLoading(false);
      }
    })();
    return () => {
      live = false;
    };
  }, []);
  useEffect(() => {
    if (!loading && init.current && !setId && !evalSet && sets.length) setSetId(sets[0].id);
  }, [sets, loading]);
  useEffect(() => {
    const sequence = ++generation.current;
    setPrep(null);
    setVersions([]);
    if (!packageId) return;
    setVersionLoading(true);
    (async () => {
      try {
        const values = await allPages(`/packages/${encodeURIComponent(packageId)}/releases`);
        if (releaseId && !values.some((r) => r.release_id === releaseId))
          values.unshift(
            await api(`/packages/${encodeURIComponent(packageId)}/releases/${encodeURIComponent(releaseId)}`),
          );
        const selected = values.find((r) => r.release_id === releaseId);
        if (
          !from &&
          query.get('release_id') === releaseId &&
          query.get('package_digest') &&
          selected?.package_digest !== query.get('package_digest')
        )
          throw new Error('指定版本的摘要与 Hub 不一致，请重新选择版本');
        if (sequence === generation.current) setVersions(values);
      } catch (e) {
        if (sequence === generation.current) {
          setError(e.message);
          setReleaseId('');
        }
      } finally {
        if (sequence === generation.current) setVersionLoading(false);
      }
    })();
    return () => {
      generation.current += 1;
    };
  }, [packageId]);
  useEffect(() => {
    setPrep(null);
    setPreview(null);
    setGraphCheck(null);
    setReplaceGraph(null);
    pendingRun.current = null;
  }, [releaseId, sourceId, source?.revision]);
  useEffect(() => {
    pendingRun.current = null;
  }, [concurrency, modelId]);
  useEffect(() => {
    if (step !== 3 || models) return undefined;
    let live = true;
    api('/consumer-models')
      .then((value) => {
        if (!live) return;
        setModels(value);
        // A model copied from an earlier run may have been disabled since.
        if (value.available) setModelId((id) => (value.items.some((item) => item.id === id) ? id : ''));
      })
      .catch(() => live && setModels({ available: false, items: [] }));
    return () => {
      live = false;
    };
  }, [step]);
  useEffect(() => {
    let live = true;
    if (!setId) {
      setCaseLoading(false);
      return;
    }
    setEvalSet(null);
    setCaseLoading(true);
    api(`/eval-sets/${setId}`)
      .then((r) => {
        if (live) {
          setEvalSet(r);
          setCaseDirty(false);
        }
      })
      .catch((e) => {
        if (live) setError(e.message);
      })
      .finally(() => {
        if (live) setCaseLoading(false);
      });
    return () => {
      live = false;
    };
  }, [setId]);
  const ref = release
    ? {
        hub_package_id: packageId,
        release_id: releaseId,
        package_digest: release.package_digest,
        manifest_id: release.manifest_id,
      }
    : null;
  async function editSource(id = '') {
    const item = id ? await api(`/data-sources/${id}`) : null;
    setSourceEditor({ record: item });
  }
  async function loadPreview() {
    const version = sourceGeneration.current;
    if (sourceId && ref) {
      const value = await api('/data-source-previews', {
        data_source_id: sourceId,
        package_ref: packageReference(ref),
      });
      if (version === sourceGeneration.current) setPreview(value);
    }
  }
  async function prepare() {
    if (caseDirty) throw new Error('请先保存当前评测集修改');
    const input = {
      package_ref: {
        hub_package_id: packageId,
        release_id: releaseId,
        package_digest: release.package_digest,
      },
      data_source_id: sourceId,
    };
    const key = requestId('prepare', JSON.stringify({ ...input, revision: source?.revision }));
    const result = await api('/package-preparations', { ...input, request_id: key.id });
    setPrep(result);
    key.done();
  }
  async function checkGraph(preparationId = '') {
    const check = await api('/graph-import-checks', {
      data_source_id: sourceId,
      package_ref: packageReference(ref),
      preparation_id: preparationId,
    });
    setGraphCheck(check);
    return check;
  }
  async function createRun(check, replaceExisting = false) {
    if (!prep || !evalSet || caseDirty) throw new Error('请确认准备结果和已保存的评测集');
    if (!pendingRun.current) {
      const input = {
        preparation_id: prep.id,
        eval_set_id: evalSet.id,
        eval_set_revision: evalSet.revision,
        concurrency,
        ...(modelId ? { model_profile_id: modelId } : {}),
        ...(check ? { graph_check_id: check.id, replace_existing_graph: replaceExisting } : {}),
      };
      const key = requestId('run', JSON.stringify({ ...input, case_revision: evalSet.revision }));
      pendingRun.current = { input: { ...input, request_id: key.id }, key };
    }
    const pending = pendingRun.current;
    let result;
    try {
      result = await api('/runs', pending.input);
    } catch (e) {
      if (e.status >= 400 && e.status < 500) {
        pending.key.done();
        pendingRun.current = null;
      }
      throw e;
    }
    pending.key.done();
    pendingRun.current = null;
    registerDirty(false);
    navigate(`/runs/${result.id}`);
  }
  async function start() {
    if (pendingRun.current) return createRun();
    if (source?.provider === 'neo4j' && source.import_data) {
      const check = await checkGraph(prep.id);
      if (check.boundary_relationship_count) throw new Error('此范围存在跨范围关系，请处理关系或调整隔离范围后再试');
      if (check.node_count) {
        setReplaceGraph(check);
        return;
      }
      await createRun(check);
    } else await createRun();
  }
  function chooseSet(id) {
    if (caseDirty) setChangeSet(id);
    else {
      setEvalSet(null);
      setSetId(id);
    }
  }
  const selectSource = (id) => {
    sourceGeneration.current += 1;
    setSourceId(id);
    setPreview(null);
  };
  async function next() {
    if (step === 0 && !release) throw new Error('请选择知识包与发布版本');
    if (step === 2 && (!evalSet?.id || caseDirty)) throw new Error('请选择并保存评测集后继续');
    setStep(step + 1);
  }
  if (sourceEditor)
    return (
      <div className="wizard-page" ref={pageRef}>
        <SourceEditor
          key={sourceEditor.record?.id || 'new'}
          record={sourceEditor.record}
          packageRef={ref}
          parentLabel="新建评测"
          onDirty={setSourceDirty}
          onClose={() => setSourceEditor(null)}
          onSaved={(item, close) => {
            refreshDefinitions();
            selectSource(item.id);
            if (close) setSourceEditor(null);
          }}
        />
      </div>
    );
  return (
    <div className="wizard-page" ref={pageRef}>
      <Heading
        title="新建评测"
        breadcrumb={
          <>
            <button className="text-link" onClick={() => navigate('/')}>
              任务中心
            </button>
            <span>/</span>
            <span>新建评测</span>
          </>
        }
        actions={<Button onClick={() => navigate('/')}>退出向导</Button>}
      >
        沿用已保存的配置，几步完成一次评测。
      </Heading>
      <div className="wizard-steps">
        {steps.map(([title, subtitle], i) => (
          <button
            type="button"
            key={title}
            className={`step ${i < step ? 'done' : ''} ${i === step ? 'current' : ''}`}
            disabled={i > step || busy}
            aria-current={i === step ? 'step' : undefined}
            onClick={() => setStep(i)}
          >
            <span className="step-node">{i < step ? <Icon name="check" /> : i + 1}</span>
            <span>
              {title}
              <small>{subtitle}</small>
            </span>
          </button>
        ))}
      </div>
      {error && <Alert tone="red">{error}</Alert>}
      {currentRun && (
        <Alert tone="amber" title="当前有一个活动评测">
          可先配置本轮输入，已有任务结束或停止后再启动。
          <button className="text-link" onClick={() => navigate(`/runs/${currentRun.id}`)}>
            查看当前任务
          </button>
        </Alert>
      )}
      {loading ? (
        <Skeleton />
      ) : (
        <div className="wizard-grid">
          <div className="wizard-main">
            {step === 0 && (
              <section className="panel">
                <div className="section-title">
                  <h2>选择知识包与固定版本</h2>
                  <Badge tone="blue">来自 Hub</Badge>
                </div>
                <p className="help-text">评测始终使用固定发布版本，后续发布新版本不会改变本轮输入。</p>
                <div className="form-grid">
                  <Field label="知识包 *">
                    <select
                      value={packageId}
                      onChange={(e) => {
                        setReleaseId('');
                        setPackageId(e.target.value);
                      }}
                    >
                      <option value="">选择知识包</option>
                      {packages.map((p) => (
                        <option key={p.hub_package_id} value={p.hub_package_id}>
                          {p.manifest_id || p.name || p.hub_package_id}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="发布版本 *">
                    <select
                      value={releaseId}
                      disabled={!packageId || versionLoading}
                      onChange={(e) => setReleaseId(e.target.value)}
                    >
                      <option value="">{versionLoading ? '正在加载版本…' : '选择固定版本'}</option>
                      {versions.map((r) => (
                        <option key={r.release_id} value={r.release_id}>
                          {r.version} · {r.source_service || 'Hub'}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
                {!packages.length && (
                  <Empty title="暂无可评测的知识包" icon="box">
                    请先在 Hub 发布知识包版本，再返回此处刷新。
                  </Empty>
                )}
                {release && (
                  <div className="version-receipt">
                    <div className="section-title">
                      <strong>{release.version}</strong>
                      <Badge tone="green">已发布版本</Badge>
                    </div>
                    <dl>
                      <dt>知识包</dt>
                      <dd>
                        {release.manifest_id ||
                          packages.find((p) => p.hub_package_id === packageId)?.manifest_id}
                      </dd>
                      <dt>版本标识</dt>
                      <dd>{release.release_id}</dd>
                      <dt>内容摘要</dt>
                      <dd>
                        <code>{release.package_digest}</code>
                      </dd>
                    </dl>
                    <p className="help-text">更换版本会使原准备结果失效，已编辑的评测集保留。</p>
                  </div>
                )}
              </section>
            )}
            {source?.provider === 'neo4j' && source.import_data && [1, 3].includes(step) && (
              <section className="panel">
                <div className="section-title">
                  <h3>图导入前检查</h3>
                  <Button busy={busy} onClick={() => act(() => checkGraph(prep?.id || ''))}>
                    检查旧图数据
                  </Button>
                </div>
                <p className="help-text">按 Workspace 与当前知识包的 Ontology 值同时匹配。保存配置不会删除图数据。</p>
                <GraphScopeStatus check={graphCheck} />
              </section>
            )}
            {step === 1 && (
              <section className="panel">
                <div className="section-title">
                  <h2>选择本轮使用的数据源</h2>
                  <Button icon="plus" variant="text" onClick={() => act(() => editSource())}>
                    新建数据源
                  </Button>
                </div>
                <p className="help-text">选择已有配置，或新建后重复使用。</p>
                {sources.map((s) => (
                  <div className={`source-option ${sourceId === s.id ? 'selected' : ''}`} key={s.id}>
                    <button
                      type="button"
                      className="source-choice"
                      aria-pressed={sourceId === s.id}
                      onClick={() => selectSource(s.id)}
                    >
                      <span className="source-icon">
                        <Icon name={s.mode === 'connection' ? 'link' : 'db'} />
                      </span>
                      <span>
                        <strong>{s.name}</strong>
                        <small>{sourceSummary(s)}</small>
                      </span>
                      <span className="radio-mark" />
                    </button>
                    <div className="source-option-actions">
                      <Button variant="text" onClick={() => act(() => editSource(s.id))}>
                        编辑配置
                      </Button>
                    </div>
                  </div>
                ))}
                <div className={`source-option ${!sourceId ? 'selected' : ''}`}>
                  <button
                    type="button"
                    className="source-choice"
                    aria-pressed={!sourceId}
                    onClick={() => selectSource('')}
                  >
                    <span className="source-icon">
                      <Icon name="box" />
                    </span>
                    <span>
                      <strong>不使用数据源</strong>
                      <small>仅验证知识包结构与语义，不查询数据。</small>
                    </span>
                    <span className="radio-mark" />
                  </button>
                </div>
                {sourceId && (source?.mode === 'dataset' || source?.import_data) && (
                  <div className="preview-action">
                    <Button busy={busy} onClick={() => act(loadPreview)}>
                      预览{source?.provider === 'neo4j' ? '图导入' : '数据映射'}
                    </Button>
                    <MappingPreview preview={preview} />
                  </div>
                )}
              </section>
            )}
            <section hidden={step !== 2} className="panel">
              <div className="section-title">
                <h2>选择评测集</h2>
                <Button
                  variant="text"
                  onClick={() => {
                    if (caseDirty) setChangeSet('__new__');
                    else {
                      setSetId('');
                      setEvalSet({ name: '', cases: [{ question: '', reference: '' }] });
                    }
                  }}
                >
                  新建评测集
                </Button>
              </div>
              <Field label="已保存的评测集">
                <select value={setId} onChange={(e) => chooseSet(e.target.value)}>
                  <option value="">选择评测集</option>
                  {sets.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} · {s.case_count ?? s.cases?.length ?? 0} 条
                    </option>
                  ))}
                </select>
              </Field>
              {caseLoading ? (
                <Skeleton />
              ) : evalSet ? (
                <CaseEditor
                  key={evalSet.id || 'new'}
                  embedded
                  record={evalSet}
                  onDirty={setCaseDirty}
                  onSaved={(item) => {
                    setEvalSet(item);
                    setCaseDirty(false);
                    if (setId !== item.id) setSetId(item.id);
                    refreshDefinitions();
                  }}
                />
              ) : (
                <Empty
                  title="请选择或创建评测集"
                  icon="set"
                  action={
                    <Button
                      icon="plus"
                      onClick={() => setEvalSet({ name: '', cases: [{ question: '', reference: '' }] })}
                    >
                      新建评测集
                    </Button>
                  }
                >
                  可逐条填写问题与参考答案，或导入 JSON。
                </Empty>
              )}
            </section>
            {step === 3 && (
              <section className="panel">
                <div className="section-title">
                  <h2>{prep ? '准备完成，请确认后启动' : '准备知识包与映射'}</h2>
                  {prep && <Badge tone="green">准备就绪</Badge>}
                </div>
                <div className="preparation-flow">
                  <span>固定 Hub 版本</span>
                  <Icon name="arrow" />
                  <span>冻结数据源配置</span>
                  <Icon name="arrow" />
                  <span>生成本轮映射</span>
                </div>
                {!prep ? (
                  <Empty
                    title="确认输入，准备本轮评测"
                    icon="shield"
                    action={
                      <Button variant="primary" busy={busy} onClick={() => act(prepare)}>
                        准备知识包与映射
                      </Button>
                    }
                  >
                    读取固定版本并生成字段映射，准备成功后再启动任务。
                  </Empty>
                ) : (
                  <>
                    <MappingPreview preview={prep.preview} />
                    {prep.changed && (
                      <Alert title="本轮使用派生评测快照">
                        映射变化已保存到内部快照，正式发布 Hub 新版本需在结果页单独确认。
                      </Alert>
                    )}
                    {!prep.mapping && (
                      <Alert title="沿用原 Hub 版本">当前数据源不需要新的映射，评测直接使用固定版本。</Alert>
                    )}
                    <ul className="check-list">
                      <li>
                        <Icon name="check" />
                        Hub 固定版本已确认
                      </li>
                      <li>
                        <Icon name="check" />
                        评测集已保存：{evalSet?.cases.length} 条
                      </li>
                      <li>
                        <Icon name={session.judge?.configured ? 'check' : 'info'} />
                        {session.judge?.configured
                          ? `评测判分模型已配置：${session.judge.model}（${session.judge.source === 'personal' ? '我的配置' : '默认配置'}）`
                          : session.judge?.source === 'disabled'
                            ? '管理员已停用默认判分模型，请先在评测判分模型配置中填写你自己的判分模型'
                            : '评测判分模型尚未配置，请先前往评测判分模型配置'}
                      </li>
                    </ul>
                  </>
                )}
              </section>
            )}
            {step === 3 && (
              <section className="panel">
                <div className="section-title">
                  <h2>执行设置</h2>
                </div>
                <div className="form-grid">
                  <Field label="并发数" hint={`同时发送给 consumer 的问题数，默认 ${DEFAULT_CONCURRENCY}`}>
                    <select value={concurrency} onChange={(e) => setConcurrency(Number(e.target.value))}>
                      {Array.from({ length: MAX_CONCURRENCY }, (_, i) => i + 1).map((n) => (
                        <option key={n} value={n}>
                          {n}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="问答模型" hint="可选 consumer 全局模型池中已启用的模型">
                    <select value={modelId} disabled={!models} onChange={(e) => setModelId(e.target.value)}>
                      <option value="">consumer 默认模型</option>
                      {models?.items.map((item) => (
                        <option key={item.id} value={item.id}>
                          {modelOptionLabel(item)}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
                {models && !models.available && (
                  <Alert tone="amber">未能读取 consumer 模型池，本轮将使用 consumer 默认模型。</Alert>
                )}
                {source?.mode === 'connection' && (
                  <p className="help-text">
                    外部连接数据源在同名的 consumer 工作空间中执行（Neo4j 取 Workspace 对应值，NL2SQL 取评测工作空间 ID，空间名称与 ID 一致）：不存在时以你的账号创建；已存在时仅复用你独有、未共享的空间，并在其中导入、激活本轮知识包版本。
                  </p>
                )}
              </section>
            )}
          </div>
          <aside className="panel summary-panel">
            <h3>本轮评测</h3>
            <dl>
              <dt>知识包</dt>
              <dd>
                {release?.manifest_id ||
                  packages.find((p) => p.hub_package_id === packageId)?.manifest_id ||
                  '尚未选择'}
              </dd>
              <dt>固定版本</dt>
              <dd>{release?.version || '尚未选择'}</dd>
              <dt>数据源</dt>
              <dd>{source?.name || '不使用数据源'}</dd>
              <dt>评测集</dt>
              <dd>{evalSet?.name || sets.find((s) => s.id === setId)?.name || '尚未选择'}</dd>
              <dt>并发数</dt>
              <dd>{concurrency}</dd>
              <dt>问答模型</dt>
              <dd>
                {modelId
                  ? modelOptionLabel(models?.items.find((item) => item.id === modelId) || { id: modelId })
                  : 'consumer 默认模型'}
              </dd>
            </dl>
            <hr />
            <h3>判分方式</h3>
            <p>
              有参考答案：模型自动判分
              <br />
              无参考答案：等待人工判定
            </p>
            {evalSet && (
              <>
                <hr />
                <p>
                  有参考答案{' '}
                  <b className="float-right">{evalSet.cases.filter((c) => c.reference?.trim()).length} 条</b>
                  <br />
                  无参考答案{' '}
                  <b className="float-right">{evalSet.cases.filter((c) => !c.reference?.trim()).length} 条</b>
                </p>
              </>
            )}
            <hr />
            <p className="help-text">创建成功后进入执行详情。离开页面后，可从任务中心找回本轮任务。</p>
          </aside>
        </div>
      )}
      <footer className="wizard-footer">
        <span>{step === 3 ? '任务创建成功后自动进入执行详情' : '已保存的配置可在后续评测中重复使用'}</span>
        <div className="actions">
          <Button disabled={busy || !step} onClick={() => setStep(step - 1)}>
            上一步
          </Button>
          {step < 3 ? (
            <Button
              variant="primary"
              icon="arrow"
              busy={busy}
              disabled={
                loading ||
                versionLoading ||
                caseLoading ||
                (step === 0 && !release) ||
                (step === 2 && (!evalSet?.id || caseDirty))
              }
              onClick={() => act(next)}
            >
              下一步：{steps[step + 1][0]}
            </Button>
          ) : (
            <Button
              variant="primary"
              busy={busy}
              disabled={!prep || !evalSet?.id || !!currentRun}
              onClick={() => (session.judge?.configured ? act(start) : setJudgeMissing(true))}
            >
              开始评测 · {evalSet?.cases.length || 0} 条
            </Button>
          )}
        </div>
      </footer>
      {judgeMissing && (
        <Confirm
          title="评测判分模型未配置"
          label="前往配置"
          onClose={() => setJudgeMissing(false)}
          onConfirm={() => navigate('/settings')}
        >
          <p>{judgeMissingReason(session.judge)}</p>
          <p>前往配置会离开当前向导，配置完成后需重新发起评测。</p>
        </Confirm>
      )}
      {changeSet !== null && (
        <Confirm
          title="放弃未保存的用例修改？"
          label="切换评测集"
          onClose={() => setChangeSet(null)}
          onConfirm={async () => {
            setCaseDirty(false);
            registerDirty(false);
            setSetId(changeSet === '__new__' ? '' : changeSet);
            if (changeSet === '__new__') setEvalSet({ name: '', cases: [{ question: '', reference: '' }] });
          }}
        >
          <p>当前用例修改尚未保存，切换后将丢失这些修改。</p>
        </Confirm>
      )}
      {replaceGraph && (
        <Confirm
          title="删除旧图数据并导入新数据？"
          label="确认删除并开始评测"
          danger
          onClose={() => setReplaceGraph(null)}
          onConfirm={() => createRun(replaceGraph, true)}
        >
          <p>
            即将替换同时满足 {replaceGraph.scope.workspace_property} = {replaceGraph.scope.workspace_value}、
            {replaceGraph.scope.ontology_property} = {replaceGraph.scope.ontology_value} 的旧图数据。
          </p>
          <p>
            删除 {replaceGraph.node_count} 个节点及 {replaceGraph.relationship_count} 条范围内关系后，
            导入当前选择的数据表。确认后无法恢复旧数据；取消后保留原数据。
          </p>
        </Confirm>
      )}
    </div>
  );
}
