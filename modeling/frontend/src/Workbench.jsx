import { useCallback, useEffect, useRef, useState } from 'react';
import { getProject, uploadDocument, runAnalyze } from './api.js';
import { InterviewPanel, CompetencyPanel, GeneratePanel, ReviewPanel } from './M2Panels.jsx';
import { TestcasesPanel, EvaluatePanel } from './M3Panels.jsx';
import { OptimizePanel } from './M4Panel.jsx';

// 建模工作台三栏：左 workflow 时间线 · 中 当前节点交互区 · 右 上下文面板。
// M1 覆盖节点 1~3；后续节点在时间线上预留灰态。
const TRUNK = [
  ['project_setup', '项目创建'], ['ingest', '资料上传与解析'], ['analyze', '初步分析'],
  ['interview', '建模访谈'], ['competency', '胜任力问题'], ['generate', '知识包生成'],
  ['review', '包评审'], ['testcases', '评测用例录入'], ['evaluate', '评测运行'], ['optimize', '优化建议与应用'],
];
const STAGE_INDEX = Object.fromEntries(TRUNK.map(([k], i) => [k, i]));

export default function Workbench({ projectId }) {
  const [data, setData] = useState(null); // { project, documents, nodes }
  const [err, setErr] = useState('');
  const [ctxTab, setCtxTab] = useState('dict');
  const [analyzing, setAnalyzing] = useState(false);
  const [focus, setFocus] = useState(null); // null=自动跟随 stage；点时间线可切换

  const load = useCallback(() =>
    getProject(projectId).then(setData).catch((e) => setErr(e.message)), [projectId]);
  useEffect(() => { load(); }, [load]);

  if (err) return <div className="empty">{err}</div>;
  if (!data) return <div className="empty">加载中…</div>;

  const { project, documents, nodes } = data;
  const stageIdx = STAGE_INDEX[project.stage] ?? 0;
  const analyzeNode = nodes.find((n) => n.type === 'analyze' && n.status === 'done');
  const interviewNode = nodes.find((n) => n.type === 'interview');
  const competencyNode = nodes.find((n) => n.type === 'competency');
  const generateNode = nodes.find((n) => n.type === 'generate' && n.status === 'done');
  const tables = documents.flatMap((d) => d.tables ?? []);
  const parsedDocs = documents.filter((d) => d.parseStatus === 'parsed');

  // 节点解锁判定：1~2 始终可进；3 需资料；4 需分析完成；5 需访谈节点存在；6 需胜任力节点；7~9 需包已生成
  const unlocked = {
    project_setup: true, ingest: true,
    analyze: tables.length > 0 || parsedDocs.length > 0,
    interview: !!analyzeNode,
    competency: !!interviewNode && interviewNode.status === 'done',
    generate: !!competencyNode,
    review: !!generateNode,
    testcases: !!generateNode,
    evaluate: !!generateNode,
    optimize: stageIdx >= STAGE_INDEX.evaluate,
  };
  const nodeStatus = {
    project_setup: 'done', ingest: parsedDocs.length ? 'done' : 'pending',
    analyze: analyzeNode ? 'done' : 'pending',
    interview: interviewNode?.status === 'done' ? 'done' : interviewNode ? 'active-run' : 'pending',
    competency: competencyNode?.status === 'done' ? 'done' : 'pending',
    generate: generateNode ? 'done' : 'pending',
    review: generateNode ? 'done' : 'pending',
    testcases: stageIdx >= STAGE_INDEX.testcases ? 'done' : 'pending',
    evaluate: stageIdx >= STAGE_INDEX.evaluate ? 'done' : 'pending',
    optimize: stageIdx >= STAGE_INDEX.optimize ? 'done' : 'pending',
  };
  // 自动焦点：最靠后的已解锁未完成节点（进入评测阶段后跟随 stage）
  const autoFocus = !unlocked.analyze ? 'ingest'
    : !analyzeNode ? (tables.length ? 'analyze' : 'ingest')
    : !(interviewNode?.status === 'done') ? 'interview'
    : !competencyNode ? 'competency'
    : !generateNode ? 'generate'
    : project.stage === 'optimize' ? 'optimize'
    : project.stage === 'evaluate' ? 'evaluate'
    : project.stage === 'testcases' ? 'testcases'
    : 'review';
  const effFocus = focus && unlocked[focus] ? focus : autoFocus;

  const doAnalyze = async () => {
    setAnalyzing(true); setErr('');
    try { await runAnalyze(projectId); await load(); }
    catch (e) { setErr(e.message); }
    finally { setAnalyzing(false); }
  };

  return (
    <div className="wb">
      {/* 左：workflow 时间线 */}
      <div className="wb-col wb-left">
        <div className="wb-h">建模流程</div>
        <div className="wb-b">
          {TRUNK.map(([key, title], i) => {
            const canGo = unlocked[key];
            const done = nodeStatus[key] === 'done';
            const active = key === effFocus;
            return (
              <div key={key}
                className={`tl-node ${done ? 'done' : ''} ${active ? 'active' : ''} ${canGo ? 'clickable' : ''}`}
                onClick={() => canGo && setFocus(key)}>
                <div className="th">
                  <span className="no">{done ? '✓' : i + 1}</span>
                  <span className="tt" style={!canGo ? { color: 'var(--muted)' } : undefined}>{title}</span>
                  {!canGo && <span className="ts">待解锁</span>}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* 中：当前节点交互区（按 effFocus 路由） */}
      <div className="wb-col wb-mid">
        <div className="wb-h">
          {{ ingest: '资料上传与解析', analyze: '初步分析', interview: '建模访谈',
             competency: '胜任力问题', generate: '知识包生成', review: '包评审',
             testcases: '评测用例录入', evaluate: '评测运行', optimize: '优化建议与应用',
             project_setup: '项目信息' }[effFocus]}
          {effFocus === 'analyze' && !analyzeNode && tables.length > 0 && (
            <button className="btn btn-p" style={{ marginLeft: 'auto', padding: '5px 14px', fontSize: 12.5 }}
              disabled={analyzing} onClick={doAnalyze}>
              {analyzing ? '分析中…（约 1~2 分钟）' : '▶ 开始初步分析'}
            </button>
          )}
        </div>
        <div className="wb-b">
          {(effFocus === 'ingest' || effFocus === 'project_setup') && (
            <IngestView project={project} documents={documents} onUploaded={load} />
          )}
          {effFocus === 'analyze' && (analyzeNode
            ? <AnalysisView analysis={analyzeNode.output} onReanalyze={doAnalyze} analyzing={analyzing}
                onAdvance={() => setFocus('interview')} />
            : <IngestView project={project} documents={documents} onUploaded={load} />)}
          {effFocus === 'interview' && (
            <InterviewPanel projectId={projectId} onAdvance={() => { load(); setFocus('competency'); }} />
          )}
          {effFocus === 'competency' && (
            <CompetencyPanel projectId={projectId} onAdvance={() => { load(); setFocus('generate'); }} />
          )}
          {effFocus === 'generate' && (
            <GeneratePanel projectId={projectId} onAdvance={() => { load(); setFocus('review'); }} />
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

      {/* 右：上下文面板 */}
      <div className="wb-col wb-right">
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
      </div>
    </div>
  );
}

function IngestView({ project, documents, onUploaded }) {
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
      onUploaded();
    } catch (e) { setMsg(`上传失败：${e.message}`); }
    finally { setBusy(false); }
  };

  return (
    <div>
      <div style={{ fontSize: 13, color: 'var(--ink2)', marginBottom: 14, lineHeight: 1.7 }}>
        上传领域资料：<b>Excel 数据库表</b>（sheet=表、首行=字段名、次行可选中文注释）、
        以及 Markdown / Word / PDF / 文本说明文档。Excel 会被解析为数据字典（右栏可查看）。
      </div>
      <div className={`dropzone ${drag ? 'drag' : ''}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); upload([...e.dataTransfer.files]); }}>
        {busy ? '上传解析中…' : '点击选择或拖拽文件到此处（支持 xlsx / md / docx / pdf / txt，可多选）'}
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
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AnalysisView({ analysis, onReanalyze, analyzing, onAdvance }) {
  if (!analysis) return <div className="empty">（无分析结果）</div>;
  return (
    <div>
      <div className="an-sec">
        <h4>领域概览</h4>
        <div style={{ fontSize: 13.5, lineHeight: 1.8 }}>{analysis.domainOverview}</div>
      </div>
      <div className="an-sec">
        <h4>候选业务对象（{analysis.candidateObjects?.length ?? 0}）</h4>
        {(analysis.candidateObjects ?? []).map((o) => (
          <div key={o.name} className="an-obj">
            <div className="on">▤ {o.name}</div>
            <div className="od">{o.rationale}</div>
            {o.sourceTables?.length > 0 && <div className="ot">来源表：{o.sourceTables.join(', ')}</div>}
          </div>
        ))}
      </div>
      <div className="an-sec">
        <h4>待澄清的开放问题（{analysis.openQuestions?.length ?? 0}）— 将进入建模访谈</h4>
        {(analysis.openQuestions ?? []).map((q, i) => (
          <div key={i} className="oq"><b>{q.topic}</b> — {q.why}</div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 10, marginTop: 6 }}>
        <button className="btn" disabled={analyzing} onClick={onReanalyze}>重新分析</button>
        <button className="btn btn-p" onClick={onAdvance}>进入建模访谈 →</button>
      </div>
    </div>
  );
}
