import React, { useEffect, useState } from 'react';

import {
  addFlowchartDiagram,
  getFlowchartDraft,
  respondToInteraction,
  saveFlowchartDraft,
} from './authoring-api.js';
import { FlowchartAddDialog, FlowchartEditor } from './flowchart-editor/index.js';
import { protectFlowchartBeforeUnload } from './flowchartNavigationGuard.js';

export default function FlowchartReviewPanel({ projectId, interaction, onDirtyChange, onCompleted }) {
  const [draft, setDraft] = useState(null);
  const [editorState, setEditorState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [addOpen, setAddOpen] = useState(false);
  const [acknowledgeWarnings, setAcknowledgeWarnings] = useState(false);

  useEffect(() => {
    let active = true;
    setDraft(null);
    setEditorState(null);
    setError('');
    setLoading(true);
    setAcknowledgeWarnings(false);
    void getFlowchartDraft(projectId, interaction.id)
      .then((value) => { if (active) setDraft(value); })
      .catch((cause) => {
        if (active) setError(cause instanceof Error ? cause.message : '流程图草稿加载失败');
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [interaction.id, projectId]);

  const dirty = Boolean(editorState?.dirty);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    const beforeUnload = (event) => { protectFlowchartBeforeUnload(event, dirty); };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, [dirty]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  const save = async (document, { expectedRevision }) => {
    const saved = await saveFlowchartDraft(projectId, interaction.id, expectedRevision, document);
    setDraft(saved);
    setAcknowledgeWarnings(false);
    return saved;
  };

  const addDiagram = async (input) => {
    if (!draft || dirty || busy) return;
    setBusy('add');
    setError('');
    try {
      const saved = await addFlowchartDiagram(projectId, interaction.id, {
        ...input,
        expectedRevision: draft.draftRevision,
      });
      setDraft(saved);
      setAddOpen(false);
      setAcknowledgeWarnings(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '流程图添加失败');
    } finally {
      setBusy('');
    }
  };

  const submit = async () => {
    if (!draft || dirty || busy || !draft.document.diagrams.length) return;
    setBusy('submit');
    setError('');
    try {
      await respondToInteraction(projectId, interaction.id, {
        action: 'accept',
        draftRevision: draft.draftRevision,
        acknowledgeWarnings: draft.warnings.length ? acknowledgeWarnings : true,
      });
      onDirtyChange?.(false);
      await onCompleted?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '流程图提交失败');
    } finally {
      setBusy('');
    }
  };

  if (loading && !draft) return <div className="empty">正在加载可编辑流程图…</div>;
  if (!draft) return <div className="empty">{error || '流程图草稿尚未就绪'}</div>;

  const warnings = draft.warnings ?? [];
  const diagramCount = editorState?.document?.diagrams?.length ?? draft.document.diagrams.length;
  const submitDisabled = dirty || Boolean(busy) || diagramCount === 0
    || (warnings.length > 0 && !acknowledgeWarnings);

  return (
    <section className="flowchart-review-panel" aria-label="业务流程图确认">
      {error && <div className="err-banner" role="alert">{error}</div>}
      <div className="flowchart-review-panel__editor">
        <FlowchartEditor
          key={`${projectId}:${interaction.id}`}
          document={draft.document}
          savedRevision={draft.draftRevision}
          disabled={Boolean(busy)}
          onSave={save}
          onStateChange={setEditorState}
          onRecommendDiagram={() => { setError(''); setAddOpen(true); }}
          onCreateBlankDiagram={() => { void addDiagram({ mode: 'blank' }); }}
        />
      </div>
      <footer className="flowchart-review-panel__footer">
        <div>
          {diagramCount === 0 && <span className="flowchart-review-panel__warning">至少保存一个业务流程后才能提交</span>}
          {warnings.length > 0 && (
            <label className="flowchart-review-panel__ack">
              <input type="checkbox" checked={acknowledgeWarnings} disabled={Boolean(busy)}
                onChange={(event) => setAcknowledgeWarnings(event.target.checked)} />
              已检查 {warnings.length} 条完整性提醒，仍确认提交
            </label>
          )}
        </div>
        <button type="button" className="btn btn-p" disabled={submitDisabled} onClick={submit}>
          {busy === 'submit' ? '正在提交…' : '确认业务流程'}
        </button>
      </footer>
      <FlowchartAddDialog
        open={addOpen}
        candidates={draft.availableCandidates}
        modelingGoal={draft.modelingGoal}
        busy={busy === 'add'}
        error={busy === 'add' ? '' : error}
        onClose={() => { if (!busy) setAddOpen(false); }}
        onAddCandidate={(candidate) => { void addDiagram({
          mode: 'candidate',
          ...(candidate?.candidateId
            ? { candidateId: candidate.candidateId }
            : { candidateTitle: candidate?.title ?? '' }),
        }); }}
        onAddAi={(instruction) => { void addDiagram({ mode: 'ai', instruction }); }}
      />
    </section>
  );
}
