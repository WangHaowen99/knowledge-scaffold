// 资料分析报告 · 自动核验（设计稿 wb-materials #enVerify）：逐条核对来源位置（R-09），「让助手逐条修正」发命令（S-20）
import { useEffect, useState } from 'react';
import { useApp, useProject } from '../../../app/context.js';
import { errorText } from '../../../api/errors.js';
import {
  Button, Chip, Drawer, Icon, Note, Stat, Table,
} from '../../../ui/index.jsx';
import { pageCommand } from './parts.jsx';
import { VERIFY_STATUS, verificationNote, verificationRows } from './model.js';
import '../../../styles/materials.css';

export function VerificationDrawer({ onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => { api.materialVerification(pid).then(setData).catch(setError); }, [api, pid]);

  const items = data?.items || [];
  const { rows, hidden } = verificationRows(items, { expanded });
  const note = verificationNote(items);
  const fix = async () => {
    const ok = await pageCommand(api, pid, {
      command: '/ontology-research', args: { fix: 'anchors' }, text: '逐条修正来源无法核验的条目', label: '资料分析报告', done: '已发给助手',
    });
    if (ok) onClose();
  };
  return (
    <Drawer
      open onClose={onClose} icon="shieldok" title="资料分析报告 · 自动核验" w={480}
      foot={<><Button onClick={onClose}>关闭</Button><Button kind="primary" disabled={!data?.problems} onClick={fix}>让助手逐条修正</Button></>}
    >
      <div className="en-vf">
        {error ? <Note tone="red">{errorText(error)}</Note> : null}
        {!data && !error ? <div className="muted sm">正在读取核验结果…</div> : null}
        {data ? (
          <>
            <div className="row g12 mb12">
              <Stat label="条目" value={data.total} />
              <Stat label="通过" value={data.passed} />
              <Stat label="有问题" value={data.problems} />
            </div>
            <Table
              compact
              cols={['条目', '来源位置', '状态']}
              rows={rows.map((r) => {
                const s = VERIFY_STATUS[r.status] || VERIFY_STATUS.pending;
                return [r.subject, <span className="mono xs" data-ev={r.anchor}>{r.anchor_label}</span>, <Chip tone={s.tone} sm icon={s.icon}>{s.label}</Chip>];
              })}
            />
            {hidden ? <div className="xs muted mt8"><a className="link" onClick={() => setExpanded(true)}>另外 {hidden} 条通过，展开看</a></div> : null}
            {note ? <div className="mt12"><Note tone="red">{note}</Note></div> : null}
            {!items.length ? <div className="xs muted mt8"><Icon name="info" size={12} /> 资料还没读</div> : null}
          </>
        ) : null}
      </div>
    </Drawer>
  );
}
