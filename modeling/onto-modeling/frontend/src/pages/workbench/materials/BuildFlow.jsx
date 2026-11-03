// 确认蓝图后怎么建：一段一段来（BP-03 next=L2 → wb-l2）；按蓝图一路建完（BP-03 next=L4 拿预览，已确认过就 AB-03，确认后 AB-04 → l4-run）
import { useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../../app/context.js';
import { navigate, pagePath } from '../../../app/router.js';
import { toast } from '../../../app/toast.jsx';
import { errorText } from '../../../api/errors.js';
import {
  Button, Chip, KV, Modal, Note,
} from '../../../ui/index.jsx';
import { evidenceInProject } from '../../../state/evidence.js';
import { startErrorText } from '../autobuildModel.js';
import { budgetText } from './model.js';

async function currentBlueprint(api, pid) {
  try {
    return await api.blueprint(pid);
  } catch (e) {
    if (e.code === 'blueprint_not_found') return null;
    throw e;
  }
}

export function AutobuildPreviewDialog({ preview: initial, onClose }) {
  const { api } = useApp();
  const project = useProject();
  const { pid } = project;
  const [preview, setPreview] = useState(initial);
  const [busy, setBusy] = useState(false);
  const included = (preview.slices || []).filter((s) => s.included);
  const start = async () => {
    setBusy(true);
    try {
      const run = await api.startAutobuild(pid, { preview_token: preview.preview_token });
      toast(run?.state === 'queued' ? `运行 #${run.run_no} 已排队，轮到时自动开始` : '自主构建已开始', 'rocket');
      onClose();
      // 运行在新开的运行对话里（AB-04 带 conversation_id）：直接切过去，运行条、进展都在那里
      navigate(pagePath('l4-run', pid), run?.conversation_id ? { c: run.conversation_id } : undefined);
    } catch (e) {
      if (e.code === 'preview_expired') {
        try { setPreview(await api.previewAutobuild(pid, {})); toast('预览已刷新，请再确认', 'refresh'); } catch (e2) { toast(errorText(e2), 'alert'); }
      } else toast(startErrorText(e, errorText), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open onClose={onClose} icon="rocket" title="按蓝图一路建完" w={580}
      foot={(
        <>
          <a className="link xs grow" onClick={() => { onClose(); navigate(pagePath('proj-settings', pid), { section: 'autobuild' }); }}>调整策略</a>
          <Button onClick={onClose}>取消</Button>
          <Button kind="l4" icon="rocket" disabled={busy} onClick={start}>{preview.queued ? '开始（排队）' : '开始自主构建'}</Button>
        </>
      )}
    >
      <KV
        w="72px"
        pairs={[
          ['范围', (
            <div>
              <div>{included.length} 段{included.length < (preview.slices || []).length ? `（另有 ${(preview.slices || []).length - included.length} 段不在范围）` : ''}</div>
              <div className="row wrap mt4" style={{ gap: 6 }}>
                {(preview.slices || []).map((s) => <Chip key={s.id} tone={s.included ? 'line' : 'gray'} sm>{s.name}{s.current ? ` · ${s.current}` : ''}</Chip>)}
              </div>
            </div>
          )],
          ['预算', budgetText(preview.budget)],
          // 证据源本期不开放：生产库采集这一行随证据源出现
          ...(evidenceInProject(project) ? [['生产库', preview.production || '—']] : []),
          ['拿不准时', preview.below_threshold_text || '—'],
        ]}
      />
      {(preview.warnings || []).map((w) => <Note key={w} tone="amber" className="mt12">{w}</Note>)}
      {preview.queued ? <Note tone="gray" className="mt12">名额已满，开始后排队{preview.queue_position ? `（第 ${preview.queue_position} 位）` : ''}</Note> : null}
    </Modal>
  );
}

// 由工作台的弹窗宿主挂载：先把蓝图确认掉，L2 直接回到对话，L4 弹出预览
export function BuildFlow({ next = 'L2', onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [preview, setPreview] = useState(null);
  const ran = useRef(false);
  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    (async () => {
      try {
        const bp = await currentBlueprint(api, pid);
        const draft = bp && bp.state !== 'confirmed';
        if (next === 'L4') {
          const p = draft ? (await api.confirmBlueprint(pid, { base_revision: bp.revision, next: 'L4' })).autobuild_preview : await api.previewAutobuild(pid, {});
          if (!p) throw new Error('没有拿到自主构建预览');
          setPreview(p);
          return;
        }
        if (draft) {
          await api.confirmBlueprint(pid, { base_revision: bp.revision, next: 'L2' });
          toast('蓝图已确认，开始第一段', 'check');
        }
        onClose();
        navigate(pagePath('wb-l2', pid));
      } catch (e) {
        // 蓝图所在的那一轮和你的修改冲突时 BP-03 回 409 merge_conflict（F3）：去待处理选保留哪边
        toast(errorText(e), 'alert');
        onClose();
        if (e.code === 'merge_conflict') navigate(pagePath('inbox', pid));
      }
    })();
  }, [api, pid, next, onClose]);
  return preview ? <AutobuildPreviewDialog preview={preview} onClose={onClose} /> : null;
}
