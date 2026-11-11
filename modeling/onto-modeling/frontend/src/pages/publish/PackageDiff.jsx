// 知识包版本对比的正文差异（PC-07）：两侧只读并排，没变的段落折叠，上一处、下一处跳转。
// 改的只是编码或换行符时，逐行看不出差别，单独说明是格式变化。
import { markdown } from '@codemirror/lang-markdown';
import { MergeView } from '@codemirror/merge';
import { EditorState } from '@codemirror/state';
import { EditorView, lineNumbers } from '@codemirror/view';
import { useEffect, useRef, useState } from 'react';
import { Button, Note } from '../../ui/index.jsx';
import { describeFormatChange, isFormatOnlyChange } from './packageCompareModel.js';

const THEME = EditorView.theme({
  '&': { fontSize: '12px', backgroundColor: '#fff' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { fontFamily: 'var(--mono)', lineHeight: '1.75' },
  '.cm-content': { caretColor: 'transparent' },
  '.cm-gutters': { backgroundColor: 'transparent', border: 'none', color: 'var(--muted)' },
});

export default function PackageDiff({ diff }) {
  const hostRef = useRef(null);
  const viewRef = useRef(null);
  const [count, setCount] = useState(0);
  const [index, setIndex] = useState(0);
  const left = diff.left?.text || '';
  const right = diff.right?.text || '';

  useEffect(() => {
    const extensions = [lineNumbers(), markdown(), EditorView.lineWrapping, EditorView.editable.of(false),
      EditorState.readOnly.of(true), THEME];
    const view = new MergeView({
      a: { doc: left, extensions },
      b: { doc: right, extensions },
      parent: hostRef.current,
      collapseUnchanged: { margin: 3, minSize: 6 },
      highlightChanges: diff.status === 'modified',
      gutter: true,
    });
    viewRef.current = view;
    setCount(view.chunks.length);
    setIndex(view.chunks.length ? 1 : 0);
    return () => { viewRef.current = null; view.destroy(); };
  }, [left, right, diff.status]);

  const move = (step) => {
    const view = viewRef.current;
    if (!view || !count) return;
    const next = Math.min(count, Math.max(1, index + step));
    const chunk = view.chunks[next - 1];
    view.a.dispatch({ selection: { anchor: chunk.fromA }, scrollIntoView: true });
    view.b.dispatch({ selection: { anchor: chunk.fromB }, scrollIntoView: true });
    setIndex(next);
  };
  const formatOnly = isFormatOnlyChange({ status: diff.status, chunkCount: count, left, right });

  return (
    <div className="pc-diff">
      {formatOnly ? (
        <Note tone="gray" className="mb8">
          {describeFormatChange({
            left, right, leftEncoding: diff.left?.encoding, rightEncoding: diff.right?.encoding,
            leftBytes: diff.left?.bytes, rightBytes: diff.right?.bytes,
          })}
        </Note>
      ) : null}
      {count > 1 ? (
        <div className="row g4 pc-nav">
          <span className="xs muted num">{index}/{count}</span>
          <Button size="xs" kind="ghost" icon="chevu" title="上一处" disabled={index <= 1} onClick={() => move(-1)} />
          <Button size="xs" kind="ghost" icon="chevd" title="下一处" disabled={index >= count} onClick={() => move(1)} />
        </div>
      ) : null}
      <div className="pc-merge" ref={hostRef} />
    </div>
  );
}
