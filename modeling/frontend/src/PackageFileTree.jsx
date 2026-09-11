import React, { useEffect, useMemo, useRef, useState } from 'react';
import { decodeLegacyArchiveName } from './legacyArchiveFilename.js';

import {
  buildPackageFileTree,
  listDirectoryPaths,
} from './packageFileTree.js';
import {
  directoryLabel,
  displayDirectoryPathsForFile,
  presentPackageFileTree,
} from './packageFileTreePresentation.js';

function FileNode({ node, selectedPath, onSelect, draftPaths, editablePaths, level }) {
  const active = node.path === selectedPath;
  const title = node.binary ? `${node.path}（二进制附件，不支持在线编辑）` : node.path;
  const draftChanged = draftPaths.has(node.path);
  const readonly = !node.binary && editablePaths && !editablePaths.has(node.path);
  return (
    <button type="button" role="treeitem" aria-level={level} aria-selected={active}
      aria-disabled={node.binary || undefined}
      data-package-path={node.path}
      className={`rv-file${active ? ' on' : ''}${node.binary ? ' binary' : ''}${readonly ? ' readonly' : ''}`}
      title={readonly ? `${title}（派生文件，只读）` : title}
      onClick={() => { if (!node.binary) onSelect(node.path); }}>
      <span className={`rv-file-type ${node.typeKind}`} aria-hidden="true">{node.typeLabel}</span>
      <span className="rv-file-name">{node.name}</span>
      {draftChanged
        ? <span className="tag tag-draft rv-file-draft-tag">草稿</span>
        : node.modified && <span className="rv-modified" title="已修订"><span className="rv-sr-only">已修订</span></span>}
      {node.binary && <span className="rv-attachment">附件</span>}
    </button>
  );
}

function DirectoryNode({ node, expanded, selectedPath, onToggle, onSelect, draftPaths, editablePaths, level }) {
  const open = expanded.has(node.path);
  const name = decodeLegacyArchiveName(node.name);
  const label = directoryLabel(name);
  const containsActive = displayDirectoryPathsForFile(selectedPath).includes(node.path);
  return (
    <div className="rv-tree-directory">
      <button type="button" className={`rv-tree-dir${containsActive ? ' contains-active' : ''}`}
        role="treeitem" aria-level={level} aria-expanded={open}
        title={`${open ? '收起' : '展开'} ${node.path}`} onClick={() => onToggle(node.path)}>
        <span className={`rv-chevron${open ? ' open' : ''}`} aria-hidden="true">›</span>
        <span className="rv-folder-mark" aria-hidden="true" />
        <span className="rv-dir-name">
          <span className="rv-dir-name-en">{name}</span>
          {label && <> <span className="rv-dir-name-zh">{label}</span></>}
        </span>
        {!!node.modifiedCount && <span className="rv-dir-modified" title={`${node.modifiedCount} 个文件已修订`} />}
        <span className="rv-dir-count">{node.fileCount}</span>
      </button>
      {open && (
        <div className="rv-tree-children" role="group">
          <TreeNodes nodes={node.children} expanded={expanded} selectedPath={selectedPath}
            onToggle={onToggle} onSelect={onSelect} draftPaths={draftPaths}
            editablePaths={editablePaths} level={level + 1} />
        </div>
      )}
    </div>
  );
}

function TreeNodes({
  nodes, expanded, selectedPath, onToggle, onSelect, draftPaths, editablePaths, level = 1,
}) {
  return nodes.map((node) => (node.type === 'directory'
    ? <DirectoryNode key={node.path} node={node} expanded={expanded} selectedPath={selectedPath}
        onToggle={onToggle} onSelect={onSelect} draftPaths={draftPaths}
        editablePaths={editablePaths} level={level} />
    : <FileNode key={node.path} node={node} selectedPath={selectedPath} onSelect={onSelect}
        draftPaths={draftPaths} editablePaths={editablePaths} level={level} />));
}

export default function PackageFileTree({
  files,
  binaryPaths = [],
  overriddenPaths = [],
  draftPaths = [],
  editablePaths = null,
  selectedPath,
  version,
  onSelect,
  onDownload,
}) {
  const tree = useMemo(
    () => presentPackageFileTree(buildPackageFileTree(files, binaryPaths, overriddenPaths)),
    [files, binaryPaths, overriddenPaths],
  );
  const allDirectories = useMemo(() => listDirectoryPaths(tree), [tree]);
  const directoryKey = allDirectories.join('|');
  const [expanded, setExpanded] = useState(() => new Set(displayDirectoryPathsForFile(selectedPath)));
  const scrollRef = useRef(null);

  useEffect(() => {
    const valid = new Set(allDirectories);
    const ancestors = displayDirectoryPathsForFile(selectedPath);
    setExpanded((current) => new Set([
      ...[...current].filter((path) => valid.has(path)),
      ...ancestors,
    ]));
  }, [selectedPath, directoryKey]);
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const selected = scrollRef.current?.querySelector('[aria-selected="true"]');
      selected?.scrollIntoView({ block: 'nearest' });
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedPath, directoryKey]);

  const toggle = (directory) => setExpanded((current) => {
    const next = new Set(current);
    next.has(directory) ? next.delete(directory) : next.add(directory);
    return next;
  });
  const modifiedCount = overriddenPaths.length;
  const attachmentCount = binaryPaths.length;
  const draftPathSet = useMemo(() => new Set(draftPaths), [draftPaths]);
  const editablePathSet = useMemo(
    () => editablePaths == null ? null : new Set(editablePaths),
    [editablePaths],
  );

  return (
    <aside className="rv-files" aria-label="知识包结构">
      <div className="rv-tree-head">
        <div className="rv-tree-heading"><span>知识包结构</span><span className="rv-tree-version">v{version}</span></div>
        <button type="button" className="rv-tree-tool" title="导出 ZIP" aria-label="导出 ZIP" onClick={onDownload}>↓</button>
      </div>
      <div className="rv-tree-summary">
        <span>{files.length} 个文件</span>
        {!!modifiedCount && <span>{modifiedCount} 已修订</span>}
        {!!attachmentCount && <span>{attachmentCount} 附件</span>}
        <span className="rv-tree-tools">
          <button type="button" title="展开全部目录" aria-label="展开全部目录"
            onClick={() => setExpanded(new Set(allDirectories))}>＋</button>
          <button type="button" title="收起全部目录" aria-label="收起全部目录"
            onClick={() => setExpanded(new Set())}>−</button>
        </span>
      </div>
      <div ref={scrollRef} className="rv-tree-scroll" role="tree" aria-label="知识包文件目录">
        <TreeNodes nodes={tree} expanded={expanded} selectedPath={selectedPath}
          onToggle={toggle} onSelect={onSelect} draftPaths={draftPathSet}
          editablePaths={editablePathSet} />
      </div>
    </aside>
  );
}
