// 版本页的弹窗与抽屉：标记迭代（V-07）、回退确认（V-05 预览后 → V-06）、查看某版本的文件（V-03、F-06）。
// 标记与回退的文字照 develop 的「历史」页签（ADR-0094）
import { useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import {
  Button, Drawer, Icon, Inp, Modal, Note,
} from '../../ui/index.jsx';
import { versionTitle } from '../../lib/versionHistory.js';
import { fileGroups, fmtStamp, pathLabel, revertConfirmText } from './versionsModel.js';

const PACK_LABEL = { 'industry-retail': '零售行业包', core: '内核' };

// 「标记迭代」：commit 为空时不出现；名称为空时点「标记」不做事
export function TagModal({ commit, onClose, onDone }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [label, setLabel] = useState('');
  useEffect(() => { if (commit) setLabel(''); }, [commit]);
  const submit = async () => {
    if (!label.trim()) return;
    try {
      await api.tagVersion(pid, commit, label.trim());
      toast('已标记', 'tag');
      onDone?.();
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  return (
    <Modal
      open={!!commit}
      onClose={onClose}
      icon="tag"
      title="标记迭代"
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" onClick={submit}>标记</Button>
        </>
      )}
    >
      <Inp
        value={label}
        placeholder="如：校验通过的里程碑"
        maxLength={120}
        autoFocus
        onChange={(e) => setLabel(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
      />
    </Modal>
  );
}

// 回退确认（develop 的确认框）：预览能回退时才打开；能力锁有变化时多说一行（插件化独有）
export function RevertConfirm({ target, onCancel, onConfirm }) {
  const preview = target?.preview;
  const packs = preview?.packs_lock_changes || [];
  return (
    <Modal
      open={!!target}
      onClose={onCancel}
      danger
      icon="undo"
      title={target ? `回退到版本 ${target.version.version ?? target.version.n}？` : ''}
      foot={(
        <>
          <Button onClick={onCancel}>取消</Button>
          <Button kind="danger fill" onClick={onConfirm}>确认回退</Button>
        </>
      )}
    >
      <div className="col sm" style={{ gap: 8 }}>
        <div>{revertConfirmText(preview?.changes)}</div>
        {packs.length ? (
          <Note tone="gray" icon="layers">
            启用的能力也回到当时的状态：
            {packs.map((p) => `${p.label || PACK_LABEL[p.pack] || p.pack} ${p.from || '未启用'} → ${p.to || '停用'}`).join('；')}
          </Note>
        ) : null}
      </div>
    </Modal>
  );
}

// 某版本的文件（V-03）：按业务类型分组；点一个看内容（F-06），可以拿去和当前对比
export function FilesDrawer({ version, onClose, onCompare }) {
  const { api, services } = useApp();
  const { pid, meta } = useProject();
  const [files, setFiles] = useState(null);
  const [open, setOpen] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    setFiles(null);
    setOpen(null);
    setError('');
    if (!version) return undefined;
    let alive = true;
    api.versionFiles(pid, version.commit).then((r) => alive && setFiles(r.items || [])).catch((e) => alive && setError(errorText(e)));
    return () => { alive = false; };
  }, [api, pid, version]);
  const show = async (path) => {
    try {
      const r = await api.fileAt(pid, version.commit, path);
      setOpen(r);
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const download = async () => {
    try {
      await services.download(`/projects/${encodeURIComponent(pid)}/versions/${encodeURIComponent(version.commit)}/files?download=1`, `${meta?.package_name || 'package'}-${version.version_label}.zip`);
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const groups = fileGroups(files);
  const shown = groups.reduce((n, g) => n + g.items.length, 0);
  return (
    <Drawer
      open={!!version}
      onClose={onClose}
      icon="eye"
      w={520}
      title={open ? pathLabel(open.path) : `${version ? versionTitle('iteration', version.n) : ''} 的文件`}
      sub={open ? `${open.version_label} · 只读` : `${files ? `${shown} 项 · ` : ''}只读，${version ? fmtStamp(version.at) : ''}`}
      foot={open ? (
        <>
          <Button size="sm" icon="arrowl" onClick={() => setOpen(null)}>返回列表</Button>
          <span className="sp" />
          <Button size="sm" kind="primary" icon="split" onClick={() => onCompare(open.path)}>与当前对比</Button>
        </>
      ) : (
        <>
          <Button size="sm" icon="download" onClick={download}>下载这个版本（zip）</Button>
          <span className="sp" />
          <Button size="sm" onClick={onClose}>关闭</Button>
        </>
      )}
    >
      {error ? <Note tone="red">{error}</Note> : null}
      {!files && !error ? <div className="muted sm">正在读取文件列表…</div> : null}
      {open ? (
        <pre className="vr-pre">{open.content}{open.truncated ? '\n…（只显示前 512 KB）' : ''}</pre>
      ) : groups.map((g) => (
        <div key={g.key} className="mb12">
          <div className="sec-t">{g.label}<span className="sp" /><span className="faint">{g.items.length}</span></div>
          {g.items.map((f) => (
            <div key={f.path} className="vr-file" onClick={() => show(f.path)}>
              <Icon name="file" size={14} />
              <span className="grow ell">{f.name}</span>
              <span className="xs faint num">{formatBytes(f.size)}</span>
            </div>
          ))}
        </div>
      ))}
      {files && !shown ? <div className="muted sm">这个版本里没有知识包条目文件。</div> : null}
    </Drawer>
  );
}
