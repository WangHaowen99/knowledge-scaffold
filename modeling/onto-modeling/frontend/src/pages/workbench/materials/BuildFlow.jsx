// 开始自主构建：AB-03 拿开始前预览，确认后 AB-04 → l4-run。F8 起蓝图不用确认，不再先走 BP-03
import { useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../../app/context.js';
import { navigate, pagePath } from '../../../app/router.js';
import { toast } from '../../../app/toast.jsx';
import { errorText } from '../../../api/errors.js';
import {
  Button, KV, Modal, Note,
} from '../../../ui/index.jsx';
import {
  previewScope, startErrorText, startLabel, takeOverText,
} from '../autobuildModel.js';

export function AutobuildPreviewDialog({ preview: initial, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [preview, setPreview] = useState(initial);
  const [busy, setBusy] = useState(false);
  const start = async () => {
    setBusy(true);
    try {
      // 预览时别的对话正占着助手：开始就接过来（撤回没回答的问题、打断那一轮），不再点了才报错
      const run = await api.startAutobuild(pid, { preview_token: preview.preview_token, ...(preview.busy ? { take_over: true } : {}) });
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
      open onClose={onClose} icon="rocket" title="开始自主构建" w={580}
      foot={(
        <>
          <a className="link xs grow" onClick={() => { onClose(); navigate(pagePath('proj-settings', pid), { section: 'autobuild' }); }}>调整策略</a>
          <Button onClick={onClose}>取消</Button>
          <Button kind="l4" icon="rocket" disabled={busy} onClick={start}>{startLabel(preview)}</Button>
        </>
      )}
    >
      <KV
        w="72px"
        pairs={[
          ['范围', previewScope(preview)],
          ['拿不准时', preview.below_threshold_text || '—'],
        ]}
      />
      {(preview.warnings || []).map((w) => <Note key={w} tone="amber" className="mt12">{w}</Note>)}
      {preview.busy ? <Note tone="amber" className="mt12">{takeOverText(preview.busy)}</Note> : null}
      {preview.queued ? <Note tone="gray" className="mt12">名额已满，开始后排队{preview.queue_position ? `（第 ${preview.queue_position} 位）` : ''}</Note> : null}
    </Modal>
  );
}

// 由工作台的弹窗宿主挂载：L4 弹出开始前预览；L2（F8 之前的「一段一段来」链接）直接回到对话
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
        if (next === 'L4') {
          setPreview(await api.previewAutobuild(pid, {}));
          return;
        }
        onClose();
        navigate(pagePath('wb-l2', pid));
      } catch (e) {
        toast(startErrorText(e, errorText), 'alert');
        onClose();
      }
    })();
  }, [api, pid, next, onClose]);
  return preview ? <AutobuildPreviewDialog preview={preview} onClose={onClose} /> : null;
}
