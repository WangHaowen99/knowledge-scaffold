// 流程图条目（图示包的插件产物 diagram.flowchart）：预览缩略、打开编辑（图示包自带的编辑器，与工作台流程图卡同一个）、
// 概况、关联条目、删除（AR-07）、校验（AR-08）；详情走 AR-03
import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Chip, Icon, KV, Modal, St } from '../../ui/index.jsx';
import { FlowPreview, LockMark, askAssistant, useLockCopy } from './parts.jsx';
import { KIND_META, flowchartNodeSummary, hhmm, kindLabel } from './studioModel.js';

const ArtifactOpener = lazy(() => import('../../components/packs/ArtifactOpener.jsx'));
const TYPE = 'diagram.flowchart';
const WHO = { assistant: '助手', web: '你在 Studio', studio: '你在 Studio', mcp: 'MCP 客户端', autobuild: '自主构建' };

function fmtWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return hhmm(iso);
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export default function FlowchartPanel({ row, onDeleted, onChanged }) {
  const { api } = useApp();
  const { pid } = useProject();
  const lockFor = useLockCopy();
  const [art, setArt] = useState(null);
  const [error, setError] = useState(null);
  const [delOpen, setDelOpen] = useState(false);
  const [editing, setEditing] = useState(false);

  const load = useCallback(() => {
    setError(null);
    return api.artifact(pid, TYPE, row.key).then(setArt).catch((e) => { setArt(null); setError(e); });
  }, [api, pid, row.key]);
  useEffect(() => { setArt(null); load(); }, [load]);

  const ref = { type: TYPE, key: row.key, title: row.name };
  const lock = lockFor(ref, row.name);
  const model = art?.content || null;
  const statusKey = (art?.status?.value || row.status) === 'draft' ? 'draft' : 'confirmed';
  const op = model?.operation || null;
  const fields = art?.fields || {};
  const by = fields.updated_by_label || WHO[fields.updated_by] || fields.updated_by || '';

  const validate = async () => {
    try {
      const r = await api.validateArtifact(pid, TYPE, row.key, {});
      const n = `${(r.errors || []).length} 错误，${(r.warnings || []).length} 提醒`;
      toast(r.ok ? `流程图校验通过：${n}` : `流程图校验没通过：${n}`, r.ok ? 'shieldok' : 'alert');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const remove = async () => {
    setDelOpen(false);
    try {
      await api.deleteArtifact(pid, TYPE, row.key, art?.revision);
      toast('已删除流程图', 'trash');
      onDeleted?.();
    } catch (e) {
      toast(errorText(e), e.isLocked ? 'lock' : 'alert');
    }
  };

  return (
    <div className="stt-r card">
      <div className="card-h" style={{ paddingBottom: 12 }}>
        <span style={{ color: 'var(--indigo)' }}><Icon name="branch" size={18} /></span>
        <h3 style={{ fontSize: 16 }}>{art?.title || row.name}</h3>
        <Chip tone="line" sm>流程图</Chip>
        <St k={statusKey} sm />
        <LockMark info={lock} />
        <span className="sp" />
        <Button size="sm" icon="sparkle" onClick={() => askAssistant(pid, `${TYPE}/${row.key}`)}>问助手</Button>
        {/* 写入范围锁住时不能删；按钮禁用后收不到悬停，说明挂在外层 */}
        <span data-pop={lock ? lock.pop : undefined} style={{ display: 'inline-flex' }}>
          <Button size="sm" kind="ghost" icon="trash" title={lock ? undefined : '删除这张流程图'} disabled={!!lock} onClick={() => setDelOpen(true)} />
        </span>
      </div>
      <div className="bd">
        <div className="stt-fcv">
          {model ? <FlowPreview model={model} idPrefix={`fc-${row.key}`} /> : <div className="ph">{error ? `流程图内容暂时读不到：${errorText(error)}` : '正在读取…'}</div>}
        </div>
        <div className="row mt12">
          <Button kind="primary" icon="edit" onClick={() => setEditing(true)}>打开编辑</Button>
        </div>
        <div className="sec-t mt16">概况</div>
        <KV
          w="72px"
          pairs={[
            ['节点', model ? flowchartNodeSummary(model) : '—'],
            ['来源', fields.source_label || fields.source || model?.process?.description || '—'],
            ['最近修改', art?.updated_at ? `${fmtWhen(art.updated_at)}${by ? ` · ${by}` : ''}` : '—'],
            ['被引用', op ? `操作「${op.name}」` : '没有操作引用'],
          ]}
        />
        <div className="sec-t mt16">关联条目</div>
        <div className="row wrap" style={{ gap: 6 }}>
          {(art?.refs || []).map((r) => (
            <Chip key={`${r.id}-${r.field || ''}`} tone={KIND_META[r.kind]?.tone || 'gray'} sm>
              {r.kind === 'rule' || r.kind === 'metric' ? r.name : `${r.name} · ${kindLabel(r.kind)}`}
            </Chip>
          ))}
          {art && !(art.refs || []).length ? <span className="xs muted">还没有关联条目</span> : null}
        </div>
      </div>
      <div className="stt-ft">
        <span className="sp" />
        <Button size="sm" icon="shieldok" onClick={validate}>校验流程图</Button>
      </div>
      <Modal
        open={delOpen}
        onClose={() => setDelOpen(false)}
        icon="trash"
        danger
        title="删除这张流程图？"
        sub="可在版本历史里回退；引用它的操作会提示「流程图缺失」"
        w={440}
        foot={<><Button onClick={() => setDelOpen(false)}>取消</Button><Button kind="danger" onClick={remove}>删除</Button></>}
      />
      {editing ? (
        <Suspense fallback={null}>
          <ArtifactOpener artifact={ref} onClose={() => { setEditing(false); load(); onChanged?.(); }} />
        </Suspense>
      ) : null}
    </div>
  );
}
