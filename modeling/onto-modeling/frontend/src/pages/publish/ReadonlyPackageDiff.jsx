// 知识包版本对比的正文差异（develop ReadonlyPackageDiff）：两侧只读并排，没变的段落折叠，上一处、下一处跳转；
// 只差编码或换行符时逐行看不出差别，单独说明是格式变化。
import { markdown } from '@codemirror/lang-markdown';
import { MergeView } from '@codemirror/merge';
import { EditorState } from '@codemirror/state';
import { EditorView, lineNumbers } from '@codemirror/view';
import { useEffect, useRef, useState } from 'react';
import { describeFormatChange, isFormatOnlyChange } from '../../lib/packageCompareView.js';
import { Button, Note } from '../../ui/index.jsx';

const THEME = EditorView.theme({
  '&': { fontSize: '12px', backgroundColor: '#fff' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { fontFamily: 'var(--mono)', lineHeight: '1.75' },
  '.cm-content': { caretColor: 'transparent' },
  '.cm-gutters': { backgroundColor: 'transparent', border: 'none', color: 'var(--muted)' },
});

export default function ReadonlyPackageDiff({ left, right, status, leftEncoding, rightEncoding, leftBytes, rightBytes }) {
  const hostRef = useRef(null);
  const viewRef = useRef(null);
  const chunkRef = useRef(0);
  const [chunkCount, setChunkCount] = useState(0);
  const [chunkIndex, setChunkIndex] = useState(0);

  useEffect(() => {
    const extensions = [lineNumbers(), markdown(), EditorView.lineWrapping,
      EditorView.editable.of(false), EditorState.readOnly.of(true), THEME];
    const view = new MergeView({
      a: { doc: left, extensions }, b: { doc: right, extensions },
      collapseUnchanged: { margin: 3, minSize: 6 }, parent: hostRef.current,
      highlightChanges: true, gutter: true,
    });
    viewRef.current = view;
    setChunkCount(view.chunks.length);
    chunkRef.current = view.chunks.length ? 1 : 0;
    setChunkIndex(chunkRef.current);
    return () => { viewRef.current = null; view.destroy(); };
  }, [left, right]);

  const move = (direction) => {
    const merge = viewRef.current;
    if (!merge || !chunkCount) return;
    const next = Math.min(chunkCount, Math.max(1, chunkRef.current + direction));
    const chunk = merge.chunks[next - 1];
    merge.a.dispatch({ selection: { anchor: chunk.fromA }, scrollIntoView: true });
    merge.b.dispatch({ selection: { anchor: chunk.fromB }, scrollIntoView: true });
    chunkRef.current = next;
    setChunkIndex(next);
  };
  const formatOnly = isFormatOnlyChange({ status, chunkCount, left, right });
  const formatDetail = describeFormatChange({ left, right, leftEncoding, rightEncoding, leftBytes, rightBytes });
  return (
    <div className="pc-diff">
      {formatOnly ? (
        <Note tone="blue" className="pc-diff-note">
          <b>格式变化</b>
          <div className="sm">{`${formatDetail}，因此文件被标记为修改；规范化后的正文没有行级差异。`}</div>
        </Note>
      ) : null}
      <div className="row pc-nav">
        <span className="sm muted grow">
          {chunkCount ? `第 ${chunkIndex} / ${chunkCount} 处差异` : (formatOnly ? '正文无行级差异，仅文件格式变化' : '内容无行级差异')}
        </span>
        <Button size="xs" icon="chevu" disabled={!chunkCount || chunkIndex <= 1} onClick={() => move(-1)}>上一处</Button>
        <Button size="xs" icon="chevd" disabled={!chunkCount || chunkIndex >= chunkCount} onClick={() => move(1)}>下一处</Button>
      </div>
      <div className="pc-merge" ref={hostRef} />
    </div>
  );
}
