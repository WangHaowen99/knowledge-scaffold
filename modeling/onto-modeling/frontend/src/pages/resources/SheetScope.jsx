// 数据 Schema 的 Sheet 范围弹窗（ADR-0096）：元素与文字同 develop LeftPane，样式用新 UI
import { useEffect, useMemo, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { Button, Check, Modal, Note } from '../../ui/index.jsx';
import { sheetScopeFlow } from './sheetScopeModel.js';

// choose(m)：改用途为数据 Schema（先预检，全无问题直接保存）；open(m)：「查看范围」
export function useSheetScope(pid, onSaved) {
  const { api } = useApp();
  const [state, setState] = useState({ editor: null, checking: false, saving: false });
  const saved = useRef(onSaved);
  saved.current = onSaved;
  const flow = useMemo(() => sheetScopeFlow({
    api,
    set: setState,
    onSaved: (r, info) => {
      toast(info.quick ? `已设为数据 Schema，选中 ${info.count} 个数据 Sheet` : 'Sheet 范围已保存；请重新执行受影响的资料分析和字段核验', 'check');
      saved.current?.(r, info);
    },
  }), [api]);
  useEffect(() => { flow.setProject(pid); }, [flow, pid]);
  const dialog = <SheetScopeDialog state={state} flow={flow} />;
  return { choose: flow.choose, open: flow.open, dialog };
}

function SheetScopeDialog({ state, flow }) {
  const { editor, checking, saving } = state;
  const scope = editor?.scope;
  return (
    <Modal
      open={!!editor}
      onClose={() => { if (!saving) flow.cancel(); }}
      sticky={saving}
      icon="table"
      title="数据 Schema 的 Sheet 范围"
      w={560}
      foot={(
        <>
          <span className="sp" />
          <Button disabled={saving} onClick={() => flow.cancel()}>取消</Button>
          <Button kind="primary" disabled={checking || saving || !scope?.file_sha256} onClick={() => flow.confirm()}>
            {saving ? <span className="spin" /> : null}确认用途与范围
          </Button>
        </>
      )}
    >
      <div className="sm b">{editor?.material?.path}</div>
      <p className="sm muted mt8">
        第一行应直接填写字段名，每列名称唯一且不合并。勾选的 Sheet 用于对象字段提取与数据映射，其余作为参考资料。确认后才保存用途，取消不改变原设置，原文件不会改写。
      </p>
      {scope?.error ? <Note tone="amber" className="mt8">{scope.error}</Note> : null}
      <div className="col mt12" style={{ gap: 8 }}>
        {(scope?.sheets || []).map((sheet) => {
          const on = editor.selected.includes(sheet.name);
          const blocked = sheet.schema_selectable === false || sheet.selectable === false;
          return (
            <div key={sheet.name}>
              <Check
                on={on} disabled={checking || saving || blocked} onChange={() => flow.toggle(sheet.name)}
                label={`${sheet.name} · ${on ? '数据结构' : '参考资料'}`}
              />
              {(sheet.issues || []).map((issue, index) => (
                <Note key={`${issue.code}-${index}`} tone={issue.severity === 'error' ? 'red' : 'amber'} className="mt4">
                  {`Sheet「${sheet.name}」：${issue.message}`}
                </Note>
              ))}
            </div>
          );
        })}
      </div>
      {editor && !editor.selected.length ? <Note className="mt12">未选择数据 Sheet：全部作为参考资料，不提供数据表字段。</Note> : null}
      <div className="row mt12">
        <Button size="sm" disabled={checking || saving} onClick={() => flow.recheck()}>
          {checking ? <span className="spin" /> : null}重新检查文件
        </Button>
      </div>
    </Modal>
  );
}
