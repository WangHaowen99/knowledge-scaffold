import { useEffect, useState } from 'react';
import SchemaForm, { schemaDefaults, schemaProblems } from './SchemaForm';
import type { BackendInfo, JsonSchema } from './backend';
import { rpc } from './rpc';
import { errorText, useToast } from './toast';
import { Button, Spinner } from './ui';

type Value = Record<string, unknown>;

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Backend-wide settings (contract 9.4), drawn from the backend's ``settings_schema``; only changed fields are sent. */
export default function BackendSettings({ backend, schema }: { backend: BackendInfo; schema: JsonSchema }) {
  const notify = useToast();
  const [initial, setInitial] = useState<Value | null>(null);
  const [value, setValue] = useState<Value>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);

  const accept = (current: Value | null) => {
    const merged = { ...schemaDefaults(schema), ...(current ?? {}) };
    setInitial(merged);
    setValue(merged);
    // secret fields remember whether they were set when mounted
    setVersion((v) => v + 1);
  };

  useEffect(() => {
    rpc
      .call<Value>('settings.get', { backend: backend.id })
      .then(accept)
      .catch((err) => setError(errorText(err)));
    // schema arrives with the backend; reloading on its id is enough
  }, [backend.id]);

  // a cleared field is left as the backend has it
  const changed = initial ? Object.keys(value).filter((k) => value[k] !== undefined && !same(value[k], initial[k])) : [];
  const invalid = schemaProblems(schema, value).length > 0;

  const save = async () => {
    setBusy(true);
    try {
      const values = Object.fromEntries(changed.map((k) => [k, value[k]]));
      accept(await rpc.call<Value>('settings.put', { backend: backend.id, values }));
      notify('已保存');
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="backend-settings">
      <h3 className="section-title">{backend.label}</h3>
      <div className="field-hint backend-settings-hint">对该后端上的全部知识库生效</div>
      {error ? (
        <div className="error-box" style={{ marginBottom: 14 }}>
          {error}
        </div>
      ) : initial === null ? (
        <div className="empty" style={{ padding: 16 }}>
          <Spinner />
        </div>
      ) : (
        <>
          <SchemaForm key={version} schema={schema} value={value} onChange={setValue} />
          <div className="row-inline" style={{ marginBottom: 14 }}>
            <Button variant="primary" disabled={!changed.length || invalid || busy} onClick={save}>
              {busy ? <Spinner /> : null}保存
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
