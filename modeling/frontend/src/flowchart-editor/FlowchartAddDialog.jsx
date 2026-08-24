import React, { useEffect, useRef, useState } from 'react';
import { FileUp, Sparkles, X } from 'lucide-react';

const MAX_FILE_BYTES = 1024 * 1024;
const MERMAID_FILE_PATTERN = /\.(?:mmd|mermaid)$/iu;

export function mermaidImportFailure(cause) {
  if (cause?.data?.diagnostic) return cause.data.diagnostic;
  return {
    code: 'MERMAID_IMPORT_REQUEST_FAILED',
    category: '导入请求失败',
    message: cause instanceof Error ? cause.message : 'Mermaid 导入失败',
    suggestion: '请检查网络与登录状态；如果问题持续，请保留当前内容并联系管理员。',
  };
}

function clientFailure(code, category, message, suggestion) {
  return { code, category, message, suggestion };
}

export function MermaidImportErrorDetails({ diagnostic }) {
  if (!diagnostic) return null;
  const location = diagnostic.line
    ? `第 ${diagnostic.line} 行${diagnostic.column ? `，第 ${diagnostic.column} 列` : ''}`
    : '';
  return (
    <div className="flowchart-import-error" role="alert">
      <div><strong>{diagnostic.category || '导入失败'}</strong>{location && <span>{location}</span>}</div>
      <p>{diagnostic.message}</p>
      {diagnostic.sourceLine && (
        <pre aria-label="错误源码位置"><code>{diagnostic.sourceLine}{'\n'}
          {' '.repeat(Math.max(0, Number(diagnostic.column || 1) - 1))}^
        </code></pre>
      )}
      {diagnostic.suggestion && <small>修复建议：{diagnostic.suggestion}</small>}
    </div>
  );
}

function MermaidPreview({ preview }) {
  if (!preview) return null;
  return (
    <div className="flowchart-import-preview" aria-label="Mermaid 导入预览">
      <div>
        <strong>{preview.diagram?.title}</strong>
        <span>{preview.summary?.diagramType} · {preview.summary?.direction}</span>
      </div>
      <dl>
        <div><dt>节点</dt><dd>{preview.summary?.nodeCount ?? 0}</dd></div>
        <div><dt>连线</dt><dd>{preview.summary?.edgeCount ?? 0}</dd></div>
        <div><dt>分组</dt><dd>{preview.summary?.subgraphCount ?? 0}</dd></div>
      </dl>
      <div className="flowchart-import-preview__path">
        {preview.diagram?.nodes?.slice(0, 8).map((node) => node.label).join(' → ')}
        {(preview.diagram?.nodes?.length ?? 0) > 8 ? ' …' : ''}
      </div>
      {preview.notices?.map((notice) => <p key={notice.code}>{notice.message}</p>)}
      {preview.warnings?.length > 0 && (
        <ul aria-label="导入流程警告">
          {preview.warnings.map((warning, index) => (
            <li key={`${warning.code}:${warning.nodeId || warning.edgeId || index}`}>{warning.message}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function FlowchartAddDialog({
  open,
  initialMode = 'assistant',
  candidates = [],
  modelingGoal,
  busy = false,
  error = '',
  onClose,
  onAddCandidate,
  onAddAi,
  onPreviewMermaid,
  onImportMermaid,
}) {
  const [mode, setMode] = useState(initialMode);
  const [instruction, setInstruction] = useState('');
  const [title, setTitle] = useState('');
  const [source, setSource] = useState('');
  const [filename, setFilename] = useState('');
  const [preview, setPreview] = useState(null);
  const [diagnostic, setDiagnostic] = useState(null);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef(null);

  useEffect(() => {
    if (open) setMode(initialMode);
  }, [initialMode, open]);
  useEffect(() => {
    if (open) return;
    setInstruction('');
    setTitle('');
    setSource('');
    setFilename('');
    setPreview(null);
    setDiagnostic(null);
    setDragging(false);
  }, [open]);
  if (!open) return null;

  const updateTitle = (value) => {
    setTitle(value);
    setPreview(null);
    setDiagnostic(null);
  };
  const updateSource = (value, nextFilename = filename) => {
    setSource(value);
    setFilename(nextFilename);
    setPreview(null);
    setDiagnostic(null);
  };
  const readFile = async (file) => {
    if (!file) return;
    setPreview(null);
    setDiagnostic(null);
    if (!MERMAID_FILE_PATTERN.test(file.name)) {
      setDiagnostic(clientFailure(
        'MERMAID_FILE_TYPE_UNSUPPORTED', '文件类型不支持',
        `不能导入“${file.name}”`, '请选择扩展名为 .mmd 或 .mermaid 的 UTF-8 文本文件。',
      ));
      return;
    }
    if (file.size > MAX_FILE_BYTES) {
      setDiagnostic(clientFailure(
        'MERMAID_SOURCE_TOO_LARGE', '文件过大',
        `文件大小为 ${file.size} 字节，超过 1 MiB 上限`, '请拆分文件后分别导入。',
      ));
      return;
    }
    try {
      const bytes = await file.arrayBuffer();
      const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      updateSource(text, file.name);
    } catch {
      setDiagnostic(clientFailure(
        'MERMAID_FILE_ENCODING_INVALID', '文件读取失败',
        '文件不是有效的 UTF-8 文本', '请将文件另存为 UTF-8 编码后重试。',
      ));
    }
  };
  const input = () => ({ title: title.trim(), source, ...(filename ? { filename } : {}) });
  const validateInput = () => {
    if (!title.trim()) return clientFailure(
      'MERMAID_TITLE_REQUIRED', '流程名称错误', '请填写新流程名称',
      '流程名称用于流程列表和知识包流程文件，不能为空。',
    );
    if (!source.trim()) return clientFailure(
      'MERMAID_EMPTY', '内容错误', 'Mermaid 内容为空',
      '请粘贴 Mermaid 源码，或选择 .mmd/.mermaid 文件。',
    );
    return null;
  };
  const runPreview = async () => {
    const invalid = validateInput();
    if (invalid) { setDiagnostic(invalid); return; }
    setDiagnostic(null);
    try {
      setPreview(await onPreviewMermaid?.(input()));
    } catch (cause) {
      setPreview(null);
      setDiagnostic(mermaidImportFailure(cause));
    }
  };
  const confirmImport = async () => {
    if (!preview) return;
    setDiagnostic(null);
    try {
      await onImportMermaid?.(input());
    } catch (cause) {
      setDiagnostic(mermaidImportFailure(cause));
    }
  };

  return (
    <div className="flowchart-add-dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose?.();
    }}>
      <section className="flowchart-add-dialog" role="dialog" aria-modal="true" aria-labelledby="flowchart-add-title">
        <header>
          <div>
            <span>围绕建模目标</span>
            <h3 id="flowchart-add-title">继续添加流程图</h3>
          </div>
          <button type="button" aria-label="关闭" disabled={busy} onClick={onClose}><X size={16} /></button>
        </header>
        <div className="flowchart-add-dialog__tabs" role="tablist" aria-label="流程图添加方式">
          <button type="button" role="tab" aria-selected={mode === 'assistant'}
            className={mode === 'assistant' ? 'is-active' : ''} disabled={busy}
            onClick={() => setMode('assistant')}><Sparkles size={14} /> AI 与资料推荐</button>
          <button type="button" role="tab" aria-selected={mode === 'mermaid'}
            className={mode === 'mermaid' ? 'is-active' : ''} disabled={busy}
            onClick={() => setMode('mermaid')}><FileUp size={14} /> 导入 Mermaid</button>
        </div>
        {mode === 'assistant' ? <>
          {modelingGoal?.problem && (
            <div className="flowchart-add-dialog__goal">
              <strong>当前目标</strong><span>{modelingGoal.problem}</span>
            </div>
          )}
          {candidates.length > 0 && (
            <div className="flowchart-add-dialog__section">
              <strong>资料分析推荐流程</strong>
              <div className="flowchart-add-dialog__candidates">
                {candidates.map((candidate, index) => (
                  <button key={candidate.candidateId || `${candidate.title}:${index}`} type="button" disabled={busy}
                    data-candidate-id={candidate.candidateId || undefined}
                    onClick={() => onAddCandidate?.(candidate)}>
                    <span>{candidate.title}</span>
                    <small>{candidate.rationale || candidate.trigger || '根据资料分析形成'}</small>
                  </button>
                ))}
              </div>
            </div>
          )}
          <div className="flowchart-add-dialog__section">
            <label htmlFor="flowchart-ai-instruction">让 AI 补充一个新流程</label>
            <textarea id="flowchart-ai-instruction" rows={3} value={instruction}
              placeholder="可补充流程范围、触发场景或关注重点；留空时由 AI 根据当前目标推荐"
              disabled={busy} onChange={(event) => setInstruction(event.target.value)} />
          </div>
          {error && <div className="flowchart-add-dialog__error" role="alert">{error}</div>}
          <footer>
            <button type="button" disabled={busy} onClick={onClose}>取消</button>
            <button type="button" className="is-primary" disabled={busy}
              onClick={() => onAddAi?.(instruction.trim())}>
              {busy ? '正在生成…' : 'AI 生成新流程'}
            </button>
          </footer>
        </> : <>
          <div className="flowchart-add-dialog__section flowchart-import-form">
            <label htmlFor="flowchart-import-title">新流程名称 <em>必填</em></label>
            <input id="flowchart-import-title" value={title} maxLength={80} disabled={busy}
              placeholder="例如：订单处理流程" onChange={(event) => updateTitle(event.target.value)} />
            <div className={`flowchart-import-drop${dragging ? ' is-dragging' : ''}`}
              role="button" tabIndex={0} aria-label="拖拽或选择 Mermaid 文件"
              onClick={() => fileRef.current?.click()}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); fileRef.current?.click(); }
              }}
              onDragEnter={(event) => { event.preventDefault(); if (!busy) setDragging(true); }}
              onDragOver={(event) => { event.preventDefault(); }}
              onDragLeave={(event) => {
                event.preventDefault();
                if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false);
              }}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                if (!busy) void readFile(event.dataTransfer.files?.[0]);
              }}>
              <FileUp size={24} aria-hidden="true" />
              <strong>{filename || '拖拽 .mmd/.mermaid 文件到这里'}</strong>
              <span>或点击选择文件，最大 1 MiB，仅支持 UTF-8 文本</span>
              <input ref={fileRef} type="file" accept=".mmd,.mermaid,text/plain" hidden disabled={busy}
                onChange={(event) => { void readFile(event.target.files?.[0]); event.target.value = ''; }} />
            </div>
            <label htmlFor="flowchart-import-source">Mermaid 源码</label>
            <textarea id="flowchart-import-source" rows={10} value={source} spellCheck="false" disabled={busy}
              placeholder={'flowchart TD\n  start([开始]) --> work[处理]\n  work --> finish([完成])'}
              onChange={(event) => updateSource(event.target.value, '')} />
            <small>仅支持 flowchart/graph；允许节点、连线、连线文字和 subgraph，不支持 click、外部链接、样式或 HTML。</small>
          </div>
          <MermaidImportErrorDetails diagnostic={diagnostic} />
          <MermaidPreview preview={preview} />
          <footer>
            <button type="button" disabled={busy} onClick={onClose}>取消</button>
            {!preview ? (
              <button type="button" className="is-primary" disabled={busy} onClick={() => { void runPreview(); }}>
                {busy ? '正在校验…' : '校验并预览'}
              </button>
            ) : (
              <button type="button" className="is-primary" disabled={busy} onClick={() => { void confirmImport(); }}>
                {busy ? '正在导入…' : '确认导入为新流程'}
              </button>
            )}
          </footer>
        </>}
      </section>
    </div>
  );
}
