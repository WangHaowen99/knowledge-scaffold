import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  getPackage, getPackageFile, exportPackageUrl, authFetch,
  createPackageDraft, discardPackageDraft, getPackageDraft, getPackageDraftFile,
  publishPackageDraft, requireOkResponse, revertPackageDraftFile,
  runPackageCheck, savePackageDiagram, savePackageDraftFile,
} from './api.js';
import { FlowchartEditor } from './flowchart-editor/index.js';
import {
  DIAGRAM_DOCUMENT_PATH, hasDiagramDocument, isProcessDiagramPath, resolveDiagramTarget,
} from './mermaidDiagramTarget.js';
import { BusyButton } from './ui.jsx';
import PackageCheck from './PackageCheck.jsx';
import PackageFileTree from './PackageFileTree.jsx';
import { resolvePackageFileTarget } from './packageFileTarget.js';
import { createLatestRequestGate } from './packageFileContent.js';
import PaneResizeHandle from './PaneResizeHandle.jsx';
import { clampPaneWidth } from './paneResize.js';
import {
  isPackageDraftEditablePath,
  isPackageDraftRevisionConflict,
  latestChangedPackageDraftPath,
} from './package-draft-ui.js';

/* ================= 知识包评审（查看 / 编辑 / 导出；右栏为修缮对话） ================= */

const PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE = '知识包修缮正在进行中，暂不能人工编辑';

export function ReviewPanel({
  projectId,
  stageContent = null,
  wideStage = false,
  onFileSelected,
  onDirtyChange,
  packageCheck = null,
  packageDraft: suppliedPackageDraft = null,
  packageRepairBusy = '',
  onDraftChange,
}) {
  const [pkg, setPkg] = useState(null);
  const [err, setErr] = useState('');
  const [path, setPath] = useState(null);
  const [file, setFile] = useState(null); // { content, overridden, rendered }
  const [fileLoading, setFileLoading] = useState(false);
  const [view, setView] = useState('view'); // view | edit（查看：md 渲染 / 其他文件原文）
  const [draft, setDraft] = useState('');
  const [packageDraft, setPackageDraftState] = useState(suppliedPackageDraft);
  const [actionBusy, setActionBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [saveState, setSaveState] = useState('idle');
  const [revisionConflict, setRevisionConflict] = useState(null);
  const [publishRevisionConflict, setPublishRevisionConflict] = useState(false);
  // 编辑中的未保存草稿：后台刷新不得覆盖
  const dirtyRef = useRef(false);
  const [textDraftDirty, setTextDraftDirtyState] = useState(false);
  const draftSessionRef = useRef(packageDraft);
  const pathRef = useRef(path);
  const editorContentRef = useRef(draft);
  const savedContentRef = useRef('');
  const savedContentHashRef = useRef('');
  const saveTimerRef = useRef(null);
  const savePromiseRef = useRef(Promise.resolve());
  const handledDraftRef = useRef(null);
  // 流程图源文档（diagrams/processes.json）：.mmd 只是它的派生产物
  const [diagramDoc, setDiagramDoc] = useState(null);
  const [diagramLoading, setDiagramLoading] = useState(false);
  const [diagramErr, setDiagramErr] = useState('');
  const [diagramView, setDiagramView] = useState('graph'); // graph | source
  // 图形编辑器的未保存状态独立于原文草稿，切换文件时同样要拦一下
  const diagramDirtyRef = useRef(false);
  const [diagramDraftDirty, setDiagramDraftDirtyState] = useState(false);
  const [leftWidth, setLeftWidth] = useState(280);
  const [leftMax, setLeftMax] = useState(420);
  const reviewGridRef = useRef(null);
  const fileRequestGate = useRef(null);
  const fileAbortController = useRef(null);
  const repairInProgress = Boolean(packageRepairBusy);
  if (!fileRequestGate.current) fileRequestGate.current = createLatestRequestGate();

  const setTextDraftDirty = useCallback((dirty) => {
    const next = Boolean(dirty);
    dirtyRef.current = next;
    setTextDraftDirtyState(next);
  }, []);
  const setDiagramDraftDirty = useCallback((dirty) => {
    const next = Boolean(dirty);
    diagramDirtyRef.current = next;
    setDiagramDraftDirtyState(next);
  }, []);
  const setPackageDraft = useCallback((nextDraft) => {
    draftSessionRef.current = nextDraft;
    setPackageDraftState(nextDraft);
    onDraftChange?.(nextDraft);
  }, [onDraftChange]);
  const hasUnsavedDraft = textDraftDirty || diagramDraftDirty
    || saveState === 'saving' || Boolean(revisionConflict);

  useEffect(() => {
    draftSessionRef.current = suppliedPackageDraft;
    setPackageDraftState(suppliedPackageDraft);
  }, [suppliedPackageDraft]);
  useEffect(() => { pathRef.current = path; }, [path]);
  useEffect(() => { editorContentRef.current = draft; }, [draft]);
  useEffect(() => { onDirtyChange?.(hasUnsavedDraft); }, [hasUnsavedDraft, onDirtyChange]);
  useEffect(() => () => { onDirtyChange?.(false); }, [onDirtyChange]);
  useEffect(() => {
    if (!repairInProgress) return;
    window.clearTimeout(saveTimerRef.current);
    setView('view');
  }, [repairInProgress]);

  useEffect(() => () => {
    // 只在面板真正卸载时作废请求。路径切换时 begin() 会自然淘汰旧请求；
    // 若在每次 path effect 清理时 invalidate，会把当前文件的有效响应也一并丢弃。
    fileRequestGate.current.invalidate();
    fileAbortController.current?.abort();
    window.clearTimeout(saveTimerRef.current);
  }, []);

  useEffect(() => {
    const element = reviewGridRef.current;
    if (!element || typeof ResizeObserver === 'undefined') return undefined;
    const update = () => {
      const max = Math.max(220, Math.min(420, element.clientWidth - 370));
      setLeftMax(max);
      setLeftWidth((current) => clampPaneWidth(current, 220, max));
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const loadPkg = useCallback(async () => {
    try {
      const next = await getPackage(projectId);
      setPkg(next);
      setPackageDraft(next.draft ?? null);
      setErr('');
      const editableFiles = next.files.filter((item) => !(next.binaryPaths ?? []).includes(item));
      setPath((current) => (
        (dirtyRef.current || diagramDirtyRef.current) && current ? current
          : current && editableFiles.includes(current) ? current
          : editableFiles.includes('index.md') ? 'index.md' : editableFiles[0]
      ));
      return next;
    } catch (error) {
      setErr(error instanceof Error ? error.message : '知识包加载失败');
      return null;
    }
  }, [projectId]);
  useEffect(() => { void loadPkg(); }, [loadPkg]);
  useEffect(() => {
    if (!packageDraft || !pkg || handledDraftRef.current === packageDraft.id) return;
    handledDraftRef.current = packageDraft.id;
    const target = latestChangedPackageDraftPath(packageDraft)
      ?? pkg.files.find(isPackageDraftEditablePath);
    if (!target) return;
    setPath(target);
    setView('edit');
    setMsg(`已恢复基于 v${packageDraft.baseVersion} 的未发布草稿`);
  }, [packageDraft, pkg]);
  // 右栏修缮对话生成新版本后自动刷新文件树与当前文件
  const loadFile = useCallback(async (nextPath) => {
    if (!nextPath) {
      fileRequestGate.current.invalidate();
      fileAbortController.current?.abort();
      fileAbortController.current = null;
      setFile(null); setDraft(''); setFileLoading(false);
      setTextDraftDirty(false);
      return false;
    }
    const isCurrent = fileRequestGate.current.begin();
    fileAbortController.current?.abort();
    const controller = new AbortController();
    fileAbortController.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 20_000);
    setFile(null); setDraft(''); setFileLoading(true);
    try {
      const baseFile = await getPackageFile(projectId, nextPath, { signal: controller.signal });
      const draftFile = draftSessionRef.current
        ? (await getPackageDraftFile(projectId, nextPath, { signal: controller.signal })).file
        : null;
      if (!isCurrent()) return false;
      const nextFile = {
        ...baseFile,
        content: draftFile?.content ?? baseFile.content,
        draftFile,
      };
      setFile(nextFile);
      setDraft(nextFile.content);
      savedContentRef.current = nextFile.content;
      savedContentHashRef.current = draftFile?.contentHash ?? '';
      setTextDraftDirty(false);
      setRevisionConflict(null);
      setSaveState('idle');
      setErr('');
      return true;
    } catch (e) {
      if (isCurrent()) {
        setErr(e?.name === 'AbortError' ? '文件加载超时，请重新点击文件' : e.message);
      }
      return false;
    } finally {
      window.clearTimeout(timeout);
      if (fileAbortController.current === controller) fileAbortController.current = null;
      if (isCurrent()) setFileLoading(false);
    }
  }, [projectId, setTextDraftDirty]);
  useEffect(() => {
    const h = () => {
      if (dirtyRef.current || diagramDirtyRef.current) {
        setMsg('检测到后台更新；当前文件有未保存修改，已保留你的草稿');
        return;
      }
      void loadPkg();
    };
    window.addEventListener('mw-package-updated', h);
    return () => window.removeEventListener('mw-package-updated', h);
  }, [loadPkg]);

  useEffect(() => {
    const selectRequestedFile = (event) => {
      const detail = event.detail ?? {};
      if (detail.projectId !== projectId || typeof detail.path !== 'string') return;
      const resolved = resolvePackageFileTarget(pkg?.files ?? [], detail.path);
      if (!resolved) {
        setMsg(`未找到与“${detail.path}”对应的知识文件，请从左侧目录选择`);
        return;
      }
      if (resolved !== path && (dirtyRef.current || diagramDirtyRef.current)
        && !window.confirm('当前文件有未保存修改，切换文件将丢弃这些修改，继续吗？')) return;
      setTextDraftDirty(false);
      setDiagramDraftDirty(false);
      if (resolved === path) setMsg(`已定位到 ${resolved}`);
      setPath(resolved);
      setView('view');
    };
    window.addEventListener('mw-package-file-select', selectRequestedFile);
    return () => window.removeEventListener('mw-package-file-select', selectRequestedFile);
  }, [projectId, pkg?.files, path, setDiagramDraftDirty, setTextDraftDirty]);

  useEffect(() => {
    void loadFile(path); setView('view'); setMsg('');
  }, [path, loadFile]);

  // 点开 process_N.mmd 时额外取回同级源文档；没有源文档就不取，直接走原文视图
  useEffect(() => {
    setDiagramView('graph');
    setDiagramDraftDirty(false);
    if (!isProcessDiagramPath(path) || !hasDiagramDocument(pkg?.files)) {
      setDiagramDoc(null); setDiagramErr(''); setDiagramLoading(false);
      return undefined;
    }
    let cancelled = false;
    setDiagramLoading(true); setDiagramErr('');
    getPackageFile(projectId, DIAGRAM_DOCUMENT_PATH)
      .then((doc) => { if (!cancelled) setDiagramDoc(doc); })
      .catch((e) => { if (!cancelled) { setDiagramDoc(null); setDiagramErr(e.message); } })
      .finally(() => { if (!cancelled) setDiagramLoading(false); });
    return () => { cancelled = true; };
  }, [projectId, path, pkg?.revision, setDiagramDraftDirty]);

  const diagramTarget = useMemo(() => resolveDiagramTarget({
    path, files: pkg?.files, documentContent: diagramDoc?.content,
  }), [path, pkg?.files, diagramDoc?.content]);

  // 保存用最新包修订号做乐观并发校验，与原文保存保持一致
  const pkgRevisionRef = useRef(null);
  pkgRevisionRef.current = pkg?.revision ?? null;
  const saveDiagramDocument = useCallback(async (nextDocument) => {
    if (repairInProgress) throw new Error(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
    if (draftSessionRef.current) {
      throw new Error('请先发布或放弃人工编辑草稿，再修改已确认业务流程');
    }
    const result = await savePackageDiagram(projectId, nextDocument, pkgRevisionRef.current);
    setDiagramDraftDirty(false);
    await loadPkg();
    window.dispatchEvent(new CustomEvent('mw-package-updated'));
    return { document: result.document, savedRevision: result.revision };
  }, [loadPkg, projectId, repairInProgress, setDiagramDraftDirty]);

  const ensureDraft = async () => {
    if (repairInProgress) throw new Error(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
    if (draftSessionRef.current) return draftSessionRef.current;
    setActionBusy('create');
    try {
      const created = await createPackageDraft(projectId);
      handledDraftRef.current = created.draft.id;
      setPackageDraft(created.draft);
      setPkg((current) => current ? { ...current, draft: created.draft } : current);
      return created.draft;
    } finally {
      setActionBusy((current) => current === 'create' ? '' : current);
    }
  };
  const persistDraftFile = useCallback((targetPath, content, quiet = false, expectedFile = null) => {
    const saveQueued = async () => {
      if (repairInProgress) throw new Error(PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE);
      const active = draftSessionRef.current ?? await ensureDraft();
      let expectedContentHash = expectedFile?.contentHash;
      if (!expectedContentHash && pathRef.current === targetPath) {
        expectedContentHash = savedContentHashRef.current || undefined;
      }
      if (!expectedContentHash) {
        expectedContentHash = (await getPackageDraftFile(projectId, targetPath)).file.contentHash;
      }
      setSaveState('saving');
      try {
        const saved = await savePackageDraftFile(
          projectId,
          targetPath,
          content,
          expectedFile?.revision ?? active.revision,
          expectedContentHash,
          expectedFile?.draftId ?? active.id,
        );
        setPackageDraft(saved.draft);
        setPkg((current) => current ? {
          ...current,
          draft: saved.draft,
          hasUnversionedChanges: saved.draft.changedFileCount > 0,
        } : current);
        if (pathRef.current === targetPath) {
          savedContentRef.current = saved.file.content;
          savedContentHashRef.current = saved.file.contentHash;
          setFile((current) => current ? {
            ...current,
            content: saved.file.content,
            draftFile: saved.file,
          } : current);
          if (editorContentRef.current === content) {
            setDraft(saved.file.content);
            setTextDraftDirty(false);
          }
        }
        setRevisionConflict((current) => current?.path === targetPath ? null : current);
        setSaveState(editorContentRef.current === content ? 'saved' : 'unsaved');
        if (!quiet) setMsg('✓ 已保存到人工编辑草稿');
        return saved;
      } catch (error) {
        setSaveState('error');
        if (isPackageDraftRevisionConflict(error)) {
          try {
            const latest = await getPackageDraft(projectId);
            if (!latest.draft) {
              setPackageDraft(null);
              setRevisionConflict(null);
              setView('view');
              setMsg('当前草稿已在其他页面发布或放弃，已恢复正式版本内容。');
              await loadPkg();
            } else {
              const latestFile = await getPackageDraftFile(projectId, targetPath);
              setPackageDraft(latest.draft);
              setRevisionConflict({
                path: targetPath,
                draft: latest.draft,
                file: latestFile.file,
                scope: error.data?.conflictScope === 'draft' ? 'draft' : 'file',
              });
              setMsg('检测到同文件并发修改，自动保存已暂停。请选择要保留的版本。');
            }
          } catch {
            setMsg('保存冲突：无法刷新草稿状态。当前输入仍保留，请稍后重试。');
          }
        } else {
          setMsg(`保存失败：${error.message}`);
        }
        throw error;
      }
    };
    const queued = savePromiseRef.current.catch(() => undefined).then(saveQueued);
    savePromiseRef.current = queued;
    return queued;
  }, [loadPkg, projectId, repairInProgress, setPackageDraft, setTextDraftDirty]);
  const saveTextDraft = async () => persistDraftFile(path, draft);
  useEffect(() => {
    window.clearTimeout(saveTimerRef.current);
    if (repairInProgress || revisionConflict?.path === path
      || view !== 'edit' || !path || !file) return undefined;
    if (draft === savedContentRef.current) return undefined;
    setSaveState('unsaved');
    const editorContent = draft;
    saveTimerRef.current = setTimeout(() => {
      persistDraftFile(path, editorContent, true).catch(() => undefined);
    }, 800);
    return () => window.clearTimeout(saveTimerRef.current);
  }, [draft, file, packageDraft, path, persistDraftFile, repairInProgress, revisionConflict, view]);
  useEffect(() => {
    if (!hasUnsavedDraft) return undefined;
    const warnBeforeUnload = (event) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warnBeforeUnload);
    return () => window.removeEventListener('beforeunload', warnBeforeUnload);
  }, [hasUnsavedDraft]);
  const save = async () => {
    setActionBusy('save'); setMsg('');
    try {
      await saveTextDraft();
      setMsg('已保存到人工编辑草稿');
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } catch (e) { setMsg(`保存失败：${e.message}`); } finally { setActionBusy(''); }
  };
  const reset = async () => {
    if (!packageDraft || !window.confirm('撤销这个文件在人工草稿中的修改？')) return;
    setActionBusy('revert'); setMsg('');
    try {
      const reverted = await revertPackageDraftFile(
        projectId,
        path,
        packageDraft.revision,
        packageDraft.id,
      );
      setPackageDraft(reverted.draft);
      setFile((current) => current ? {
        ...current,
        content: reverted.file.content,
        draftFile: reverted.file,
      } : current);
      setDraft(reverted.file.content);
      setTextDraftDirty(false);
      setMsg('已撤销当前文件的草稿修改');
      await loadPkg();
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } catch (e) { setMsg(`恢复失败：${e.message}`); } finally { setActionBusy(''); }
  };
  const discardDraft = async () => {
    if (!packageDraft || !window.confirm('放弃整份人工编辑草稿？所有已保存但未发布的修改都会丢失。')) return;
    setActionBusy('discard'); setMsg('');
    try {
      await discardPackageDraft(projectId, packageDraft.revision, packageDraft.id);
      setPackageDraft(null);
      setTextDraftDirty(false);
      setMsg('已放弃人工编辑草稿');
      await loadPkg();
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } catch (e) {
      if (isPackageDraftRevisionConflict(e)) setPublishRevisionConflict(true);
      else setMsg(`放弃失败：${e.message}`);
    } finally { setActionBusy(''); }
  };
  const publishRevision = async () => {
    const targetVersion = pkg.nextCandidateVersion || '新版本';
    if (!window.confirm(`发布整份人工编辑草稿并生成 v${targetVersion}？`)) return;
    setActionBusy('publish'); setMsg('');
    try {
      let active = packageDraft;
      if (draft !== content) active = (await saveTextDraft(active)).draft;
      if (!active?.changedFileCount) throw new Error('人工编辑草稿没有待发布修改');
      const result = await publishPackageDraft(
        projectId,
        active.revision,
        '人工编辑',
        active.id,
      );
      setPackageDraft(null);
      setTextDraftDirty(false);
      const checked = await runPackageCheck(projectId);
      const errors = checked.findings?.filter((finding) => finding.severity === '错误').length ?? 0;
      setMsg(errors > 0
        ? `已生成 v${result.published.version}，内容检查还有 ${errors} 个错误`
        : `已生成 v${result.published.version} 并完成内容检查`);
      await loadPkg();
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } catch (e) {
      if (isPackageDraftRevisionConflict(e)) setPublishRevisionConflict(true);
      else setMsg(`生成版本失败：${e.message}`);
    } finally {
      setActionBusy('');
    }
  };

  const loadConflictVersion = () => {
    const conflict = revisionConflict;
    if (!conflict) return;
    setPackageDraft(conflict.draft);
    setFile((current) => current ? {
      ...current,
      content: conflict.file.content,
      draftFile: conflict.file,
    } : current);
    setDraft(conflict.file.content);
    savedContentRef.current = conflict.file.content;
    savedContentHashRef.current = conflict.file.contentHash;
    setTextDraftDirty(false);
    setRevisionConflict(null);
    setSaveState('saved');
    setMsg('已加载其他页面保存的版本');
  };

  const keepCurrentConflictVersion = async () => {
    const conflict = revisionConflict;
    if (!conflict) return;
    setActionBusy('resolve-conflict');
    try {
      await persistDraftFile(conflict.path, editorContentRef.current, false, {
        revision: conflict.draft.revision,
        contentHash: conflict.file.contentHash,
        draftId: conflict.draft.id,
      });
      setRevisionConflict(null);
      setMsg('已保留当前内容并覆盖其他页面版本');
    } catch (error) {
      if (!isPackageDraftRevisionConflict(error)) setMsg(`解决冲突失败：${error.message}`);
    } finally {
      setActionBusy('');
    }
  };

  const download = async () => {
    setMsg('');
    try {
      const res = await authFetch(exportPackageUrl(projectId));
      await requireOkResponse(res);
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (res.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1]) || 'package.zip';
      a.click();
      window.setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (e) { setMsg(`导出失败：${e.message}`); }
  };

  if (err && !pkg) return <div className="empty">{err}</div>;
  if (!pkg) return <div className="empty">加载中…</div>;
  const isMd = path?.endsWith('.md');
  const binarySet = new Set(pkg.binaryPaths ?? []);
  const content = file?.content ?? '';
  const editable = isPackageDraftEditablePath(path);
  const draftPaths = packageDraft?.changedFiles?.map((item) => item.path) ?? [];
  const editablePaths = pkg.files.filter(isPackageDraftEditablePath);
  // 体检发现的 target（对象/操作 key）→ 包内文件路径
  const openByTarget = (target) => {
    const editableFiles = pkg.files.filter((item) => !binarySet.has(item));
    const hit = resolvePackageFileTarget(editableFiles, target);
    if (hit) setPath(hit);
  };

  return (
    <div className="review-panel">
    <PackageCheck projectId={projectId} onOpenFile={openByTarget}
      packageDraft={packageDraft}
      check={packageCheck?.check ?? null}
      warningReview={packageCheck?.warningReview ?? null}
      errorDeferral={packageCheck?.errorDeferral ?? null}
      repair={packageCheck?.repair ?? null}
      rechecking={packageCheck?.rechecking ?? false}
      error={packageCheck?.error ?? ''} />
    {repairInProgress && (
      <div className="stale-banner" role="status">{PACKAGE_REPAIR_EDIT_BLOCKED_MESSAGE}</div>
    )}
    {revisionConflict?.path === path && (
      <div className="stale-banner package-draft-conflict" role="alert">
        <div>
          <strong>检测到同文件并发修改，自动保存已暂停。</strong>
          <div>请选择加载其他页面版本，或保留当前内容并覆盖。</div>
        </div>
        <button type="button" className="btn" disabled={!!actionBusy} onClick={loadConflictVersion}>
          加载其他页面版本
        </button>
        <button type="button" className="btn btn-p" disabled={!!actionBusy}
          onClick={() => { void keepCurrentConflictVersion(); }}>
          保留当前内容并覆盖
        </button>
      </div>
    )}
    {publishRevisionConflict && (
      <div className="modal-bg" role="alertdialog" aria-modal="true" aria-labelledby="draft-publish-conflict-title">
        <div className="modal">
          <h3 id="draft-publish-conflict-title">有其他修改已应用到当前草稿</h3>
          <p>当前页面的 revision 已过期。为避免覆盖其他页面内容，请刷新页面后重新检查并发布。</p>
          <div className="modal-actions">
            <button type="button" className="btn btn-p" onClick={() => window.location.reload()}>刷新页面</button>
          </div>
        </div>
      </div>
    )}
    <div ref={reviewGridRef} className={`rv${wideStage ? ' wide-stage' : ''}`} style={{ '--rv-files-width': `${leftWidth}px` }}>
      {!wideStage && <PackageFileTree files={pkg.files} binaryPaths={pkg.binaryPaths} overriddenPaths={pkg.overriddenPaths}
        draftPaths={draftPaths} editablePaths={editablePaths}
        selectedPath={path} version={pkg.version} onSelect={(nextPath) => {
          if (nextPath === path) { void loadFile(nextPath); onFileSelected?.(nextPath); return; }
          if ((dirtyRef.current || diagramDirtyRef.current)
            && !window.confirm('当前文件有未保存修改，切换文件将丢弃这些修改，继续吗？')) return;
          setTextDraftDirty(false);
          setDiagramDraftDirty(false);
          setPath(nextPath);
          onFileSelected?.(nextPath);
        }} onDownload={download} />}
      {!wideStage && <PaneResizeHandle label="调整知识包结构栏宽度" value={leftWidth} min={220} max={leftMax}
        defaultValue={280} onChange={setLeftWidth} />}
      <div className="rv-view">
        {stageContent ? (
          <div className="rv-stage">{stageContent}</div>
        ) : <>
        {err && (
          <div className="lerr" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
            <span style={{ flex: 1 }}>文件加载失败：{err}</span>
            <button type="button" className="btn" onClick={() => { setErr(''); void loadFile(path); }}>重试</button>
          </div>
        )}
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
          <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12, color: 'var(--muted)' }}>
            {path}{file?.draftFile?.changed
              ? <span className="tag tag-draft" style={{ marginLeft: 8 }}>人工编辑草稿</span>
              : null}
          </span>
          {diagramTarget ? (
            // .mmd 由源文档渲染而来，直接改文本会在下次保存时被覆盖，这里只给只读源码视图
            <span className="viewtog" style={{ marginLeft: 'auto' }}>
              <button className={diagramView === 'graph' ? 'on' : ''} onClick={() => setDiagramView('graph')}>图形</button>
              <button className={diagramView === 'source' ? 'on' : ''} onClick={() => setDiagramView('source')}>源码</button>
            </span>
          ) : editable ? (
            <span className="viewtog" style={{ marginLeft: 'auto' }}>
              <button className={view === 'view' ? 'on' : ''} onClick={() => setView('view')}>查看</button>
              <button className={view === 'edit' ? 'on' : ''}
                disabled={repairInProgress || !editable || actionBusy === 'create'}
                onClick={() => { setDraft(content); setView('edit'); }}>编辑</button>
            </span>
          ) : (
            <span style={{ marginLeft: 'auto', color: 'var(--muted)', fontSize: 12 }}>
              派生或配置文件只读
            </span>
          )}
        </div>
        {isProcessDiagramPath(path) && !diagramTarget && !diagramLoading && (
          <div className="lerr" style={{ marginBottom: 10 }}>
            {diagramErr
              ? `流程图源文档读取失败：${diagramErr}，已回退到源码视图`
              : '该流程图缺少配套的结构化源文档（diagrams/processes.json），暂不支持图形编辑'}
          </div>
        )}
        {diagramTarget ? (
          diagramView === 'source'
            ? <pre className="srcview">{content}</pre>
            : <div className="rv-flowchart">
              <FlowchartEditor
                key={`${projectId}:${path}`}
                document={diagramTarget.document}
                initialDiagramId={diagramTarget.activeDiagramId}
                savedRevision={pkg.revision}
                onSave={saveDiagramDocument}
                onStateChange={(state) => { setDiagramDraftDirty(Boolean(state?.dirty)); }}
              />
            </div>
        ) : fileLoading || diagramLoading ? <div className="empty">文件加载中…</div> : view === 'edit' ? (
          <div>
              <textarea className="rv-editor" value={draft} spellCheck={false}
              disabled={repairInProgress || !!revisionConflict}
              onChange={(e) => { setTextDraftDirty(true); setDraft(e.target.value); }} />
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
              <button className="btn btn-p"
                disabled={repairInProgress || !!revisionConflict || !!actionBusy || saveState === 'saving'}
                onClick={save}>{actionBusy === 'save' || saveState === 'saving' ? '保存中…' : '保存修改'}</button>
              {(packageDraft?.changedFileCount > 0 || draft !== content) && (
                <BusyButton busy={actionBusy === 'publish'} busyText="保存并检查中" expect="约 1~3 分钟" className="btn"
                  disabled={repairInProgress || !!revisionConflict || !!actionBusy || saveState === 'saving'}
                  onClick={publishRevision}>
                  发布草稿并生成{pkg.nextCandidateVersion ? ` v${pkg.nextCandidateVersion}` : '新版本'}
                </BusyButton>
              )}
              {file?.draftFile?.changed && (
                <button className="btn" disabled={repairInProgress || !!actionBusy} onClick={reset}>
                  撤销该派生文件覆盖
                </button>
              )}
              {packageDraft && (
                <button className="btn" disabled={repairInProgress || !!actionBusy} onClick={discardDraft}>
                  放弃整份草稿
                </button>
              )}
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>{msg}</span>
            </div>
            <p className="muted" style={{ marginTop: 8 }}>
              带“草稿”标记的派生文件将在发布后一起生成新的知识包版本。
            </p>
          </div>
        ) : isMd
          ? <div className="mdv"><ReactMarkdown remarkPlugins={[remarkGfm]}>{stripFrontmatter(content)}</ReactMarkdown></div>
          : <pre className="srcview">{content}</pre>}
        </>}
      </div>
    </div>
    </div>
  );
}

function stripFrontmatter(md) {
  const m = /^---\n[\s\S]*?\n---\n?/.exec(md || '');
  return m ? md.slice(m[0].length) : md;
}
