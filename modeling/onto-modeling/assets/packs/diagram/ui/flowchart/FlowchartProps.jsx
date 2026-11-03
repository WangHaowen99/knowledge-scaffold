// 流程图属性面板：流程属性 / 节点属性 / 连线属性三个页签（设计稿 fc-props）
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Chip, Field, Inp, Note, Select, Tabs } from '@onto/ui/index.jsx';
import { EDGE_TYPE_LABEL, NODE_TYPE_LABEL, listToText, textToList } from './flowchartModel.js';

const NODE_OPTIONS = ['step', 'decision', 'data', 'start', 'end'].map((value) => ({ value, label: NODE_TYPE_LABEL[value] }));
const EDGE_OPTIONS = ['seq', 'cond', 'exc', 'data'].map((value) => ({ value, label: EDGE_TYPE_LABEL[value] }));
const REF_KIND = { object: ['对象', 'blue'], operation: ['操作', 'teal'], rule: ['规则', 'amber'], metric: ['指标', 'indigo'], script: ['脚本', 'gray'] };

// 文本框按内容长高，最小高度同设计稿
function AutoArea({ value, onChange, minHeight, disabled, placeholder, label }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const t = ref.current;
    if (!t) return;
    t.style.height = '0px';
    t.style.height = `${t.scrollHeight}px`;
  }, [value]);
  return (
    <div className="inp area fc-area" style={{ minHeight }}>
      <textarea ref={ref} rows={1} value={value} disabled={disabled} placeholder={placeholder} aria-label={label} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

// 每行一项的列表：输入时保留原文（含空行），模型里存去掉空行后的列表
function ListArea({ list, onChange, ...rest }) {
  const [text, setText] = useState(listToText(list));
  useEffect(() => {
    if (listToText(textToList(text)) !== listToText(list)) setText(listToText(list));
  }, [list]); // eslint-disable-line react-hooks/exhaustive-deps
  return <AutoArea {...rest} value={text} onChange={(v) => { setText(v); onChange(textToList(v)); }} />;
}

export function selectionLabel(model, selection) {
  if (!selection) return '';
  if (selection.type === 'node') return model.nodes.find((n) => n.id === selection.id)?.label || '';
  const e = model.edges.find((x) => x.id === selection.id);
  if (!e) return '';
  const name = (id) => model.nodes.find((n) => n.id === id)?.label || '';
  return `${name(e.from)} → ${name(e.to)}`;
}

function FlowPane({ model, dispatch, disabled }) {
  const p = model.process;
  const up = (key, value) => dispatch({ type: 'update-process', patch: { [key]: value }, coalesce: key });
  return (
    <div className="col" style={{ gap: 12 }}>
      <Field label="流程名称"><Inp value={p.name} disabled={disabled} aria-label="流程名称" onChange={(e) => up('name', e.target.value)} /></Field>
      <Field label="流程说明"><AutoArea label="流程说明" value={p.description} minHeight={64} disabled={disabled} onChange={(v) => up('description', v)} /></Field>
      <Field label="参与角色"><ListArea label="参与角色" placeholder="每行一项" list={p.roles} minHeight={48} disabled={disabled} onChange={(v) => up('roles', v)} /></Field>
      <Field label="流程输入"><ListArea label="流程输入" placeholder="每行一项" list={p.inputs} minHeight={48} disabled={disabled} onChange={(v) => up('inputs', v)} /></Field>
      <Field label="异常处理"><ListArea label="异常处理" placeholder="每行一项" list={p.exceptions} minHeight={40} disabled={disabled} onChange={(v) => up('exceptions', v)} /></Field>
      <Field label="流程交接"><ListArea label="流程交接" placeholder="每行一项" list={p.handoffs} minHeight={40} disabled={disabled} onChange={(v) => up('handoffs', v)} /></Field>
    </div>
  );
}

function NodePane({ node, dispatch, disabled }) {
  if (!node) return <div className="fc-empty">未选中节点</div>;
  const up = (patch, coalesce) => dispatch({ type: 'update-node', id: node.id, patch, coalesce });
  return (
    <div className="col" style={{ gap: 12 }}>
      <Field label="节点名称">
        <Inp value={node.label} disabled={disabled} aria-label="节点名称" onChange={(e) => up({ label: e.target.value }, 'label')} />
      </Field>
      <Field label="节点类型">
        <Select value={node.type} options={NODE_OPTIONS} disabled={disabled} onChange={(v) => up({ type: v })} />
      </Field>
      <Field label="业务说明">
        <AutoArea label="业务说明" value={node.description} minHeight={72} disabled={disabled} onChange={(v) => up({ description: v }, 'description')} />
      </Field>
      {node.pending ? (
        <Note tone="amber" icon="hand">
          {node.pending_note ? `口径待定（${node.pending_note}），在对话里等你决定` : '口径待定，在对话里等你决定'}
        </Note>
      ) : null}
      {node.refs?.length ? (
        <div className="card pad" style={{ background: 'var(--panel2)' }}>
          <div className="xs muted">关联条目</div>
          <div className="row wrap mt8" style={{ gap: 6 }}>
            {node.refs.map((r) => {
              const [k, tone] = REF_KIND[r.kind] || ['条目', 'gray'];
              return <Chip key={r.id} tone={tone} sm>{r.name} · {k}</Chip>;
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function EdgePane({ edge, dispatch, disabled }) {
  if (!edge) return <div className="fc-empty">未选中连线</div>;
  const up = (patch, coalesce) => dispatch({ type: 'update-edge', id: edge.id, patch, coalesce });
  return (
    <div className="col" style={{ gap: 12 }}>
      <Field label="图上文字">
        <Inp value={edge.label} disabled={disabled} aria-label="图上文字" placeholder="如：是、否、超出范围" onChange={(e) => up({ label: e.target.value }, 'label')} />
      </Field>
      <Field label="流转类型">
        <Select value={edge.type} options={EDGE_OPTIONS} disabled={disabled} onChange={(v) => up({ type: v })} />
      </Field>
      <Field label="业务逻辑或条件">
        <AutoArea label="业务逻辑或条件" value={edge.condition} minHeight={64} disabled={disabled} onChange={(v) => up({ condition: v }, 'condition')} />
      </Field>
    </div>
  );
}

/**
 * @param {object} p
 * @param {'flow'|'node'|'edge'} p.tab
 */
export default function FlowchartProps({ model, selection, tab, onTab, dispatch, disabled }) {
  const node = selection?.type === 'node' ? model.nodes.find((n) => n.id === selection.id) : null;
  const edge = selection?.type === 'edge' ? model.edges.find((e) => e.id === selection.id) : null;
  const sel = selectionLabel(model, selection);
  return (
    <aside className="fc-props">
      <div style={{ padding: '14px 16px 0' }}>
        <div className="row"><b>属性</b><span className="sp" /><span className="xs muted ell">{sel ? `选中：${sel}` : '未选中'}</span></div>
        <div className="mt8"><Tabs items={[['flow', '流程属性'], ['node', '节点属性'], ['edge', '连线属性']]} value={tab} onChange={onTab} /></div>
      </div>
      <div className="bd">
        {tab === 'flow' ? <FlowPane model={model} dispatch={dispatch} disabled={disabled} /> : null}
        {tab === 'node' ? <NodePane key={node?.id} node={node} dispatch={dispatch} disabled={disabled} /> : null}
        {tab === 'edge' ? <EdgePane key={edge?.id} edge={edge} dispatch={dispatch} disabled={disabled} /> : null}
      </div>
    </aside>
  );
}
