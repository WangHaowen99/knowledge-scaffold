// 资料分析 / 建模蓝图共用的小件：资料图标、带出处的文字、上传资料、打开弹窗、发命令
import { Fragment, useCallback, useContext, useRef, useState } from 'react';
import { useApp } from '../../../app/context.js';
import { navigate, pagePath } from '../../../app/router.js';
import { toast } from '../../../app/toast.jsx';
import { errorText } from '../../../api/errors.js';
import { Ev, Icon } from '../../../ui/index.jsx';
import { useActions } from '../cards.jsx';
import { WbCtx } from '../parts.jsx';
import { anchorLabel, anchorTokens, fileIcon } from './model.js';

export function MatIcon({ kind, size = 16, boxed }) {
  const { icon, table } = fileIcon(kind);
  const color = table ? 'var(--teal)' : 'var(--blue)';
  if (!boxed) return <span style={{ color, display: 'flex' }}><Icon name={icon} size={size} /></span>;
  return <span className="fi" style={{ background: table ? 'var(--t50)' : 'var(--b50)', color }}><Icon name={icon} size={size} /></span>;
}

// 「**加粗**」和 [^n] 角标；角标按 anchors[{n, anchor, label}] 换成出处标签，悬停看原文
export function AnchorText({ text, anchors }) {
  const byN = Object.fromEntries((anchors || []).map((a, i) => [a.n ?? i + 1, a]));
  return anchorTokens(text).map((t, i) => {
    if (t.t === 'bold') return <b key={i}>{t.v}</b>;
    if (t.t === 'cite') {
      const a = byN[t.n];
      return a ? <Ev key={i} anchor={a.anchor} label={a.label || anchorLabel(a.anchor)} /> : null;
    }
    return <Fragment key={i}>{t.v}</Fragment>;
  });
}

// 页面按钮发命令到对话（S-20）：对话里出现一条「来自…」的消息
export async function pageCommand(api, pid, { command, args, text, page = 'wb-materials', label = '资料地图', done }) {
  try {
    await api.sendCommand(pid, { command, args, text, origin: { page, label } });
    if (done) toast(done, 'send');
    return true;
  } catch (e) {
    toast(errorText(e), 'alert');
    return false;
  }
}

// 打开资料地图、核验、蓝图等弹窗：工作台里由页面统一挂；别处（没有宿主）跳到对应页面带上 dialog
export function useOpenDialog() {
  const { openMaterials } = useActions();
  const { pid } = useContext(WbCtx);
  return useCallback((dialog) => {
    if (openMaterials) { openMaterials(dialog); return; }
    const kind = typeof dialog === 'string' ? dialog : dialog.kind;
    navigate(pagePath(kind === 'blueprint' ? 'wb-blueprint' : 'wb-materials', pid), { dialog: kind });
  }, [openMaterials, pid]);
}

// 上传资料：先进暂存区（U-01），再落到资料区（R-02）；一次选多份
export function useMaterialUpload(pid, accept) {
  const { api, services } = useApp();
  const inputRef = useRef(null);
  const [busy, setBusy] = useState(0);
  const upload = useCallback(async (files) => {
    const list = [...(files || [])];
    if (!list.length) return;
    setBusy((n) => n + list.length);
    let ok = 0;
    for (const file of list) {
      try {
        const up = await services.upload(file, 'material', { projectId: pid });
        await api.addResources(pid, { upload_id: up.upload_id });
        ok += 1;
      } catch (e) {
        toast(`${file.name}：${errorText(e)}`, 'alert');
      } finally {
        setBusy((n) => n - 1);
      }
    }
    if (ok) toast(`已上传 ${ok} 份资料，助手会接着读`, 'upload');
  }, [api, services, pid]);
  const input = (
    <input
      ref={inputRef} type="file" multiple hidden accept={(accept || []).join(',') || undefined}
      onChange={(e) => { upload(e.target.files); e.target.value = ''; }}
    />
  );
  return { pick: () => inputRef.current?.click(), upload, input, busy: busy > 0 };
}
