// 数据 Schema 的 Sheet 范围弹窗（ADR-0096）：勾选的 Sheet 当数据表，其余只作参考；表头有错误的不能勾
import { useEffect, useMemo, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Check, Chip, Modal, Note } from '../../ui/index.jsx';
import { sheetScopeFlow } from './sheetScopeModel.js';

// choose(m)：改用途为数据 Schema（先预检，没问题直接保存）；edit(m)：看或改已标 Schema 的范围
export function useSheetScope(pid, onSaved) {
  const { api } = useApp();
  const [state, setState] = useState({ editor: null, checking: false, saving: false });
  const saved = useRef(onSaved);
  saved.current = onSaved;
  const flow = useMemo(() => sheetScopeFlow({
    api,
    set: setState,
    onSaved: (r, scope) => saved.current?.(r, scope),
    onError: (e) => toast(errorText(e), 'alert'),
  }), [api]);
  useEffect(() => { flow.setProject(pid); }, [flow, pid]);
  const dialog = <SheetScopeDialog state={state} flow={flow} />;
  return { choose: flow.choose, edit: flow.edit, dialog, checking: state.checking };
}

function SheetScopeDialog({ state, flow }) {
  const { editor, checking, saving } = state;
  const busy = checking || saving;
  const scope = editor?.scope;
  return (
    <Modal
      open={!!editor}
      onClose={() => { if (!saving) flow.cancel(); }}
      sticky={saving}
      icon="table"
      title="Sheet 范围"
      sub={editor?.material?.name}
      foot={(
        <>
          <Button size="sm" kind="ghost" disabled={busy} onClick={() => flow.recheck()}>{checking ? '检查中…' : '重新检查'}</Button>
          <span className="sp" />
          <Button size="sm" disabled={saving} onClick={() => flow.cancel()}>取消</Button>
          <Button size="sm" kind="primary" disabled={!flow.canSave()} onClick={() => flow.confirm()}>{saving ? '保存中…' : '确认'}</Button>
        </>
      )}
    >
      {scope?.error ? <Note tone="amber">{scope.error}</Note> : null}
      <div className="col" style={{ gap: 10 }}>
        {(scope?.sheets || []).map((sheet) => {
          const on = editor.selected.includes(sheet.name);
          const blocked = sheet.schema_selectable === false || sheet.selectable === false;
          return (
            <div key={sheet.name}>
              <div className="row g8">
                <Check on={on} disabled={busy || blocked} onChange={() => flow.toggle(sheet.name)} label={sheet.name} />
                <span className="sp" />
                <Chip tone={on ? 'blue' : 'line'} sm>{on ? '数据表' : '参考'}</Chip>
              </div>
              {(sheet.issues || []).map((issue, i) => (
                <div key={`${issue.code}-${i}`} className="xs mt4" style={{ color: issue.severity === 'error' ? 'var(--red)' : 'var(--amber)' }}>
                  {issue.message}
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </Modal>
  );
}
