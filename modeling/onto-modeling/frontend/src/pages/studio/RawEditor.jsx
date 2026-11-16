// 编辑原文（高级）：读 F-02、存 F-03；未保存的改动随手暂存进同一份草稿（ST-07 content，按顺序排队），
// 打开时恢复草稿；草稿基于的版本和文件当前版本不一致（助手改过）时不载入、提示放弃旧草稿（develop !608）；
// 保存时文件已被别人改过，提示刷新后对比（去版本历史 V-04）；大文件只读
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import CodeEditor from '../../components/cm/CodeEditor.jsx';
import { enqueueLatestDraftSave } from '../../lib/studioDraftQueue.js';
import { Button, Chip, Icon, Modal, Note } from '../../ui/index.jsx';
import { LockBar, copyText } from './parts.jsx';
import { STALE_DRAFT_TEXT, fmtBytes, hhmm, rawNotices } from './studioModel.js';

const AUTOSAVE_MS = 2000;
const WHO = { assistant: '助手', web: '网页', studio: 'Studio', mcp: 'MCP 客户端', autobuild: '自主构建' };

export default function RawEditor({ entry, lockInfo, lock, onBack, onSaved }) {
  const { api, services } = useApp();
  const { pid } = useProject();
  const [file, setFile] = useState(null);
  const [draft, setDraft] = useState(entry.draft || null);
  const [text, setText] = useState(null);
  const [gen, setGen] = useState(0);
  const [saved, setSaved] = useState(false);
  const [conflict, setConflict] = useState(null);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [error, setError] = useState(null);
  const dirty = useRef(false);
  const textRef = useRef(text);
  textRef.current = text;
  const saveQueue = useRef(Promise.resolve());
  const baseRef = useRef('');

  const load = useCallback(async (withDraft = true) => {
    try {
      const [f, d] = await Promise.all([api.fileContent(pid, entry.path), withDraft ? api.studioDraft(pid, entry.id).then((r) => r.draft).catch(() => null) : Promise.resolve(null)]);
      // 草稿只在基于文件当前版本时载入（rawNotices 的 restored）；旧版本草稿留着，提示放弃
      const restored = rawNotices(f, d).restored;
      baseRef.current = restored ? d.base_revision || f.revision : f.revision;
      setFile(f);
      setDraft(d);
      setText(restored ? d.content : f.content);
      setGen((g) => g + 1);
      setConflict(null);
      dirty.current = false;
    } catch (e) {
      setError(e);
    }
  }, [api, pid, entry.path, entry.id]);
  useEffect(() => { load(); }, [load]);

  const notes = rawNotices(file, draft);
  const changed = conflict;
  const readOnly = !!notes.big || !!lockInfo;
  const modified = file && text != null && text !== file.content;
  const unsaved = modified || !!notes.restored;

  // 改动随手暂存（和结构化编辑共用一份草稿），切去别处稍后回来还在；失败的留到下次再存
  const putDraft = useCallback(() => enqueueLatestDraftSave(saveQueue, dirty, async () => {
    const r = await api.saveStudioDraft(pid, entry.id, { base_revision: baseRef.current, content: textRef.current });
    setDraft(r.draft);
    return r;
  }), [api, pid, entry.id]);
  useEffect(() => {
    if (!dirty.current) return undefined;
    const t = setTimeout(putDraft, AUTOSAVE_MS);
    return () => clearTimeout(t);
  }, [text, putDraft]);
  const flushRef = useRef(putDraft);
  flushRef.current = putDraft;
  useEffect(() => () => { flushRef.current(); }, []);

  const save = async () => {
    dirty.current = false;
    await saveQueue.current; // 排着的暂存先落地
    try {
      const r = await api.saveFileContent(pid, { path: file.path, content: text, base_revision: baseRef.current });
      baseRef.current = r.revision;
      setSaved(true);
      setDraft(null);
      setFile((f) => ({ ...f, content: text, revision: r.revision }));
      toast(`已保存并记录版本 ${r.version_label}`, 'check');
      onSaved?.();
    } catch (e) {
      dirty.current = true;
      if (e.isConflict) setConflict({ by: e.details?.changed_by, at: e.details?.changed_at, version_label: e.details?.version_label });
      else toast(errorText(e), e.isLocked ? 'lock' : 'alert');
    }
  };
  const dropMine = async () => {
    try {
      await api.discardStudioDraft(pid, entry.id);
      await load(false);
      setSaved(false);
      toast('已换成文件当前内容', 'undo');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const cancel = async () => {
    setCancelOpen(false);
    dirty.current = false;
    await saveQueue.current; // 等正在存的那次落地，免得放弃之后又被它存回来
    try { if (draft || notes.restored) await api.discardStudioDraft(pid, entry.id); } catch { /* 草稿已经没了 */ }
    toast('已放弃修改', 'undo');
    onBack();
  };
  const download = async () => {
    try {
      await services.download(`/projects/${encodeURIComponent(pid)}/files/raw?path=${encodeURIComponent(file.path)}&download=true`, file.path.split('/').pop());
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  if (error) return <div className="stt-raw"><div className="sd-empty">{errorText(error)}</div></div>;
  if (!file || text == null) return <div className="stt-raw"><div className="sd-empty">正在读取原文…</div></div>;

  let chip = <Chip tone="gray" sm>没有改动</Chip>;
  if (saved && !modified) chip = <Chip tone="teal" icon="check">已保存</Chip>;
  else if (unsaved) chip = <Chip tone="amber" dot>未保存</Chip>;

  return (
    <div className="stt-raw">
      <div className="fl-ptb">
        <span>{chip}</span>
        <span className="sp" />
        <Button size="sm" kind="ghost" icon="copy" title="复制内容" onClick={async () => toast((await copyText(text)) ? '已复制全文' : '复制失败，请手动选择', 'copy')} />
        <Button size="sm" onClick={() => (unsaved ? setCancelOpen(true) : onBack())}>取消</Button>
        <Button size="sm" kind="primary" icon="check" disabled={readOnly || !unsaved} onClick={save}>保存</Button>
      </div>
      {notes.big ? (
        <div style={{ padding: '12px 16px 0' }}>
          <Note tone="gray" icon="info">
            只显示前 {fmtBytes(notes.big.shown)}，完整内容请<a className="link" style={{ cursor: 'pointer' }} onClick={download}>下载原文件</a>
          </Note>
        </div>
      ) : (
        <div style={{ padding: '12px 16px 0' }} className="col">
          {lockInfo ? <LockBar info={lockInfo} lock={lock} /> : null}
          {notes.restored ? <Note icon="history">已恢复上次未保存的草稿（{hhmm(notes.restored.at)}）</Note> : null}
          {notes.stale && !changed ? (
            <div className="note amber">
              <Icon name="history" />
              <div className="grow">{STALE_DRAFT_TEXT}</div>
              <Button size="xs" kind="ghost" onClick={dropMine}>放弃旧草稿</Button>
            </div>
          ) : null}
          {changed ? (
            <div className="note amber">
              <Icon name="alert" />
              <div className="grow">
                <b>文件已被{WHO[changed.by] || '其他人'}修改</b>{changed.version_label ? `（版本 ${changed.version_label}）` : ''}，你的修改还在
              </div>
              <Button size="xs" kind="primary" onClick={() => navigate(pagePath('versions', pid), { path: file.path, from: file.last_change?.commit || '', to: 'WORKTREE' })}>刷新后对比</Button>
              <Button size="xs" kind="ghost" onClick={dropMine}>放弃我的修改</Button>
            </div>
          ) : null}
        </div>
      )}
      <div className="fl-pb">
        <CodeEditor
          key={`${gen}-${readOnly}`}
          className={`fl-ed-cm${readOnly ? ' ro' : ''}`}
          value={text}
          readOnly={readOnly}
          onChange={(v) => { dirty.current = true; setSaved(false); setText(v); }}
        />
      </div>
      <Modal
        open={cancelOpen}
        onClose={() => setCancelOpen(false)}
        icon="alert"
        title="放弃未保存的修改？"
        sub="本次改动会丢掉。"
        w={440}
        foot={<><Button onClick={() => setCancelOpen(false)}>继续编辑</Button><Button kind="danger" onClick={cancel}>放弃修改</Button></>}
      />
    </div>
  );
}
