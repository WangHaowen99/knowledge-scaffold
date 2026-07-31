import React, { useEffect, useMemo, useState } from 'react';

const TASK_KINDS = [
  ['material_analysis', '资料分析'], ['package_baseline_review', '基线检查'],
  ['modeling_brief', '建模目标'], ['process_interview', '流程访谈'],
  ['process_diagram', '流程图绘制'], ['process_review', '流程确认'],
  ['operation_extraction', '业务操作提取'], ['knowledge_extraction', '知识结构提取'],
  ['script_decision', '确定性能力判断'], ['script_generation', '脚本生成'],
  ['package_validation', '知识包校验'], ['package_repair', '知识包修复'],
  ['evaluation_repair', '评测缺陷修缮'], ['completion_review', '完成审阅'],
];

const ACTION_LABEL = { add: '新增任务', reorder: '重排任务', split: '拆分任务', merge: '合并任务' };

function etaDeltaText(delta) {
  const min = Number(delta?.minMinutes ?? 0);
  const max = Number(delta?.maxMinutes ?? 0);
  if (!min && !max) return '预计时长基本不变';
  const direction = max < 0 ? '减少' : '增加';
  const low = Math.min(Math.abs(min), Math.abs(max));
  const high = Math.max(Math.abs(min), Math.abs(max));
  return low === high ? `预计${direction} ${high} 分钟` : `预计${direction} ${low}–${high} 分钟`;
}

function initialDraft(config) {
  if (config.mode === 'add') return {
    kind: 'modeling_brief', title: '', goal: '', required: false,
  };
  if (config.mode === 'split') return {
    parts: [
      { title: `${config.task.title}（第一部分）`, goal: '' },
      { title: `${config.task.title}（第二部分）`, goal: '' },
    ],
  };
  if (config.mode === 'merge') return {
    title: `合并：${config.tasks.map((task) => task.title).join('、')}`.slice(0, 180),
    goal: '',
  };
  return {};
}

function buildEdit(config, draft) {
  if (config.mode === 'add') return {
    action: 'add', milestoneId: config.milestoneId,
    task: {
      kind: draft.kind, title: draft.title, goal: draft.goal,
      required: Boolean(draft.required), dependsOn: [],
    },
  };
  if (config.mode === 'split') return {
    action: 'split', taskId: config.task.id,
    parts: draft.parts.map((part) => ({ kind: config.task.kind, title: part.title, goal: part.goal })),
  };
  if (config.mode === 'merge') return {
    action: 'merge', taskIds: config.tasks.map((task) => task.id),
    task: { kind: config.tasks[0].kind, title: draft.title, goal: draft.goal },
  };
  return config.edit;
}

export default function TaskGraphEditDialog({ config, busy = false, onPreview, onApply, onCancel }) {
  const [draft, setDraft] = useState(() => initialDraft(config));
  const [preview, setPreview] = useState(null);
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [previewing, setPreviewing] = useState(false);
  const edit = useMemo(() => buildEdit(config, draft), [config, draft]);

  // 切换到另一种编辑模式（如从“新增”直接打开“合并”）时重置表单
  useEffect(() => {
    setDraft(initialDraft(config));
    setPreview(null);
    setReason('');
    setError('');
  }, [config]);

  // Esc 关闭（busy 时不打断进行中的应用）
  useEffect(() => {
    const onKeyDown = (event) => { if (event.key === 'Escape' && !busy) onCancel?.(); };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [busy, onCancel]);

  const requestPreview = async () => {
    setError('');
    setPreviewing(true);
    try { setPreview(await onPreview(edit)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setPreviewing(false); }
  };

  useEffect(() => {
    if (config.mode === 'reorder') void requestPreview();
    // 仅在打开一个新的直接重排对话时自动预览。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config]);

  const updatePart = (index, field, value) => setDraft((current) => ({
    ...current,
    parts: current.parts.map((part, partIndex) => partIndex === index ? { ...part, [field]: value } : part),
  }));

  return <section className="task-graph-edit-dialog" role="dialog" aria-modal="true" aria-label="编辑动态任务">
    <header><div><span>动态任务图</span><strong>{ACTION_LABEL[config.mode]}</strong></div>
      <button type="button" className="btn" onClick={onCancel}>关闭</button></header>
    {config.mode === 'add' && <div className="task-graph-edit-form">
      <label><span>任务类型</span><select value={draft.kind} onChange={(event) => setDraft({ ...draft, kind: event.target.value })}>
        {TASK_KINDS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <label><span>任务名称</span><input value={draft.title} maxLength={200} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
      <label><span>完成目标</span><textarea rows={3} value={draft.goal} maxLength={2000} onChange={(event) => setDraft({ ...draft, goal: event.target.value })} /></label>
      <label className="task-graph-edit-check"><input type="checkbox" checked={draft.required}
        onChange={(event) => setDraft({ ...draft, required: event.target.checked })} />设为当前里程碑必选任务</label>
    </div>}
    {config.mode === 'split' && <div className="task-graph-edit-form">
      <p>把“{config.task.title}”拆成前后串联的两个任务，下游任务会自动依赖第二项。</p>
      {draft.parts.map((part, index) => <fieldset key={index}><legend>子任务 {index + 1}</legend>
        <label><span>任务名称</span><input value={part.title} maxLength={200} onChange={(event) => updatePart(index, 'title', event.target.value)} /></label>
        <label><span>完成目标</span><textarea rows={2} value={part.goal} maxLength={2000} onChange={(event) => updatePart(index, 'goal', event.target.value)} /></label>
      </fieldset>)}
    </div>}
    {config.mode === 'merge' && <div className="task-graph-edit-form">
      <p>将合并：{config.tasks.map((task) => task.title).join('、')}</p>
      <label><span>合并后名称</span><input value={draft.title} maxLength={200} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
      <label><span>合并后目标</span><textarea rows={3} value={draft.goal} maxLength={2000} onChange={(event) => setDraft({ ...draft, goal: event.target.value })} /></label>
    </div>}
    {config.mode === 'reorder' && <p className="task-graph-edit-summary">系统会校验任务依赖，不能把任务移动到它所依赖任务之前。</p>}
    {!preview && config.mode !== 'reorder' && <button type="button" className="btn btn-p" disabled={previewing || busy}
      onClick={() => void requestPreview()}>{previewing ? '正在分析影响…' : '预览影响'}</button>}
    {error && <div className="task-adjustment-blocked" role="alert">{error}</div>}
    {preview && <div className="task-graph-edit-preview">
      <div><strong>{preview.allowed ? '可以应用这次调整' : '当前调整不可应用'}</strong><em>{etaDeltaText(preview.etaDelta)}</em></div>
      {preview.reason && <p>{preview.reason}</p>}
      {(preview.proposedTitles ?? []).length > 0 && <p>调整后的任务：{preview.proposedTitles.join('、')}</p>}
      {(preview.affectedTasks ?? []).length > 0 && <p>影响任务：{preview.affectedTasks.map((task) => task.title).join('、')}</p>}
      {preview.allowed && <label><span>调整原因</span><textarea rows={2} value={reason} maxLength={500}
        placeholder="说明本次调整原因，保存到审计记录" onChange={(event) => setReason(event.target.value)} /></label>}
    </div>}
    <footer>
      <button type="button" className="btn" disabled={busy} onClick={onCancel}>取消</button>
      {preview?.allowed && <button type="button" className="btn btn-p" disabled={busy || !reason.trim()}
        onClick={() => onApply({ edit, expectedRevision: preview.graphRevision, reason: reason.trim() })}>
        {busy ? '应用中…' : '确认应用'}
      </button>}
    </footer>
  </section>;
}
