// 对比右侧：当前文件，可直接改（设计稿 .mr-cmp .c.ed）。CodeMirror 只做编辑，样式仿左侧的行列表：
// 行号 34px、等宽 12px、行高 1.75；和左侧（历史版本）相比新增或改过的行整行标绿，边改边重算。
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { EditorState, RangeSet, RangeSetBuilder, StateField } from '@codemirror/state';
import {
  Decoration, EditorView, GutterMarker, gutterLineClass, keymap, lineNumbers,
} from '@codemirror/view';
import { useEffect, useRef } from 'react';
import { addedLines } from './versionsModel.js';

class AddedMarker extends GutterMarker {
  get elementClass() { return 'cmp-a'; }
}
const ADDED_LINE = Decoration.line({ class: 'cmp-a' });
const ADDED_GUTTER = new AddedMarker();

function marks(state, left) {
  const deco = new RangeSetBuilder();
  const gut = new RangeSetBuilder();
  addedLines(left, state.doc.toString()).forEach((n) => {
    if (n > state.doc.lines) return;
    const from = state.doc.line(n).from;
    deco.add(from, from, ADDED_LINE);
    gut.add(from, from, ADDED_GUTTER);
  });
  return { deco: deco.finish(), gut: gut.finish() };
}

// 每个编辑器一份：左侧内容固定，文档变了就重算标绿的行
const addedField = (left) => StateField.define({
  create: (state) => marks(state, left),
  update: (v, tr) => (tr.docChanged ? marks(tr.state, left) : v),
  provide: (f) => [EditorView.decorations.from(f, (v) => v.deco), gutterLineClass.from(f, (v) => v.gut || RangeSet.empty)],
});

const THEME = EditorView.theme({
  '&': { fontSize: '12px', backgroundColor: '#fff' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { fontFamily: 'var(--mono)', lineHeight: '1.75', overflow: 'visible' },
  '.cm-content': { padding: '0', caretColor: 'var(--blue)' },
  '.cm-line': { padding: '0 10px 0 0' },
  '.cm-gutters': { backgroundColor: 'transparent', border: 'none', color: 'var(--muted)' },
  '.cm-lineNumbers .cm-gutterElement': { minWidth: '34px', boxSizing: 'border-box', padding: '0 10px 0 0', textAlign: 'right' },
  '.cm-cursor': { borderLeft: '1.5px solid var(--blue)' },
  '.cm-line.cmp-a, .cm-gutterElement.cmp-a': { backgroundColor: '#ECFDF3' },
  '&.ro .cm-content': { caretColor: 'transparent' },
});

export default function CompareEditor({ left, value, readOnly, onChange }) {
  const hostRef = useRef(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    const view = new EditorView({
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          history(),
          keymap.of([...defaultKeymap, ...historyKeymap]),
          EditorView.lineWrapping,
          EditorState.readOnly.of(!!readOnly),
          EditorView.editable.of(!readOnly),
          EditorView.editorAttributes.of({ class: readOnly ? 'ro' : '' }),
          addedField(left || ''),
          THEME,
          EditorView.updateListener.of((u) => {
            if (u.docChanged) onChangeRef.current?.(u.state.doc.toString());
          }),
        ],
      }),
      parent: hostRef.current,
    });
    return () => view.destroy();
    // 左侧、初始内容、只读变化时由调用方换 key 重建
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <div className="vr-ed" ref={hostRef} />;
}
