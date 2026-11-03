// 版本页的弹窗与抽屉：标记版本（V-07）、删除标记（V-08）、回退（V-05 预览 → V-06）、查看某版本的文件（V-03、F-06）
import { useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import {
  Button, Chip, Drawer, Field, Icon, Inp, Modal, Note, Select,
} from '../../ui/index.jsx';
import {
  fmtStamp, pathLabel, revertBlock, revertChips, revertCountText, revertErrorText, tagLabelError, undoneText,
} from './versionsModel.js';

const PACK_LABEL = { 'industry-retail': '零售行业包', core: '内核' };

export function TagModal({ open, items, initial, onClose, onDone }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [commit, setCommit] = useState(initial);
  const [label, setLabel] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) { setCommit(initial); setLabel(''); setError(''); }
  }, [open, initial]);
  const v = (items || []).find((x) => x.commit === commit);
  const submit = async () => {
    const bad = tagLabelError(label);
    if (bad) { setError(bad); return; }
    setBusy(true);
    try {
      const r = await api.tagVersion(pid, commit, label.trim());
      toast(`已标记 ${r.version_label || v?.version_label || ''}`, 'tag');
      onDone?.(r);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={open}
      onClose={onClose}
      icon="tag"
      title="标记版本"
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" disabled={busy} onClick={submit}>标记</Button>
        </>
      )}
    >
      <div className="col" style={{ gap: 12 }}>
        <Field label="版本">
          <Select
            value={commit}
            options={(items || []).map((x) => ({ value: x.commit, label: `${x.version_label}${x.current ? ' · 当前' : ` · ${fmtStamp(x.at)}`}` }))}
            onChange={setCommit}
            className="vr-sel"
          />
        </Field>
        <Field label="标记名称" hint={error ? <span style={{ color: 'var(--red)' }}>{error}</span> : null}>
          <Inp
            value={label}
            placeholder="如：校验通过的里程碑"
            maxLength={120}
            error={!!error}
            onChange={(e) => { setLabel(e.target.value); setError(''); }}
            onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
          />
        </Field>
      </div>
    </Modal>
  );
}

export function DeleteTagModal({ tag, onClose, onDone }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      await api.deleteVersionTag(pid, tag.name);
      toast('已删除标记', 'tag');
      onDone?.();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={!!tag}
      onClose={onClose}
      danger
      icon="tag"
      title={`删除标记${tag?.label ? `「${tag.label}」` : ''}？`}
      sub="只删标记，版本本身保留"
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="danger fill" disabled={busy} onClick={submit}>删除标记</Button>
        </>
      )}
    />
  );
}

// 回退被挡住时（合入冲突、助手在跑、自主构建中）的去处
function BlockAction({ action, lock }) {
  const { pid } = useProject();
  const conv = lock?.conversation_id ? { c: lock.conversation_id } : undefined;
  if (action === 'confirm') return <Button size="xs" kind="primary" icon="merge" onClick={() => navigate(pagePath('inbox', pid))}>去处理</Button>;
  if (action === 'run') return <Button size="xs" onClick={() => navigate(pagePath('l4-run', pid))}>查看运行</Button>;
  return <Button size="xs" onClick={() => navigate(pagePath('wb-l2', pid), conv)}>回到对话</Button>;
}

export function RevertModal({ version, onClose, onDone }) {
  const { api } = useApp();
  const { pid, scope } = useProject();
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setPreview(null);
    setError(null);
    if (!version) return undefined;
    let alive = true;
    api.revertPreview(pid, version.commit).then((p) => alive && setPreview(p)).catch((e) => alive && setError({ text: errorText(e) }));
    return () => { alive = false; };
  }, [api, pid, version]);
  const block = revertBlock(scope);
  const submit = async () => {
    setBusy(true);
    try {
      const r = await api.revertVersion(pid, version.commit);
      toast(`已回退并生成版本 ${r.version_label}`, 'undo');
      onDone?.(r);
    } catch (e) {
      const lockBlock = e.code === 'write_scope_locked' ? revertBlock(e.details) : e.code === 'autobuild_running' ? revertBlock({ state: 'autobuild_slice' }) : null;
      setError({ text: revertErrorText(e, errorText(e)), block: lockBlock, lock: e.details });
    } finally {
      setBusy(false);
    }
  };
  const label = version?.version_label || '';
  const chips = revertChips(preview?.files);
  const shownBlock = block || error?.block;
  return (
    <Modal
      open={!!version}
      onClose={onClose}
      icon="undo"
      title={`回退到 ${label}？`}
      sub={`会生成新版本 ${preview?.next_version_label || '…'}，可再回退`}
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" disabled={busy || !preview || !!shownBlock} onClick={submit}>回退并生成新版本</Button>
        </>
      )}
    >
      <div className="col sm" style={{ gap: 8 }}>
        {shownBlock ? (
          <div className="note amber">
            <Icon name="lock" />
            <div className="grow">{shownBlock.text}</div>
            <BlockAction action={shownBlock.action} lock={error?.lock || scope} />
          </div>
        ) : null}
        {error && !error.block ? <Note tone="red">{error.text}</Note> : null}
        {!preview && !error ? <div className="muted">正在计算要恢复的文件…</div> : null}
        {preview ? (
          <>
            <div>
              {revertCountText(preview.files)}
              {preview.undone_versions.length ? `，${undoneText(preview.undone_versions)} 的改动会被撤回：` : '。'}
            </div>
            {chips.length ? (
              <div className="row wrap g4">
                {chips.map((c) => <Chip key={c.key} tone="line" sm className={c.del ? 'vr-del' : undefined} title={c.del ? '这个文件在那时还没有，回退后会删掉' : undefined}>{c.label}</Chip>)}
              </div>
            ) : null}
            <Note tone="gray" icon="layers">
              启用的能力也回到 {label} 时的状态
              {preview.packs_lock_changes.length ? (
                <div className="xs mt4">
                  {preview.packs_lock_changes.map((p) => `${p.label || PACK_LABEL[p.pack] || p.pack} ${p.from || '未启用'} → ${p.to || '停用'}`).join('；')}
                </div>
              ) : null}
            </Note>
          </>
        ) : null}
      </div>
    </Modal>
  );
}

// 查看某个版本的文件：列表（V-03）→ 点开看内容（F-06），可以拿去和当前对比
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
  const groups = {};
  (files || []).forEach((f) => {
    const dir = f.path.split('/').slice(0, -1).join('/');
    (groups[dir] = groups[dir] || []).push(f);
  });
  return (
    <Drawer
      open={!!version}
      onClose={onClose}
      icon="eye"
      w={520}
      title={open ? pathLabel(open.path) : `${version?.version_label || ''} 的文件`}
      sub={open ? `${open.version_label} · 只读` : `${files ? `${files.length} 个文件 · ` : ''}只读，${version ? fmtStamp(version.at) : ''}`}
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
      ) : Object.entries(groups).map(([dir, list]) => (
        <div key={dir} className="mb12">
          <div className="sec-t"><Icon name="folder" size={14} />{dir || '根目录'}<span className="sp" /><span className="faint">{list.length}</span></div>
          {list.map((f) => (
            <div key={f.path} className="vr-file" onClick={() => show(f.path)}>
              <Icon name="file" size={14} />
              <span className="grow ell">{f.path.split('/').pop()}</span>
              <span className="xs faint num">{formatBytes(f.size)}</span>
            </div>
          ))}
        </div>
      ))}
      {files && !files.length ? <div className="muted sm">这个版本里没有知识包条目文件。</div> : null}
    </Drawer>
  );
}
