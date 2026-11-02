import { markdown } from '@codemirror/lang-markdown';
import { MergeView } from '@codemirror/merge';
import { EditorState } from '@codemirror/state';
import { EditorView, lineNumbers } from '@codemirror/view';
import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Space, Typography } from 'antd';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { describeFormatChange, isFormatOnlyChange } from '../lib/packageCompareView.js';

export default function ReadonlyPackageDiff({ left, right, status, leftEncoding, rightEncoding, leftBytes, rightBytes }) {
  const hostRef = useRef(null);
  const viewRef = useRef(null);
  const chunkRef = useRef(0);
  const [chunkCount, setChunkCount] = useState(0);
  const [chunkIndex, setChunkIndex] = useState(0);

  useEffect(() => {
    const extensions = [lineNumbers(), markdown(), EditorView.lineWrapping,
      EditorView.editable.of(false), EditorState.readOnly.of(true)];
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
  return <>
    {formatOnly && <Alert type="info" showIcon message="格式变化" description={`${formatDetail}，因此文件被标记为修改；规范化后的正文没有行级差异。`} style={{ marginBottom: 8 }} />}
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
      <Typography.Text type="secondary">{chunkCount ? `第 ${chunkIndex} / ${chunkCount} 处差异` : (formatOnly ? '正文无行级差异，仅文件格式变化' : '内容无行级差异')}</Typography.Text>
      <Space size={6}>
        <Button size="small" icon={<ChevronUp size={14} />} disabled={!chunkCount || chunkIndex <= 1} onClick={() => move(-1)}>上一处</Button>
        <Button size="small" icon={<ChevronDown size={14} />} disabled={!chunkCount || chunkIndex >= chunkCount} onClick={() => move(1)}>下一处</Button>
      </Space>
    </div>
    <div className="cm-host cm-merge" ref={hostRef} />
  </>;
}
