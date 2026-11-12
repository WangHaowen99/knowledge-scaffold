// 项目设置 · 危险操作：导出迁移包（P-09）、归档 / 恢复（P-07）、永久删除（P-06，强确认名称，可一并删除 Hub 包，删除后回我的项目）
import { useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import ProjectDeleteDialog from '../projects/ProjectDeleteDialog.jsx';
import { Button, Icon } from '../../ui/index.jsx';

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
  const exportPkg = () => act('export', () => services.download(`/projects/${encodeURIComponent(pid)}/transfer-package`, `${meta.name}-transfer.zip`), '已导出迁移包', 'download');

  return (
    <div className="card" data-sec="danger" style={{ borderColor: '#F3C4C4' }}>
      <div className="card-h"><h3 style={{ color: 'var(--red)' }}>危险操作</h3></div>
      <div className="card-b" style={{ paddingTop: 4 }}>
        <div className="evd-danger">
          <span style={{ color: 'var(--ink3)' }}><Icon name="download" size={18} /></span>
          <div className="grow"><b>导出迁移包</b><div className="sm muted">含工作区和版本历史，不含对话记录</div></div>
          <Button icon="download" disabled={!!busy} onClick={exportPkg}>导出</Button>
        </div>
        {archived ? (
          <div className="evd-danger">
            <span style={{ color: 'var(--ink3)' }}><Icon name="refresh" size={18} /></span>
            <div className="grow"><b>恢复项目</b><div className="sm muted">恢复后可以继续建模</div></div>
            <Button icon="refresh" disabled={!!busy} onClick={() => act('archive', () => api.archiveProject(pid, false), '已恢复', 'refresh')}>恢复</Button>
          </div>
        ) : (
          <div className="evd-danger">
            <span style={{ color: 'var(--ink3)' }}><Icon name="archive" size={18} /></span>
            <div className="grow"><b>归档项目</b><div className="sm muted">归档后只读，可以恢复</div></div>
            <Button icon="archive" disabled={!!busy} onClick={() => act('archive', () => api.archiveProject(pid, true), '已归档', 'archive')}>归档</Button>
          </div>
        )}
        <div className="evd-danger">
          <span style={{ color: 'var(--red)' }}><Icon name="trash" size={18} /></span>
          <div className="grow"><b>删除项目</b><div className="sm muted">工作区文件、对话记录和版本历史永久清除，不能恢复。</div></div>
          <Button kind="danger" icon="trash" onClick={() => setDel(true)}>删除项目</Button>
        </div>
      </div>
      {del ? <ProjectDeleteDialog project={{ id: pid, name: meta.name }} title={`永久删除「${meta.name}」？`} onClose={() => setDel(false)} onDeleted={() => navigate('/projects')} /> : null}
    </div>
  );
}
