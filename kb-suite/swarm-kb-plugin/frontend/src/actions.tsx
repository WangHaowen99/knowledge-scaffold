import { useState } from 'react';
import SchemaForm, { schemaDefaults, schemaProblems } from './SchemaForm';
import { useKbScope, type ActionDecl } from './backend';
import { rpc } from './rpc';
import { errorText, useToast } from './toast';
import { Button, Menu, Modal, Spinner } from './ui';
import { ContentView } from './views';

type Target = { kb_id: string } | { doc_id: string };
type Outcome = { job?: unknown; result?: unknown };

const hasParams = (action: ActionDecl) => Object.keys(action.params_schema?.properties ?? {}).length > 0;

const worthShowing = (v: unknown) =>
  typeof v === 'string' ? v.trim() !== '' : typeof v === 'object' && v !== null && Object.keys(v).length > 0;

function ActionDialog({ action, onClose, onRun }: { action: ActionDecl; onClose: () => void; onRun: (params: Record<string, unknown>) => void }) {
  const schema = action.params_schema ?? null;
  const [params, setParams] = useState<Record<string, unknown>>(() => schemaDefaults(schema));
  const invalid = schemaProblems(schema, params).length > 0;
  return (
    <Modal
      title={action.label}
      width={hasParams(action) ? 560 : 420}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" disabled={invalid} onClick={() => onRun(params)}>
            执行
          </Button>
        </>
      }
    >
      {action.description ? <div className="field-hint action-desc">{action.description}</div> : null}
      {schema && hasParams(action) ? <SchemaForm schema={schema} value={params} onChange={setParams} /> : null}
      {!action.description && !hasParams(action) ? <div className="action-desc">确定执行「{action.label}」？</div> : null}
    </Modal>
  );
}

/**
 * One button per declared action, or one menu when there are more than ``inline``. ``confirm`` or parameters
 * open a dialog first; an action that runs as a job is handed to the jobs list, a direct result worth reading
 * is shown.
 */
export default function ActionButtons({
  actions,
  target,
  onDone,
  inline = Infinity,
}: {
  actions: ActionDecl[];
  target: Target;
  onDone: () => void;
  inline?: number;
}) {
  const notify = useToast();
  const scope = useKbScope();
  const [pending, setPending] = useState<ActionDecl | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [shown, setShown] = useState<{ label: string; result: unknown } | null>(null);
  if (!actions.length) return null;

  const run = async (action: ActionDecl, params: Record<string, unknown>) => {
    setPending(null);
    setBusy(action.id);
    try {
      const res = await rpc.call<Outcome>('actions.run', { ...target, action: action.id, params }, 600000);
      if (res?.job) {
        notify(`${action.label}已开始`);
        scope?.refreshJobs();
      } else {
        notify(`${action.label}已完成`);
        if (worthShowing(res?.result)) setShown({ label: action.label, result: res.result });
      }
      onDone();
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(null);
    }
  };

  const start = (action: ActionDecl) => (action.confirm || hasParams(action) ? setPending(action) : void run(action, {}));

  return (
    <>
      {actions.length > inline ? (
        <Menu
          label="操作"
          align="right"
          icon={busy ? <Spinner /> : undefined}
          items={actions.map((action) => ({ key: action.id, label: action.label, title: action.description, onSelect: () => start(action) }))}
        />
      ) : null}
      {(actions.length > inline ? [] : actions).map((action) => (
        <Button
          key={action.id}
          size="sm"
          title={action.description}
          disabled={busy !== null}
          icon={busy === action.id ? <Spinner /> : undefined}
          onClick={() => start(action)}
        >
          {action.label}
        </Button>
      ))}
      {pending ? <ActionDialog action={pending} onClose={() => setPending(null)} onRun={(params) => void run(pending, params)} /> : null}
      {shown ? (
        <Modal title={shown.label} width={720} onClose={() => setShown(null)}>
          <div className="report-body">
            <ContentView content={shown.result} />
          </div>
        </Modal>
      ) : null}
    </>
  );
}
