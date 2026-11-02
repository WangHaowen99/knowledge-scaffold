// 流程图标准编辑器（设计稿 fc）：工具条 + 画布 + 属性面板。编辑会话由宿主（流程图弹窗）持有，
// 保存、校验、切换 drawio 高级编辑也由宿主决定；这里只管编辑。
import { useEffect, useState } from 'react';
import { toast } from '@onto/app/toast.jsx';
import { Button, Icon, Seg, cx } from '@onto/ui/index.jsx';
import FlowchartCanvas from './FlowchartCanvas.jsx';
import FlowchartProps from './FlowchartProps.jsx';
import { nextNodePosition } from './flowchartModel.js';
import { autoLayout } from './layout.js';

const isEditable = (t) => !!t?.closest?.('input, textarea, select, [contenteditable="true"]');

/**
 * @param {object} p
 * @param {object} p.session 编辑会话（flowchartEditorState）
 * @param {(a: object) => void} p.dispatch
 * @param {'std'|'dio'} p.mode
 * @param {(m: 'std'|'dio') => void} p.onModeChange
 * @param {React.ReactNode} p.drawio drawio 高级编辑时盖在画布上的内容
 * @param {boolean} p.readOnly 写入范围锁住时只读
 * @param {() => void} p.onValidate
 * @param {{errors: object[], warnings: object[]}|null} p.findings 校验没通过时的结果
 */
export default function FlowchartEditor({
  session, dispatch, mode = 'std', onModeChange, drawio, readOnly = false, onValidate, validating = false,
  findings = null, onDismissFindings, fitKey = '',
}) {
  const { model, selection } = session;
  const editing = !readOnly && mode === 'std';
  const [tab, setTab] = useState(() => (selection?.type === 'node' ? 'node' : selection?.type === 'edge' ? 'edge' : 'flow'));
  const [layouts, setLayouts] = useState(0);

  // 点选节点 / 连线时切到对应页签；点空白回到流程属性
  const selKey = selection ? `${selection.type}:${selection.id}` : '';
  useEffect(() => {
    setTab(selection?.type === 'node' ? 'node' : selection?.type === 'edge' ? 'edge' : 'flow');
  }, [selKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Delete 删除选中项；Ctrl/Cmd+Z 撤销，Ctrl/Cmd+Shift+Z 或 Ctrl+Y 重做；输入框内不劫持
  useEffect(() => {
    if (!editing) return undefined;
    const h = (e) => {
      if (isEditable(e.target)) return;
      const k = e.key.toLowerCase();
      if ((k === 'delete' || k === 'backspace') && session.selection) { e.preventDefault(); dispatch({ type: 'delete-selection' }); return; }
      if ((e.ctrlKey || e.metaKey) && !e.altKey) {
        if (k === 'z' && !e.shiftKey) { e.preventDefault(); dispatch({ type: 'undo' }); }
        if ((k === 'z' && e.shiftKey) || k === 'y') { e.preventDefault(); dispatch({ type: 'redo' }); }
      }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [editing, session.selection, dispatch]);

  const add = (type) => dispatch({ type: 'add-node', node: { type, label: type === 'decision' ? '新建判断' : '新建步骤' }, position: nextNodePosition(model.nodes) });
  const layout = () => {
    dispatch({ type: 'set-positions', positions: autoLayout(model) });
    setLayouts((v) => v + 1);
    toast('已自动布局', 'grid');
  };

  return (
    <div className="fc">
      <section className="fc-mid">
        <div className="fc-tb">
          <Seg items={[['std', '标准编辑器'], ['dio', 'drawio 高级编辑']]} value={mode} onChange={(m) => m !== mode && onModeChange?.(m)} />
          <span className="vd" />
          <Button size="sm" icon="plus" title="新增业务步骤" disabled={!editing} onClick={() => add('step')}>业务步骤</Button>
          <Button size="sm" icon="plus" title="新增判断节点" disabled={!editing} onClick={() => add('decision')}>判断</Button>
          <Button size="sm" icon="trash" kind="ghost" title="删除选中节点或边（Delete）" disabled={!editing || !selection} onClick={() => dispatch({ type: 'delete-selection' })} />
          <span className="vd" />
          <Button size="sm" icon="undo" kind="ghost" title="撤销（Ctrl+Z）" disabled={!editing || !session.past.length} onClick={() => dispatch({ type: 'undo' })} />
          <Button size="sm" icon="redo" kind="ghost" title="重做（Ctrl+Shift+Z）" disabled={!editing || !session.future.length} onClick={() => dispatch({ type: 'redo' })} />
          <Button size="sm" icon="grid" kind="ghost" title="自动布局" disabled={!editing || !model.nodes.length} onClick={layout} />
          <span className="sp" />
          <Button size="sm" kind="ghost" icon="shieldok" disabled={validating} onClick={onValidate}>校验</Button>
        </div>
        {findings?.errors?.length ? (
          <div className="fc-find">
            <Icon name="alert" size={14} />
            <b>校验没通过：{findings.errors.length} 个错误{findings.warnings?.length ? `，${findings.warnings.length} 个提醒` : ''}</b>
            {findings.errors.slice(0, 3).map((f, i) => {
              const [kind, id] = (f.field || '').split('/');
              const can = (kind === 'nodes' || kind === 'edges') && id;
              return (
                <span
                  key={i} className={cx('it', can && 'go')}
                  onClick={can ? () => dispatch({ type: 'select', selection: { type: kind === 'nodes' ? 'node' : 'edge', id } }) : undefined}
                >
                  {f.message}
                </span>
              );
            })}
            <span className="sp" />
            <Button size="xs" kind="ghost" icon="x" title="收起" onClick={onDismissFindings} />
          </div>
        ) : null}
        <div className="fc-stage">
          <FlowchartCanvas model={model} selection={selection} dispatch={dispatch} disabled={!editing} fitKey={`${fitKey}:${layouts}`} />
          {mode === 'dio' ? <div className="fc-drawio">{drawio}</div> : null}
        </div>
      </section>
      <FlowchartProps model={model} selection={selection} tab={tab} onTab={setTab} dispatch={dispatch} disabled={!editing} />
    </div>
  );
}
