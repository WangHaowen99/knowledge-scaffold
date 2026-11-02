// 插件产物的通用详情（详设 5.9.5 第一档）：按登记的字段与分段显示，表格类给预览；不带包的代码。
import { useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import { Chip, KV, Modal, Note, Table } from '../../ui/index.jsx';
import { Md } from '../../pages/workbench/parts.jsx';

function fieldText(value) {
  if (value == null || value === '') return '';
  if (Array.isArray(value)) return value.map(fieldText).filter(Boolean).join('、');
  if (typeof value === 'object') return value.name || value.title || value.key || '';
  return String(value);
}

export default function ArtifactDetail({ type, artifact, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [art, setArt] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    api.artifact(pid, type.type, artifact.key).then(setArt).catch(setError);
  }, [api, pid, type.type, artifact.key]);

  const fields = (type.schema?.fields || [])
    .map((f) => [f.label || f.name, fieldText(art?.fields?.[f.name])])
    .filter(([, v]) => v);
  const sections = Object.entries(art?.sections || {}).filter(([, text]) => String(text || '').trim());
  return (
    <Modal open onClose={onClose} icon="layers" title={art?.title || artifact.title || artifact.key} sub={type.title} w={720}>
      {error ? <Note tone="red">{errorText(error)}</Note> : null}
      {!art && !error ? <div className="pk-load" role="status"><span className="spin" /></div> : null}
      {art ? (
        <>
          {art.status ? <div className="row mb8"><Chip tone={art.status.tone || 'gray'} sm>{art.status.label}</Chip></div> : null}
          {fields.length ? <KV pairs={fields} w="96px" /> : null}
          {sections.map(([title, text]) => (
            <div key={title} className="mt12">
              <div className="b sm">{title}</div>
              <Md text={text} />
            </div>
          ))}
          {art.table ? (
            <div className="mt12">
              <Table compact cols={art.table.columns} rows={art.table.rows} />
            </div>
          ) : null}
        </>
      ) : null}
    </Modal>
  );
}
