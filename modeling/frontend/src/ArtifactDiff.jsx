import React, { useEffect, useMemo, useState } from 'react';
import { normalizeDiff, safeUiError, summarizeDiff } from './revision-state.js';

const FILE_STATUS = {
  added: { label: '新增', color: '#067647', background: '#ecfdf3' },
  modified: { label: '修改', color: '#175cd3', background: '#eff8ff' },
  deleted: { label: '删除', color: '#b42318', background: '#fef3f2' },
};

const LINE_STYLE = {
  context: { background: '#fff', color: '#344054' },
  add: { background: '#ecfdf3', color: '#05603a' },
  remove: { background: '#fef3f2', color: '#912018' },
};

const styles = {
  shell: { minWidth: 0, border: '1px solid #e4e7ec', borderRadius: 8, overflow: 'hidden', background: '#fff' },
  head: { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px', borderBottom: '1px solid #eaecf0', flexWrap: 'wrap' },
  count: { color: '#667085', fontSize: 12 },
  body: { display: 'flex', flexWrap: 'wrap', minHeight: 220, maxHeight: 560, overflow: 'auto' },
  files: { flex: '1 1 150px', minWidth: 150, maxHeight: 560, overflow: 'auto', borderRight: '1px solid #eaecf0', background: '#f9fafb' },
  fileButton: { width: '100%', border: 0, borderBottom: '1px solid #eaecf0', background: 'transparent', padding: '9px 10px', textAlign: 'left', cursor: 'pointer' },
  fileTop: { display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 },
  filePath: { minWidth: 0, overflowWrap: 'anywhere', color: '#344054', fontSize: 12 },
  badge: { flex: 'none', borderRadius: 4, padding: '1px 5px', fontSize: 11, fontWeight: 650 },
  stats: { marginTop: 4, color: '#667085', fontSize: 11.5 },
  preview: { flex: '3 1 320px', minWidth: 0, minHeight: 220, overflow: 'auto' },
  previewHead: { position: 'sticky', top: 0, zIndex: 1, display: 'flex', alignItems: 'center', gap: 8, padding: '9px 12px', borderBottom: '1px solid #eaecf0', background: '#fff' },
  previewPath: { minWidth: 0, overflowWrap: 'anywhere', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 12, fontWeight: 650 },
  openButton: { marginLeft: 'auto', flex: 'none', padding: '3px 9px', fontSize: 11.5 },
  hunkTitle: { padding: '5px 10px', background: '#f2f4f7', color: '#667085', fontSize: 11.5 },
  line: { display: 'grid', gridTemplateColumns: '42px 42px 20px minmax(0, 1fr)', minHeight: 22, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 12, lineHeight: '22px' },
  lineNo: { padding: '0 6px', textAlign: 'right', color: '#98a2b3', borderRight: '1px solid rgba(152, 162, 179, .2)', userSelect: 'none' },
  mark: { textAlign: 'center', userSelect: 'none' },
  content: { margin: 0, padding: '0 8px', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', font: 'inherit' },
  state: { padding: 24, textAlign: 'center', color: '#667085', fontSize: 13 },
  error: { padding: 12, color: '#b42318', background: '#fef3f2', fontSize: 12.5 },
};

function numberedLines(hunk) {
  let oldLine = hunk.oldStart;
  let newLine = hunk.newStart;
  return hunk.lines.map((line, index) => {
    const row = {
      ...line,
      key: `${oldLine}:${newLine}:${index}`,
      oldNumber: line.type === 'add' ? '' : oldLine,
      newNumber: line.type === 'remove' ? '' : newLine,
    };
    if (line.type !== 'add') oldLine += 1;
    if (line.type !== 'remove') newLine += 1;
    return row;
  });
}

/**
 * Props:
 * - diff: { fromRevision, toRevision, files[] }
 * - selectedPath: optional controlled file path
 * - onSelectFile(path, file): called when a file is selected
 * - onOpenFile(path): optional link to the full file view
 */
export default function ArtifactDiff({
  diff,
  loading = false,
  error = '',
  selectedPath,
  onSelectFile,
  onOpenFile,
}) {
  const value = useMemo(() => normalizeDiff(diff || {}), [diff]);
  const summary = useMemo(() => summarizeDiff(value), [value]);
  const [localPath, setLocalPath] = useState('');
  const requestedPath = selectedPath === undefined ? localPath : selectedPath;
  const activeFile = value.files.find((file) => file.path === requestedPath) || value.files[0] || null;

  useEffect(() => {
    if (selectedPath === undefined && activeFile && activeFile.path !== localPath) {
      setLocalPath(activeFile.path);
    }
  }, [activeFile, localPath, selectedPath]);

  const selectFile = (file) => {
    if (selectedPath === undefined) setLocalPath(file.path);
    onSelectFile?.(file.path, file);
  };

  if (loading) return <div style={styles.state}>正在读取修改...</div>;
  if (error) return <div role="alert" style={styles.error}>{safeUiError(error, '修改读取失败，请重试')}</div>;
  if (!value.files.length) return <div style={styles.state}>这个版本没有文件修改</div>;

  return (
    <section style={styles.shell} aria-label="文件修改">
      <div style={styles.head}>
        <strong style={{ fontSize: 13 }}>文件修改</strong>
        <span style={styles.count}>{summary.files} 个文件</span>
        <span style={{ ...styles.count, color: '#067647' }}>+{summary.additions}</span>
        <span style={{ ...styles.count, color: '#b42318' }}>-{summary.deletions}</span>
      </div>
      <div style={styles.body}>
        <nav style={styles.files} aria-label="文件列表">
          {value.files.map((file) => {
            const status = FILE_STATUS[file.status];
            const active = file.path === activeFile?.path;
            return (
              <button
                key={file.path}
                type="button"
                aria-current={active ? 'true' : undefined}
                style={{ ...styles.fileButton, background: active ? '#fff' : 'transparent', boxShadow: active ? 'inset 3px 0 #2e90fa' : 'none' }}
                onClick={() => selectFile(file)}
              >
                <span style={styles.fileTop}>
                  <span style={{ ...styles.badge, color: status.color, background: status.background }}>{status.label}</span>
                  <span style={styles.filePath}>{file.path}</span>
                </span>
                <span style={styles.stats}>+{file.additions} / -{file.deletions}</span>
              </button>
            );
          })}
        </nav>

        <div style={styles.preview}>
          {activeFile && (
            <>
              <div style={styles.previewHead}>
                <span style={styles.previewPath}>{activeFile.path}</span>
                {onOpenFile && (
                  <button type="button" className="btn" style={styles.openButton} onClick={() => onOpenFile(activeFile.path)}>
                    打开
                  </button>
                )}
              </div>
              {activeFile.binary && <div style={styles.state}>此文件无法预览</div>}
              {!activeFile.binary && !activeFile.hunks.length && <div style={styles.state}>暂无内容预览</div>}
              {!activeFile.binary && activeFile.hunks.map((hunk, hunkIndex) => (
                <div key={`${hunk.oldStart}:${hunk.newStart}:${hunkIndex}`}>
                  <div style={styles.hunkTitle}>第 {hunk.newStart || hunk.oldStart || 1} 行附近</div>
                  {numberedLines(hunk).map((line) => (
                    <div key={line.key} style={{ ...styles.line, ...LINE_STYLE[line.type] }}>
                      <span style={styles.lineNo}>{line.oldNumber}</span>
                      <span style={styles.lineNo}>{line.newNumber}</span>
                      <span style={styles.mark}>{line.type === 'add' ? '+' : line.type === 'remove' ? '-' : ''}</span>
                      <pre style={styles.content}>{line.content || ' '}</pre>
                    </div>
                  ))}
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
