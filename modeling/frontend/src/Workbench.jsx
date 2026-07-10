import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getProject, uploadDocument, deleteDocument, runAnalyze } from './api.js';
import { InterviewPanel, GeneratePanel, ReviewPanel, DomainQuestionsBlock } from './M2Panels.jsx';
import { TestcasesPanel, EvaluatePanel } from './M3Panels.jsx';
import { OptimizePanel } from './M4Panel.jsx';
import PackageChat from './PackageChat.jsx';
import Tour, { WORKBENCH_STEPS } from './Tour.jsx';
import { BusyButton } from './ui.jsx';

// 建模工作台三栏：左 workflow 时间线 · 中 当前节点交互区 · 右 上下文面板（包完善时切换为修缮对话）。
// 「领域分析」= 原初步分析 + 领域问题候选（原胜任力问题，分析完成后自动生成）。
const TRUNK = [
  ['project_setup', '项目创建'], ['ingest', '资料上传与解析'], ['analyze', '领域分析'],
  ['interview', '知识萃取访谈'], ['generate', '知识包生成'],
  ['review', '知识包完善'], ['testcases', '评测用例录入'], ['evaluate', '评测运行'], ['optimize', '优化建议与应用'],
];
// stage 序含历史节点 competency（老项目 stage 可能停在该值）
const STAGE_ORDER = ['project_setup', 'ingest', 'analyze', 'interview', 'competency', 'generate', 'review', 'testcases', 'evaluate', 'optimize'];
const STAGE_INDEX = Object.fromEntries(STAGE_ORDER.map((k, i) => [k, i]));

const TITLE = {
  ingest: '资料上传与解析', analyze: '领域分析', interview: '知识萃取访谈',
  competency: '领域问题', generate: '知识包生成', review: '知识包完善',
  testcases: '评测用例录入', evaluate: '评测运行', optimize: '优化建议与应用',
  project_setup: '项目信息',
};

export default function Workbench({ projectId }) {
  const [data, setData] = useState(null); // { project, documents, nodes }
  const [err, setErr] = useState('');
  const [ctxTab, setCtxTab] = useState('dict');
  const [analyzing, setAnalyzing] = useState(false);
  const [focus, setFocus] = useState(null); // null=自动跟随 stage；点时间线可切换

  const load = useCallback(() =>
    getProject(projectId).then(setData).catch((e) => setErr(e.message)), [projectId]);
  useEffect(() => { load(); }, [load]);
  // 长任务（分析/出题/生成…）在服务端执行并落节点状态；只要有节点 running 就轮询，
  // 切走再回来、甚至回项目列表再进来，进行中/已完成状态都能接上
  const anyRunning = (data?.nodes ?? []).some((n) => n.status === 'running');
  useEffect(() => {
    if (!anyRunning) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [anyRunning, load]);

  // 数据未加载成功才整页报错；已有数据时（如重新分析失败）用可关闭的横幅提示，不吞掉页面
  if (err && !data) return <div className="empty">{err}</div>;
  if (!data) return <div className="empty">加载中…</div>;

  const { project, documents, nodes } = data;
  const stageIdx = STAGE_INDEX[project.stage] ?? 0;
  const nodeOf = (type) => nodes.find((n) => n.type === type);
  const analyzeNode = nodeOf('analyze');
  const interviewNode = nodeOf('interview');
  const competencyNode = nodeOf('competency');
  const generateNode = nodeOf('generate');
  const tables = documents.flatMap((d) => d.tables ?? []);
  const parsedDocs = documents.filter((d) => d.parseStatus === 'parsed');
  const staleTypes = nodes.filter((n) => n.status === 'stale').map((n) => n.type);

  const analyzeDone = analyzeNode && analyzeNode.status !== 'running';
  const interviewDone = interviewNode?.status === 'done';
  const generateDone = generateNode && ['done', 'stale'].includes(generateNode.status);
  const cqSelected = data.competency?.selected ?? 0;

  // 节点解锁：1~2 始终可进；3 需资料；4 需领域分析产出且领域问题选满 5 条；5 需访谈完成；6~9 需包已生成
  const unlocked = {
    project_setup: true, ingest: true,
    analyze: tables.length > 0 || parsedDocs.length > 0,
    interview: !!analyzeDone && cqSelected >= 5,
    generate: interviewDone && !!competencyNode,
    review: !!generateDone,
    testcases: !!generateDone,
    evaluate: !!generateDone,
    optimize: stageIdx >= STAGE_INDEX.evaluate,
  };
  const nodeStatus = {
    project_setup: 'done', ingest: parsedDocs.length ? 'done' : 'pending',
    analyze: analyzeNode?.status === 'running' ? 'running' : analyzeNode?.status === 'stale' ? 'stale' : analyzeDone ? 'done' : 'pending',
    interview: interviewNode?.status === 'running' ? 'running' : interviewNode?.status === 'stale' ? 'stale' : interviewDone ? 'done' : interviewNode ? 'active-run' : 'pending',
    generate: generateNode?.status === 'running' ? 'running' : generateNode?.status === 'stale' ? 'stale' : generateDone ? 'done' : 'pending',
    review: generateDone ? 'done' : 'pending',
    testcases: stageIdx >= STAGE_INDEX.testcases ? 'done' : 'pending',
    evaluate: stageIdx >= STAGE_INDEX.evaluate ? 'done' : 'pending',
    optimize: stageIdx >= STAGE_INDEX.optimize ? 'done' : 'pending',
  };
  // 自动焦点：最靠后的已解锁未完成节点（进入评测阶段后跟随 stage）
  const autoFocus = !unlocked.analyze ? 'ingest'
    : !analyzeDone || cqSelected < 5 ? (tables.length ? 'analyze' : 'ingest')
    : !interviewDone ? 'interview'
    : !generateDone ? 'generate'
    : project.stage === 'optimize' ? 'optimize'
    : project.stage === 'evaluate' ? 'evaluate'
    : project.stage === 'testcases' ? 'testcases'
    : 'review';
  const effFocus = focus && unlocked[focus] ? focus : autoFocus;

  const analyzeRunning = analyzing || analyzeNode?.status === 'running';
  const doAnalyze = async () => {
    setAnalyzing(true); setErr('');
    try {
      const req = runAnalyze(projectId);
      // 服务端一开跑就把节点置为 running，稍后刷新一次让时间线/标题栏同步显示「进行中」
      setTimeout(load, 800);
      await req; await load();
    }
    catch (e) { setErr(e.message); }
    finally { setAnalyzing(false); }
  };

  return (
    <div className="wb">
      <Tour scene="workbench" steps={WORKBENCH_STEPS} />
      {/* 左：workflow 时间线 */}
      <div className="wb-col wb-left">
        <div className="wb-h">建模流程</div>
        <div className="wb-b" data-tour="timeline">
          {TRUNK.map(([key, title], i) => {
            const canGo = unlocked[key];
            const st = nodeStatus[key];
            const active = key === effFocus;
            return (
              <div key={key} data-tour={`node-${key}`}
                className={`tl-node ${st === 'done' ? 'done' : ''} ${st === 'stale' ? 'stale' : ''} ${st === 'running' ? 'running' : ''} ${active ? 'active' : ''} ${canGo ? 'clickable' : ''}`}
                onClick={() => canGo && setFocus(key)}>
                <div className="th">
                  <span className="no">{st === 'done' ? '✓' : st === 'stale' ? '⟳' : st === 'running' ? <span className="spinner" /> : i + 1}</span>
                  <span className="tt" style={!canGo ? { color: 'var(--muted)' } : undefined}>{title}</span>
                  {st === 'running' && <span className="ts run-ts">进行中</span>}
                  {st === 'stale' && <span className="ts stale-ts">可能过时</span>}
                  {!canGo && st !== 'running' && <span className="ts">待解锁</span>}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* 中：当前节点交互区（按 effFocus 路由） */}
      <div className="wb-col wb-mid">
        <div className="wb-h">
          {TITLE[effFocus]}
          {effFocus === 'analyze' && (!analyzeDone || nodeStatus.analyze === 'stale') && tables.length > 0 && (
            <BusyButton busy={analyzeRunning} busyText="领域分析中" expect="通常 1~3 分钟，可切换页面"
              style={{ marginLeft: 'auto', padding: '5px 14px', fontSize: 12.5 }} onClick={doAnalyze}>
              ▶ {nodeStatus.analyze === 'stale' ? '重新领域分析' : '开始领域分析'}
            </BusyButton>
          )}
        </div>
        {err && (
          <div className="err-banner">
            {err}
            <button className="tc-del" title="关闭" onClick={() => setErr('')}>✕</button>
          </div>
        )}
        {staleTypes.length > 0 && (
          <div className="stale-banner">
            ⟳ 资料已变更，{staleTypes.map((t) => TITLE[t] ?? t).join('、')}的结果可能过时（历史评测与版本保留）。
            <button className="btn" style={{ marginLeft: 10, padding: '3px 12px', fontSize: 12 }}
              onClick={() => setFocus('analyze')}>去重新分析</button>
          </div>
        )}
        <div className="wb-b">
          {(effFocus === 'ingest' || effFocus === 'project_setup') && (
            <IngestView project={project} documents={documents} onChanged={load} />
          )}
          {effFocus === 'analyze' && (analyzeNode
            ? <AnalysisView projectId={projectId} analysis={analyzeNode.output} stale={nodeStatus.analyze === 'stale'}
                onReanalyze={doAnalyze} analyzing={analyzeRunning} onQuestionsChange={load}
                questionsGenerating={competencyNode?.status === 'running'}
                tableCount={tables.length} docCount={parsedDocs.length}
                onAdvance={() => { load(); setFocus('interview'); }} />
            : <IngestView project={project} documents={documents} onChanged={load} />)}
          {effFocus === 'interview' && (
            <InterviewPanel projectId={projectId} agentBusy={interviewNode?.status === 'running'}
              onAdvance={() => { load(); setFocus('generate'); }} />
          )}
          {effFocus === 'generate' && (
            <GeneratePanel projectId={projectId} node={generateNode} onRefresh={load}
              onAdvance={() => { load(); setFocus('review'); }} />
          )}
          {effFocus === 'review' && <ReviewPanel projectId={projectId} />}
          {effFocus === 'testcases' && (
            <TestcasesPanel projectId={projectId} onAdvance={() => { load(); setFocus('evaluate'); }} />
          )}
          {effFocus === 'evaluate' && <EvaluatePanel projectId={projectId} />}
          {effFocus === 'optimize' && (
            <OptimizePanel projectId={projectId} onGoEvaluate={() => { load(); setFocus('evaluate'); }} />
          )}
        </div>
      </div>

      {/* 右：上下文面板；包完善时切换为修缮对话 */}
      <div className="wb-col wb-right" data-tour="context">
        {effFocus === 'review' ? (
          <PackageChat projectId={projectId} />
        ) : (
          <>
            <div className="ctx-tabs">
              <button className={ctxTab === 'dict' ? 'on' : ''} onClick={() => setCtxTab('dict')}>数据字典（{tables.length}）</button>
              <button className={ctxTab === 'docs' ? 'on' : ''} onClick={() => setCtxTab('docs')}>文档（{parsedDocs.length}）</button>
            </div>
            <div className="wb-b">
              {ctxTab === 'dict' && (tables.length
                ? tables.map((t) => (
                  <div key={t.name} className="dict-table">
                    <div className="tn">▤ {t.name} <span className="tag tag-lang">{t.rowCount} 行</span></div>
                    <table className="dt">
                      <thead><tr><th>字段</th><th>类型</th><th>说明</th></tr></thead>
                      <tbody>
                        {t.fields.map((f) => (
                          <tr key={f.name}>
                            <td style={{ fontFamily: 'ui-monospace, monospace' }}>{f.name}{f.distinctRatio === 1 ? ' 🔑' : ''}</td>
                            <td>{f.inferredType}</td>
                            <td style={{ color: 'var(--ink2)' }}>{f.note || ''}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ))
                : <div className="empty">上传 Excel 数据表后，这里显示解析出的数据字典</div>)}
              {ctxTab === 'docs' && (parsedDocs.length
                ? parsedDocs.map((d) => (
                  <div key={d.id} className="doc-row">
                    <span>{d.kind === 'excel' ? '📊' : '📄'}</span>
                    <span className="dn">{d.filename}</span>
                    <span className="tag tag-ok">已解析</span>
                  </div>
                ))
                : <div className="empty">暂无文档</div>)}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ---- 节点 2：资料上传（配图说明 + 删除） ---- */

function IngestView({ project, documents, onChanged }) {
  const inputRef = useRef(null);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  const upload = async (files) => {
    if (!files?.length) return;
    setBusy(true); setMsg('');
    try {
      for (const f of files) await uploadDocument(project.id, f);
      setMsg(`已上传 ${files.length} 个文件`);
      onChanged();
    } catch (e) { setMsg(`上传失败：${e.message}`); }
    finally { setBusy(false); }
  };

  const remove = async (doc) => {
    if (!window.confirm(`删除「${doc.filename}」？\n\n删除后已完成的领域分析/访谈/生成结果会标记为「可能过时」，建议之后重新执行领域分析。已录入的评测用例与历史评测不受影响。`)) return;
    setBusy(true); setMsg('');
    try { await deleteDocument(project.id, doc.id); setMsg(`已删除 ${doc.filename}`); onChanged(); }
    catch (e) { setMsg(`删除失败：${e.message}`); }
    finally { setBusy(false); }
  };

  return (
    <div>
      {/* 配图说明：Excel 格式示例 */}
      <div className="ig-guide">
        <div className="ig-left">
          <div className="ig-title">📊 Excel 数据库表怎么准备？</div>
          <div className="ig-desc">
            每个 <b>sheet 是一张表</b>；<b>首行 = 字段名</b>；<b>次行可选中文注释</b>；之后是数据行。
          </div>
          <div className="ig-desc" style={{ marginTop: 6 }}>
            业务规则、口径、流程规范等文档（md / docx / pdf / txt）是建模的领域知识来源，建议一并上传。
          </div>
        </div>
        <div className="ig-sheet" aria-hidden="true">
          <div className="ig-cap">示例：structure-list.xlsx</div>
          <table>
            <tbody>
              <tr className="ig-hd"><td>ID</td><td>ParentPart</td><td>ChildPart</td><td>Num</td></tr>
              <tr className="ig-note"><td>行号</td><td>父项件号</td><td>子项件号</td><td>数量</td></tr>
              <tr><td>1</td><td>A001</td><td>B010</td><td>2</td></tr>
              <tr><td>2</td><td>A001</td><td>B011</td><td>4</td></tr>
            </tbody>
          </table>
          <div className="ig-tabs">
            <span className="on">STRUCTURE_LIST</span><span>CHANGE_ORDER</span><span>TargetItem</span><span>＋</span>
          </div>
          <div className="ig-legend">
            <span><i className="lg lg-hd" />首行=字段名</span>
            <span><i className="lg lg-note" />次行=注释（可选）</span>
            <span><i className="lg lg-tab" />sheet=表</span>
          </div>
        </div>
      </div>

      <div className={`dropzone ${drag ? 'drag' : ''}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); upload([...e.dataTransfer.files]); }}>
        {busy ? '处理中…' : '点击选择或拖拽文件到此处（支持 xlsx / md / docx / pdf / txt，可多选）'}
        <input ref={inputRef} type="file" multiple style={{ display: 'none' }}
          accept=".xlsx,.xls,.md,.markdown,.txt,.docx,.doc,.pdf"
          onChange={(e) => { upload([...e.target.files]); e.target.value = ''; }} />
      </div>
      {msg && <div style={{ fontSize: 12.5, color: 'var(--ink2)', marginTop: 10 }}>{msg}</div>}
      {documents.length > 0 && (
        <div style={{ marginTop: 16 }}>
          {documents.map((d) => (
            <div key={d.id} className="doc-row">
              <span>{d.kind === 'excel' ? '📊' : '📄'}</span>
              <span className="dn">{d.filename}</span>
              {d.parseStatus === 'parsed' && <span className="tag tag-ok">已解析{d.tables ? ` · ${d.tables.length} 表` : ''}</span>}
              {d.parseStatus === 'failed' && <span className="tag tag-err" title={d.parseError}>解析失败</span>}
              <button className="tc-del" title="删除该资料" disabled={busy} onClick={() => remove(d)}>✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---- 节点 3：领域分析（分析结果 + 领域问题候选） ---- */

const Q_SEV = { 错误: 'tag-err', 警告: 'tag-draft', 提示: 'tag-lang' };

// 资料质量与建包缺口：确定性检查发现 + LLM 缺口分析（附补齐方式）
function QualitySection({ q }) {
  const [open, setOpen] = useState((q.summary?.错误 ?? 0) + (q.summary?.警告 ?? 0) > 0);
  const total = (q.findings?.length ?? 0) + (q.gaps?.length ?? 0);
  return (
    <div className="an-sec">
      <h4>
        资料质量与建包缺口
        <button className="pcheck-sum" onClick={() => setOpen((v) => !v)}>
          <span className="tag tag-err">警告 {(q.summary?.错误 ?? 0) + (q.summary?.警告 ?? 0)}</span>
          <span className="tag tag-lang">提示 {q.summary?.提示 ?? 0}</span>
          <span className="tag tag-draft">缺口 {q.gaps?.length ?? 0}</span>
          <span style={{ color: 'var(--muted)' }}>{open ? '收起 ▾' : '展开 ▸'}</span>
        </button>
      </h4>
      {total === 0 && <div style={{ fontSize: 12.5, color: 'var(--good, #0e6b4a)' }}>✓ 未发现影响建包的资料问题</div>}
      {open && total > 0 && (
        <div className="pcheck-list" style={{ maxHeight: 320 }}>
          {(q.findings ?? []).map((f, i) => (
            <div key={`f${i}`} className="pcheck-item">
              <span className={`tag ${Q_SEV[f.severity] ?? 'tag-lang'}`}>{f.severity}</span>
              <span className="tag tag-lang">{f.category}</span>
              <span className="pcheck-target">{f.target}</span>
              <div className="pcheck-msg">{f.message}{f.suggestion ? <span style={{ color: 'var(--muted)' }}>（{f.suggestion}）</span> : null}</div>
            </div>
          ))}
          {(q.gaps ?? []).map((g, i) => (
            <div key={`g${i}`} className="pcheck-item" style={{ background: '#fdfaf2' }}>
              <span className="tag tag-draft">缺口</span>
              <span className="tag tag-lang">{g.kind}</span>
              <span className="pcheck-target">{g.target}</span>
              <div className="pcheck-msg">{g.message}<span style={{ color: 'var(--muted)' }}>（补齐：{g.how}）</span></div>
            </div>
          ))}
        </div>
      )}
      {(q.gaps?.length ?? 0) > 0 && (
        <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 6 }}>缺口已同步给建模访谈作为优先澄清点</div>
      )}
    </div>
  );
}

// 分析进行中的占位：说明正在发生什么 + 步骤药丸 + 结果骨架预览，避免整屏只有一行空态
function AnalyzeRunning({ tableCount, docCount }) {
  const steps = ['探查数据表结构与字段', '阅读业务规范文档', '归纳领域概览', '识别候选业务对象', '生成领域问题候选'];
  return (
    <div>
      <div className="an-run-head">
        <span className="spinner" />
        <div>
          <div className="an-run-t">领域分析进行中</div>
          <div className="an-run-d">
            正在阅读 {tableCount} 张数据表{docCount > 0 ? `与 ${docCount} 份文档` : ''}，归纳领域概览、识别候选业务对象并生成领域问题。
            通常需要 1~3 分钟；分析在服务端执行，切换页面或稍后回来都不会中断。
          </div>
        </div>
      </div>
      <div className="an-run-steps">
        {steps.map((s, i) => (
          <span key={s} className="an-run-step" style={{ animationDelay: `${i * 0.3}s` }}>
            <span className="no">{i + 1}</span>{s}
          </span>
        ))}
      </div>
      <div className="an-sec">
        <h4>领域概览</h4>
        <div className="skel-card"><div className="skel w80" /><div className="skel w95" /><div className="skel w60" /></div>
      </div>
      <div className="an-sec">
        <h4>候选业务对象</h4>
        <div className="an-objs">
          {[0, 1].map((i) => (
            <div key={i} className="skel-card"><div className="skel w40" /><div className="skel w90" /><div className="skel w70" /></div>
          ))}
        </div>
      </div>
      <div className="an-sec">
        <h4>领域问题</h4>
        <div className="skel-card"><div className="skel w90" /><div className="skel w85" /><div className="skel w75" /></div>
      </div>
    </div>
  );
}

function AnalysisView({ projectId, analysis, stale, onReanalyze, analyzing, onAdvance, onQuestionsChange, questionsGenerating, tableCount, docCount }) {
  const [selectedCount, setSelectedCount] = useState(null); // null=未知（加载中）
  if (!analysis) {
    if (analyzing) return <AnalyzeRunning tableCount={tableCount} docCount={docCount} />;
    return <div className="empty">（无分析结果）</div>;
  }
  const ready = (selectedCount ?? 0) >= 5;
  return (
    <div>
      {stale && <div className="lerr">⟳ 资料已变更，建议重新领域分析。</div>}
      <div className="an-sec">
        <h4>领域概览</h4>
        <div className="mdv an-ov">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{analysis.domainOverview || ''}</ReactMarkdown>
        </div>
      </div>
      {analysis.quality && <QualitySection q={analysis.quality} />}
      <div className="an-sec">
        <h4>候选业务对象（{analysis.candidateObjects?.length ?? 0}）</h4>
        <div className="an-objs">
          {(analysis.candidateObjects ?? []).map((o) => (
            <div key={o.name} className="an-obj">
              <div className="on">▤ {o.name}</div>
              <div className="od">{o.rationale}</div>
              {o.sourceTables?.length > 0 && (
                <div className="ot">
                  <span className="ot-lb">来源表</span>
                  {o.sourceTables.map((t) => <span key={t} className="ot-chip">{t}</span>)}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
      <div className="an-sec">
        <h4>领域问题<span className="h4-sub">建成后必须能回答的业务问题 · 勾选 ≥5 条</span></h4>
        <DomainQuestionsBlock projectId={projectId} generating={questionsGenerating}
          onCountChange={(n) => { setSelectedCount(n); onQuestionsChange?.(); }} />
      </div>
      <div style={{ display: 'flex', gap: 10, marginTop: 6, alignItems: 'center' }}>
        <BusyButton busy={analyzing} busyText="重新分析中" expect="通常 1~3 分钟，可切换页面"
          className="btn" onClick={onReanalyze}>重新分析</BusyButton>
        <button className="btn btn-p" disabled={!ready} onClick={onAdvance}
          title={ready ? '' : '勾选满 5 条领域问题后解锁'}>
          进入知识萃取访谈 →{ready ? '' : `（还差 ${5 - (selectedCount ?? 0)} 条）`}
        </button>
      </div>
    </div>
  );
}
