import { Trash2 } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import SchemaForm, { schemaDefaults, schemaProblems } from './SchemaForm';
import { ADJUSTED_NOTICE, saveKb } from './SettingsTab';
import { has, type BackendInfo, type JsonSchema } from './backend';
import { rpc } from './rpc';
import { errorText, useToast } from './toast';
import type { KnowledgeBase } from './types';
import { Button, Field, Spinner } from './ui';

const withDefaults = (schema: JsonSchema | null, kb: KnowledgeBase): Record<string, unknown> => ({
  ...schemaDefaults(schema),
  ...((kb.config as unknown as Record<string, unknown>) ?? {}),
});

/** Settings of a knowledge base on a backend other than kb-service: name, description and its schema-described config. */
export default function GenericSettings({
  kb,
  backend,
  onSaved,
  onDeleted,
}: {
  kb: KnowledgeBase;
  backend: BackendInfo | undefined;
  onSaved: () => void;
  onDeleted: () => void;
}) {
  const notify = useToast();
  const schema = backend?.config_schema ?? null;
  // what the server holds: the list's copy, or what was read back right after saving
  const [base, setBase] = useState<KnowledgeBase>(kb);
  useEffect(() => setBase(kb), [kb]);
  const initial = useMemo(() => withDefaults(schema, base), [schema, base]);
  const [name, setName] = useState(kb.name);
  const [description, setDescription] = useState(kb.description);
  const [config, setConfig] = useState<Record<string, unknown>>(initial);
  const [busy, setBusy] = useState(false);
  const writable = has(kb, 'kbs.write');
  const dirty = name !== base.name || description !== base.description || JSON.stringify(config) !== JSON.stringify(initial);
  const invalid = schemaProblems(schema, config).length > 0;

  const save = async () => {
    setBusy(true);
    try {
      const sent = JSON.stringify(config);
      const saved = await saveKb(kb.id, { name: name.trim(), description, ...(schema ? { config } : {}) });
      if (saved) {
        setBase(saved);
        setName(saved.name);
        setDescription(saved.description);
        setConfig(withDefaults(schema, saved));
      }
      notify(schema && saved && JSON.stringify(withDefaults(schema, saved)) !== sent ? ADJUSTED_NOTICE : '已保存');
      onSaved();
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`删除知识库「${kb.name}」及其全部文档？删除后不可恢复。`)) return;
    try {
      await rpc.call('kbs.delete', { kb_id: kb.id });
      notify('已删除');
      onDeleted();
    } catch (err) {
      notify(errorText(err), true);
    }
  };

  return (
    <div className="settings-body">
      <div className="field-hint" style={{ marginBottom: 12 }}>
        这个知识库在「{kb.backend?.label ?? '外部知识后端'}」上，解析、分块和检索参数由该后端管理。
      </div>
      <Field label="名称">
        <input className="input" value={name} maxLength={100} disabled={!writable} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="说明" hint="写清楚这个知识库装了什么，agent 会据此判断要不要检索它。">
        <textarea className="textarea" value={description} disabled={!writable} onChange={(e) => setDescription(e.target.value)} />
      </Field>
      {schema && writable ? <SchemaForm schema={schema} value={config} onChange={setConfig} /> : null}
      {writable ? (
        <div className="row-inline" style={{ marginTop: 16 }}>
          <Button variant="primary" disabled={!dirty || !name.trim() || invalid || busy} onClick={save}>
            {busy ? <Spinner /> : null}保存
          </Button>
          <span className="grow" />
          <Button variant="danger" icon={<Trash2 size={14} />} onClick={remove}>
            删除知识库
          </Button>
        </div>
      ) : (
        <div className="field-hint">这个知识后端以只读方式接入，不能在这里修改。</div>
      )}
    </div>
  );
}
