import { useState } from 'react';
import type { JsonSchema } from './backend';
import { Field, NumberField, Switch } from './ui';

export const SECRET_SET = '******';

type Value = Record<string, unknown>;

function typeOf(schema: JsonSchema): string {
  const t = Array.isArray(schema.type) ? schema.type.find((x) => x !== 'null') : schema.type;
  return t ?? (schema.properties ? 'object' : 'string');
}

function rangeProblem(schema: JsonSchema, value: number): string | null {
  if (schema.minimum !== undefined && value < schema.minimum) return `不能小于 ${schema.minimum}`;
  if (schema.maximum !== undefined && value > schema.maximum) return `不能大于 ${schema.maximum}`;
  if (typeOf(schema) === 'integer' && !Number.isInteger(value)) return '应为整数';
  return null;
}

/** Values outside the schema's bounds, as messages; a form with any is not sent. */
export function schemaProblems(schema: JsonSchema | null | undefined, value: Value): string[] {
  const out: string[] = [];
  for (const [key, prop] of Object.entries(schema?.properties ?? {})) {
    const type = typeOf(prop);
    const v = value[key];
    if (type === 'object') out.push(...schemaProblems(prop, (v as Value) ?? {}));
    else if ((type === 'integer' || type === 'number') && typeof v === 'number') {
      const problem = rangeProblem(prop, v);
      if (problem) out.push(`${prop.title ?? key}${problem}`);
    }
  }
  return out;
}

export function schemaDefaults(schema: JsonSchema | null | undefined): Value {
  const out: Value = {};
  for (const [key, prop] of Object.entries(schema?.properties ?? {})) {
    if (typeOf(prop) === 'object') out[key] = schemaDefaults(prop);
    else if (prop.default !== undefined) out[key] = prop.default;
  }
  return out;
}

function FieldFor({ name, schema, value, onChange }: { name: string; schema: JsonSchema; value: unknown; onChange: (v: unknown) => void }) {
  const label = schema.title ?? name;
  const hint = schema.description;
  const type = typeOf(schema);
  if (schema.enum) {
    const labels = schema['x-enum-labels'] ?? {};
    // nothing set and no default: an empty choice, so what is shown is what will be sent (nothing)
    const blank = (value === undefined || value === '') && schema.default === undefined && !schema.enum.includes('');
    return (
      <Field label={label} hint={hint}>
        <select
          className="select"
          value={String(value ?? '')}
          onChange={(e) => onChange(e.target.value === '' && blank ? undefined : type === 'string' ? e.target.value : Number(e.target.value))}
        >
          {blank ? <option value="">—</option> : null}
          {schema.enum.map((option) => (
            <option key={String(option)} value={String(option)}>
              {labels[String(option)] ?? String(option)}
            </option>
          ))}
        </select>
      </Field>
    );
  }
  if (type === 'boolean') return <Switch checked={Boolean(value)} onChange={onChange} label={label} hint={hint} />;
  if (type === 'integer' || type === 'number') {
    const number = typeof value === 'number' ? value : Number.NaN;
    return (
      <NumberField
        label={label}
        hint={hint}
        error={Number.isFinite(number) ? rangeProblem(schema, number) : null}
        value={number}
        // cleared: left out, so the backend keeps its own default
        onChange={(v) => onChange(Number.isFinite(v) ? v : undefined)}
        min={schema.minimum}
        max={schema.maximum}
        step={type === 'integer' ? 1 : 0.01}
      />
    );
  }
  if (type === 'object') {
    return (
      <section className="cfg-group">
        <h4 className="cfg-group-title">{label}</h4>
        {hint ? <div className="field-hint" style={{ marginTop: -6, marginBottom: 10 }}>{hint}</div> : null}
        <SchemaForm schema={schema} value={(value as Value) ?? {}} onChange={onChange} />
      </section>
    );
  }
  if (schema['x-secret']) return <SecretField label={label} hint={hint} value={value} onChange={onChange} />;
  const long = (schema.maxLength ?? 0) > 200;
  return (
    <Field label={label} hint={hint}>
      {long ? (
        <textarea className="textarea" value={String(value ?? '')} maxLength={schema.maxLength} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input className="input" value={String(value ?? '')} maxLength={schema.maxLength} onChange={(e) => onChange(e.target.value)} />
      )}
    </Field>
  );
}

/** Write-only: a value already set reads back as SECRET_SET and stays so (unchanged) until something is typed. */
function SecretField({ label, hint, value, onChange }: { label: string; hint?: string; value: unknown; onChange: (v: unknown) => void }) {
  const [wasSet] = useState(value === SECRET_SET);
  return (
    <Field label={label} hint={hint}>
      <input
        className="input"
        type="password"
        autoComplete="new-password"
        placeholder={wasSet ? '已设置' : '未设置'}
        value={value === SECRET_SET ? '' : String(value ?? '')}
        onChange={(e) => onChange(e.target.value === '' ? (wasSet ? SECRET_SET : undefined) : e.target.value)}
      />
    </Field>
  );
}

function withValue(value: Value, key: string, v: unknown): Value {
  const next = { ...value, [key]: v };
  if (v === undefined) delete next[key];
  return next;
}

/** Settings form for a backend's private config, drawn from the JSON Schema it publishes in its capabilities. */
export default function SchemaForm({ schema, value, onChange }: { schema: JsonSchema; value: Value; onChange: (v: Value) => void }) {
  const props = Object.entries(schema.properties ?? {});
  if (!props.length) return <div className="field-hint">这个知识后端没有可调整的设置。</div>;
  return (
    <>
      {props.map(([key, prop]) => (
        <FieldFor key={key} name={key} schema={prop} value={value[key]} onChange={(v) => onChange(withValue(value, key, v))} />
      ))}
    </>
  );
}
