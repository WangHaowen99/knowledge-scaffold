// 项目设置 · 危险操作：导出迁移包（P-09）、归档 / 恢复（P-07）、永久删除（P-06，强确认名称，删除后回我的项目）
import { useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Field, Icon, Inp, Modal, Note,
} from '../../ui/index.jsx';

function DeleteDialog({ onClose }) {
  const { api } = useApp();
  const { pid, meta } = useProject();
  const [v, setV] = useState('');
  const [busy, setBusy] = useState(false);
  const name = meta.name;
  const go = async () => {
    setBusy(true);
    try {
      await api.deleteProject(pid, v);
      toast(`已删除「${name}」`, 'trash');
      onClose();
      navigate('/projects');
    } catch (e) {
      toast(errorText(e), 'alert');
      setBusy(false);
    }
  };
  return (
    <Modal
      open onClose={onClose} icon="trash" danger title={`永久删除「${name}」？`} sub="删除后不可恢复。" w={520}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="danger" className="fill" disabled={v !== name || busy} onClick={go}>确认删除</Button></>}
    >
      <Note tone="red">无论项目当前状态如何都会直接删除：进行中的建模会话、排队中的消息和正在运行的自主构建立即强制终止。</Note>
      <div className="mt12 sm muted">工作区文件、对话记录与版本历史将被永久清除。如果只是暂时不用，建议先归档。</div>
      <div className="mt12">
        <Field label="输入项目名称确认">
          <Inp placeholder={name} value={v} autoFocus onChange={(e) => setV(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && v === name && !busy) go(); }} />
        </Field>
      </div>
    </Modal>
  );
}

export function DangerSection({ archived }) {
  const { api, services } = useApp();
  const { pid, meta, setMeta } = useProject();
  const [busy, setBusy] = useState(null);
  const [del, setDel] = useState(false);

  const act = async (key, fn, msg, icon) => {
    setBusy(key);
    try {
      const r = await fn();
      if (r && r.id) setMeta(r);
      toast(msg, icon);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(null);
    }
  };
  const exportPkg = () => act('export', () => services.download(`/projects/${encodeURIComponent(pid)}/transfer-package`, `${meta.name}-transfer.zip`), '已导出迁移包（含版本历史，不含凭据）', 'download');

  return (
    <div className="card" data-sec="danger" style={{ borderColor: '#F3C4C4' }}>
      <div className="card-h"><h3 style={{ color: 'var(--red)' }}>危险操作</h3></div>
      <div className="card-b" style={{ paddingTop: 4 }}>
        <div className="evd-danger">
          <span style={{ color: 'var(--ink3)' }}><Icon name="download" size={18} /></span>
          <div className="grow"><b>导出迁移包</b><div className="sm muted">完整工作区和版本历史，可以在另一套部署里导入；不含对话记录和数据源凭据。</div></div>
          <Button icon="download" disabled={!!busy} onClick={exportPkg}>导出</Button>
        </div>
        {archived ? (
          <div className="evd-danger">
            <span style={{ color: 'var(--ink3)' }}><Icon name="refresh" size={18} /></span>
            <div className="grow"><b>恢复项目</b><div className="sm muted">项目已归档、只读。恢复后回到项目列表，可以继续建模。</div></div>
            <Button icon="refresh" disabled={!!busy} onClick={() => act('archive', () => api.archiveProject(pid, false), '已恢复，可以继续建模', 'refresh')}>恢复</Button>
          </div>
        ) : (
          <div className="evd-danger">
            <span style={{ color: 'var(--ink3)' }}><Icon name="archive" size={18} /></span>
            <div className="grow"><b>归档项目</b><div className="sm muted">归档后只读，不能再建模；可以在项目列表的「已归档」里恢复。</div></div>
            <Button icon="archive" disabled={!!busy} onClick={() => act('archive', () => api.archiveProject(pid, true), '已归档，可在项目列表恢复', 'archive')}>归档</Button>
          </div>
        )}
        <div className="evd-danger">
          <span style={{ color: 'var(--red)' }}><Icon name="trash" size={18} /></span>
          <div className="grow"><b>删除项目</b><div className="sm muted">工作区文件、对话记录和版本历史永久清除，不能恢复。</div></div>
          <Button kind="danger" icon="trash" onClick={() => setDel(true)}>删除项目</Button>
        </div>
      </div>
      {del ? <DeleteDialog onClose={() => setDel(false)} /> : null}
    </div>
  );
}
