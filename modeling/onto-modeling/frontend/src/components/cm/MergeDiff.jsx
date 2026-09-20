import { markdown } from '@codemirror/lang-markdown';
import { MergeView } from '@codemirror/merge';
import { EditorState } from '@codemirror/state';
import { EditorView, lineNumbers } from '@codemirror/view';
import { useEffect, useRef } from 'react';

// 左侧：历史版本（只读）；右侧：当前内容（可编辑）。
// revertControls 提供逐块"用旧版覆盖当前"的箭头。
export default function MergeDiff({ original, modified, onModifiedChange }) {
  const hostRef = useRef(null);
  const onChangeRef = useRef(onModifiedChange);
  onChangeRef.current = onModifiedChange;

  useEffect(() => {
    const shared = [lineNumbers(), markdown(), EditorView.lineWrapping];
    const view = new MergeView({
      a: {
        doc: original,
        extensions: [...shared, EditorView.editable.of(false), EditorState.readOnly.of(true)],
      },
      b: {
        doc: modified,
        extensions: [
          ...shared,
          EditorView.updateListener.of((update) => {
            if (update.docChanged) onChangeRef.current?.(update.state.doc.toString());
          }),
        ],
      },
      revertControls: 'a-to-b',
      collapseUnchanged: { margin: 3, minSize: 6 },
      parent: hostRef.current,
    });
    return () => view.destroy();
    // 内容变化由调用方用 key 重建
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <div className="cm-host cm-merge" ref={hostRef} />;
}
