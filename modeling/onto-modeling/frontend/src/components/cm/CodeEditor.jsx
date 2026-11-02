import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { EditorState } from '@codemirror/state';
import { EditorView, highlightActiveLine, highlightActiveLineGutter, keymap, lineNumbers } from '@codemirror/view';
import { useEffect, useRef } from 'react';

// 原文编辑器：按设计稿 .fl-ed 不做语法着色（不引 lang-markdown，它会连带 html / js / css 语言包）。
// readOnly：大文件只显示前 512KB、写入范围锁住时只读
export default function CodeEditor({ value, onChange, readOnly = false, className }) {
  const hostRef = useRef(null);
  const viewRef = useRef(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    const view = new EditorView({
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          highlightActiveLine(),
          highlightActiveLineGutter(),
          history(),
          keymap.of([...defaultKeymap, ...historyKeymap]),
          EditorView.lineWrapping,
          EditorState.readOnly.of(readOnly),
          EditorView.editable.of(!readOnly),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) onChangeRef.current?.(update.state.doc.toString());
          }),
        ],
      }),
      parent: hostRef.current,
    });
    viewRef.current = view;
    return () => view.destroy();
    // 仅挂载时创建；外部 value / readOnly 变化由 key 重建（见调用方）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <div className={`cm-host${className ? ` ${className}` : ''}`} ref={hostRef} />;
}
