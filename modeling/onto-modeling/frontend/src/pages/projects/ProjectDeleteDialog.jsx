// 删除项目（P-06）：名称逐字确认；已发布过的可以勾选「同时删除本项目发布的知识包」，二次确认后先物理删除 Hub 包
// （develop !590 的删除确认元素与文字，ADR-0098）。远端失败项目保留，再次删除可以重试。
// 项目列表卡片菜单与项目设置的危险区共用。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Check, Field, Inp, Modal, Note } from '../../ui/index.jsx';
import {
  DELETE_TEXT, HUB_CHECKBOX, HUB_CONFIRM_TEXT, HUB_CONFIRM_TITLE, HUB_DELETE_BUTTON, HUB_USAGE_CHECKBOX, packageLabel,
  previewFailed, progressNote, unpublishedNote,
} from './projectDeleteModel.js';

export default function ProjectDeleteDialog({ project, title, onClose, onDeleted }) {
  const { api } = useApp();
  const [name, setName] = useState('');
  const [preview, setPreview] = useState(null);
  const [deleteHub, setDeleteHub] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setName(''); setPreview(null); setDeleteHub(false); setConfirming(false); setAcknowledged(false);
    if (!project) return undefined;
    let alive = true;
    api.hubDeletePreview(project.id)
      .then((result) => { if (alive) setPreview(result); })
      .catch((e) => { if (alive) setPreview(previewFailed(errorText(e))); });
    return () => { alive = false; };
  }, [api, project]);
  if (!project) return null;

  const perform = async (withHub) => {
    setBusy(true);
    try {
      await api.deleteProject(project.id, name, { deleteHubPackage: withHub });
      toast(`已删除「${project.name}」`, 'trash');
      onClose();
      onDeleted?.();
    } catch (e) {
      toast(errorText(e) || '删除失败', 'alert');
    } finally {
      setBusy(false);
    }
  };
  const confirm = () => (deleteHub ? setConfirming(true) : perform(false));
  const nameOk = name === project.name;
  const progress = progressNote(preview);
  const unpublished = unpublishedNote(preview);

  if (confirming) {
    return (
      <Modal open onClose={() => setConfirming(false)} icon="trash" danger title={HUB_CONFIRM_TITLE} w={560}
        foot={<><Button onClick={() => setConfirming(false)}>取消</Button>
          <Button kind="danger" className="fill" disabled={!acknowledged || busy} onClick={() => perform(true)}>{HUB_DELETE_BUTTON}</Button></>}>
        <div className="b">{packageLabel(preview)}</div>
        <Note tone="red" className="mt12">{HUB_CONFIRM_TEXT}</Note>
        <div className="mt12"><Check on={acknowledged} onChange={setAcknowledged} label={HUB_USAGE_CHECKBOX} /></div>
      </Modal>
    );
  }
  return (
    <Modal open onClose={onClose} icon="trash" danger title={title || `删除「${project.name}」？`} sub="这个操作不可恢复" w={540}
      foot={<><Button onClick={onClose}>取消</Button>
        <Button kind="danger" className="fill" icon="trash" disabled={!nameOk || busy || !preview} onClick={confirm}>确认删除</Button></>}>
      <Note tone="red">{DELETE_TEXT}</Note>
      <div className="mt12">
        <Check on={deleteHub} onChange={setDeleteHub} label={HUB_CHECKBOX} disabled={!preview?.published} />
        {unpublished ? <div className="xs muted mt4">{unpublished}</div> : null}
        {progress ? <div className="xs mt4">{progress}</div> : null}
      </div>
      <div className="mt16">
        <Field label="输入项目名称确认删除">
          <Inp icon="edit" placeholder={project.name} value={name} autoFocus onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && nameOk && !busy) confirm(); }} />
        </Field>
      </div>
    </Modal>
  );
}
