// 确认蓝图后怎么建：一片一片来（BP-03 next=L2 → wb-l2）；按蓝图一路建完（BP-03 next=L4 拿预览，已确认过就 AB-03，确认后 AB-04 → l4-run）
import { useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../../app/context.js';
import { navigate, pagePath } from '../../../app/router.js';
import { toast } from '../../../app/toast.jsx';
import { errorText } from '../../../api/errors.js';
import {
  Button, Chip, Icon, KV, Modal, Note,
} from '../../../ui/index.jsx';
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
  const { pid } = useProject();
  const [preview, setPreview] = useState(initial);
  const [busy, setBusy] = useState(false);
  const included = (preview.slices || []).filter((s) => s.included);
  const start = async () => {
    setBusy(true);
    try {
      await api.startAutobuild(pid, { preview_token: preview.preview_token });
      toast('自主构建已开始，按蓝图一路建完', 'rocket');
      onClose();
      navigate(pagePath('l4-run', pid));
    } catch (e) {
      if (e.code === 'preview_expired') {
        try { setPreview(await api.previewAutobuild(pid, {})); toast('预览过期了，已按最新状态刷新，再确认一次', 'refresh'); } catch (e2) { toast(errorText(e2), 'alert'); }
      } else toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open onClose={onClose} icon="rocket" title="按蓝图一路建完" w={580}
      sub="开始前确认这次的范围和预算；运行中随时可以暂停，或转为陪伴继续"
      foot={(
        <>
          <a className="link xs grow" onClick={() => { onClose(); navigate(pagePath('proj-settings', pid), { section: 'autobuild' }); }}>在项目设置里调整自主构建策略</a>
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
              <div>{included.length} 片{included.length < (preview.slices || []).length ? `（另有 ${(preview.slices || []).length - included.length} 片不在范围）` : ''}</div>
              <div className="row wrap mt4" style={{ gap: 6 }}>
                {(preview.slices || []).map((s) => <Chip key={s.id} tone={s.included ? 'line' : 'gray'} sm>{s.name}{s.current ? ` · ${s.current}` : ''}</Chip>)}
              </div>
            </div>
          )],
          ['预算', budgetText(preview.budget)],
          ['生产库', preview.production || '—'],
          ['拿不准时', preview.below_threshold_text || '—'],
        ]}
      />
      {(preview.warnings || []).map((w) => <Note key={w} tone="amber" className="mt12">{w}</Note>)}
      {preview.queued ? <Note tone="gray" className="mt12">平台同时运行的自主构建已满，开始后排队{preview.queue_position ? `（第 ${preview.queue_position} 位）` : ''}，轮到时自动开始。</Note> : null}
      <div className="xs faint mt12 row g4"><Icon name="clock" size={12} />这份预览 10 分钟内有效，过期后重新预览</div>
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
          toast('蓝图已确认，开始第一个切片', 'check');
        }
        onClose();
        navigate(pagePath('wb-l2', pid));
      } catch (e) {
        toast(errorText(e), 'alert');
        onClose();
      }
    })();
  }, [api, pid, next, onClose]);
  return preview ? <AutobuildPreviewDialog preview={preview} onClose={onClose} /> : null;
}
