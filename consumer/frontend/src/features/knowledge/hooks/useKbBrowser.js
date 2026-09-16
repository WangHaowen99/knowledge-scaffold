// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { useCallback, useEffect, useRef, useState } from 'react';
import { authFetch } from '../../../shared/api/auth';
import { friendlyRequestError } from '../../../shared/api/errors';
import {
  IMPORT_PHASE,
  createImportFlowState,
  importFlowNotice,
  importFlowNoticeTone,
  importRequestFields,
  reduceImportFlow,
} from '../import/importFlow';
import {
  nextKnowledgeRevision,
  reconcileTreeSelection,
  resolveKnowledgeVersionOverride,
} from '../import/knowledgeRefresh';
import {
  describeZipDownload,
  resolveZipDownloadFileName,
  startBlobDownload,
} from '../lib/browserInterop';
import { useAuthContext } from '../../auth';

// 知识包工作台的浏览状态：知识包、版本、目录和文件选择统一由右侧工作台消费。
// 数据源：/agentic/ontologies + /tree（整包结构，后端已排除 origin_data/datasets）+ /raw-file。
// 管理操作走既有本体端点：启用/禁用 PUT /status、激活版本 PUT /active-version、
// 删除版本 DELETE /versions/<v>、下载 GET /download（均支持 version 参数按版本操作）。
export const DIR_LABELS = {
  objects: '业务对象',
  operations: '操作',
  scripts: '脚本',
  mappings: '数据映射',
};
export const DIR_ICONS = {
  objects: '▤',
  operations: '⚙',
  scripts: '𝑓',
  mappings: '⇄',
};

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
export default function useKbBrowser({
  listEnabled = false,
  treeEnabled = false,
  ontologyResource,
} = {}) {
  const { can, user, workspaceId, status, workspaceDataReady } =
    useAuthContext();
  const principalId = String(user?.username || '');
  const scopeKey = `${workspaceDataReady ? 'ready' : status}:${principalId}:${workspaceId || ''}`;
  const listStateKey = `${scopeKey}:${listEnabled ? 'enabled' : 'disabled'}`;
  const canImport = can('ws.kb.import');
  const canActivate = can('ws.kb.activate');
  const canManage = can('ws.kb.write');
  const [packages, setPackages] = useState([]);
  const [pkgsLoading, setPkgsLoading] = useState(true);
  const [packagesError, setPackagesError] = useState('');
  const [resolvedListStateKey, setResolvedListStateKey] =
    useState(listStateKey);
  const [pkgId, setPkgId] = useState(null);
  const [tree, setTree] = useState([]);
  const [treeLoading, setTreeLoading] = useState(false);
  const [treeError, setTreeError] = useState('');
  const [treeReloadKey, setTreeReloadKey] = useState(0);
  const [knowledgeRevision, setKnowledgeRevision] = useState(0);
  const knowledgeRevisionRef = useRef(0);
  // sel.dir = 当前目录相对路径（'' 为包根）；sel.file = 当前文件相对路径或 null
  const [sel, setSel] = useState({ dir: 'objects', file: null });
  const [importFlow, setImportFlow] = useState(createImportFlowState);
  // versionOverride = null 表示跟随激活版本；字符串表示浏览指定历史版本
  const [versionOverride, setVersionOverride] = useState(null);
  const [opBusy, setOpBusy] = useState('');
  const [opNotice, setOpNotice] = useState('');
  const packageRequestRef = useRef(0);
  const packageSubscriptionRef = useRef(null);
  const currentListContextRef = useRef(null);
  const loadPackagesRef = useRef(null);
  const treeRequestRef = useRef(0);
  const selectionEpochRef = useRef(0);
  const importRequestRef = useRef(0);
  const importAbortRef = useRef(null);
  const importReconcileTimerRef = useRef(null);
  const scopeRef = useRef(scopeKey);
  scopeRef.current = scopeKey;
  currentListContextRef.current = {
    principalId,
    workspaceId,
    listEnabled,
    workspaceDataReady,
  };

  const listStateCurrent = resolvedListStateKey === listStateKey;
  const visiblePackages = listStateCurrent ? packages : [];
  const visiblePkgId = listStateCurrent ? pkgId : null;
  const pkg = visiblePackages.find((p) => p.id === visiblePkgId) || null;
  const activeVersion = pkg?.active_version || pkg?.version || '';
  const viewVersion = versionOverride ?? activeVersion;
  const viewingActive =
    versionOverride === null || versionOverride === activeVersion;

  const releasePackageSubscription = useCallback(() => {
    packageSubscriptionRef.current?.release();
    packageSubscriptionRef.current = null;
  }, []);

  const loadPackages = useCallback(
    async ({ signal, revision = knowledgeRevisionRef.current } = {}) => {
      const requestId = ++packageRequestRef.current;
      const requestScope = scopeKey;
      const isCurrent = () =>
        !signal?.aborted &&
        requestId === packageRequestRef.current &&
        requestScope === scopeRef.current;
      if (!listEnabled || !workspaceDataReady || !ontologyResource) {
        releasePackageSubscription();
        if (isCurrent()) {
          setPackages([]);
          setPkgId(null);
          setTree([]);
          setPkgsLoading(false);
          setPackagesError('');
        }
        return;
      }
      setPkgsLoading(true);
      setPackagesError('');
      const subscription = ontologyResource.subscribe({
        principalId,
        workspaceId,
        revision,
      });
      const previousSubscription = packageSubscriptionRef.current;
      packageSubscriptionRef.current = subscription;
      previousSubscription?.release();
      try {
        const list = await subscription.promise;
        if (!isCurrent()) {
          return;
        }
        setPackages(list);
        setPkgId((prev) =>
          prev && list.some((p) => p.id === prev) ? prev : list[0]?.id || null,
        );
      } catch (error) {
        if (!isCurrent() || error?.name === 'AbortError') {
          return;
        }
        setPackages([]);
        setPackagesError(friendlyRequestError(error, '知识包列表加载失败'));
      } finally {
        if (packageSubscriptionRef.current === subscription) {
          packageSubscriptionRef.current = null;
        }
        subscription.release();
        if (isCurrent()) {
          setPkgsLoading(false);
        }
      }
    },
    [
      listEnabled,
      ontologyResource,
      principalId,
      releasePackageSubscription,
      scopeKey,
      workspaceDataReady,
      workspaceId,
    ],
  );
  loadPackagesRef.current = loadPackages;

  useEffect(() => {
    knowledgeRevisionRef.current = 0;
    setKnowledgeRevision(0);
    return () => {
      if (principalId && workspaceId) {
        ontologyResource?.invalidateScope({ principalId, workspaceId });
      }
    };
  }, [ontologyResource, principalId, workspaceId]);

  useEffect(() => {
    const controller = new AbortController();
    setResolvedListStateKey(listStateKey);
    setPackages([]);
    setPkgId(null);
    setTree([]);
    setTreeLoading(false);
    setPackagesError('');
    setTreeError('');
    setVersionOverride(null);
    setSel({ dir: 'objects', file: null });
    setOpBusy('');
    setOpNotice('');
    setPkgsLoading(listEnabled && workspaceDataReady);
    const timer = setTimeout(
      () => loadPackages({ signal: controller.signal }),
      0,
    );
    return () => {
      clearTimeout(timer);
      controller.abort();
      releasePackageSubscription();
    };
  }, [
    listEnabled,
    listStateKey,
    loadPackages,
    releasePackageSubscription,
    workspaceDataReady,
  ]);

  useEffect(() => {
    if (importReconcileTimerRef.current) {
      window.clearTimeout(importReconcileTimerRef.current);
      importReconcileTimerRef.current = null;
    }
    importAbortRef.current?.abort();
    importAbortRef.current = null;
    importRequestRef.current += 1;
    setImportFlow(createImportFlowState());
    return () => {
      if (importReconcileTimerRef.current) {
        window.clearTimeout(importReconcileTimerRef.current);
        importReconcileTimerRef.current = null;
      }
    };
  }, [scopeKey]);

  // 浏览中的版本从包里消失时（如刚被删除）回落到激活版本
  useEffect(() => {
    if (
      versionOverride !== null &&
      pkg &&
      !(pkg.versions || []).includes(versionOverride)
    ) {
      setVersionOverride(null);
    }
  }, [pkg, versionOverride]);

  useEffect(() => {
    // G.CTL.07 exception: tests enforce this complete tree-admission contract as one visible guard.
    if (!treeEnabled || !workspaceDataReady || !listStateCurrent || !pkgId) {
      setTree([]);
      setTreeLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    const requestId = ++treeRequestRef.current;
    const requestScope = scopeRef.current;
    const isCurrent = () =>
      !controller.signal.aborted &&
      requestId === treeRequestRef.current &&
      requestScope === scopeRef.current;
    const t = setTimeout(async () => {
      setTreeLoading(true);
      setTreeError('');
      try {
        const q =
          versionOverride !== null ?
            `?version=${encodeURIComponent(versionOverride)}` :
            '';
        const res = await authFetch(
          `/agentic/ontologies/${encodeURIComponent(pkgId)}/tree${q}`,
          {
            signal: controller.signal,
          },
        );
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(
            data.error || data.detail || `知识包目录加载失败（${res.status}）`,
          );
        }
        if (!isCurrent()) {
          return;
        }
        const entries = data.entries || [];
        setTree(entries);
        const firstDir =
          entries.find((e) => e.type === 'dir' && e.name === 'objects') ||
          entries.find((e) => e.type === 'dir');
        setSel((previous) =>
          reconcileTreeSelection(entries, previous, firstDir?.path || ''),
        );
      } catch (error) {
        if (isCurrent() && error?.name !== 'AbortError') {
          setTree([]);
          setTreeError(friendlyRequestError(error, '知识包目录加载失败'));
        }
      } finally {
        if (isCurrent()) {
          setTreeLoading(false);
        }
      }
    }, 0);
    return () => {
      controller.abort();
      clearTimeout(t);
    };
  }, [
    listStateCurrent,
    pkgId,
    treeEnabled,
    workspaceId,
    versionOverride,
    workspaceDataReady,
    treeReloadKey,
  ]);

  const selectPkg = useCallback(
    (id) => {
      selectionEpochRef.current += 1;
      setPkgId(id);
      setVersionOverride(null);
      setTree([]);
      setTreeError('');
      setTreeLoading(Boolean(id && treeEnabled));
      setSel({ dir: 'objects', file: null });
      setTreeReloadKey((value) => value + 1);
      setOpNotice('');
    },
    [treeEnabled],
  );
  const selectDir = useCallback((dirPath) => {
    setSel({ dir: dirPath, file: null });
  }, []);
  const selectFile = useCallback((filePath) => {
    const parent = filePath.includes('/') ?
      filePath.slice(0, filePath.lastIndexOf('/')) :
      '';
    setSel({ dir: parent, file: filePath });
  }, []);
  const selectVersion = useCallback(
    (version) => {
      setVersionOverride(version === activeVersion ? null : version);
    },
    [activeVersion],
  );
  const reloadTree = useCallback(
    () => setTreeReloadKey((value) => value + 1),
    [],
  );

  const bumpKnowledgeRevision = useCallback(() => {
    const revision = nextKnowledgeRevision(knowledgeRevisionRef.current);
    knowledgeRevisionRef.current = revision;
    setKnowledgeRevision(revision);
    return revision;
  }, []);

  const advanceKnowledgeRevision = useCallback(() => {
    ontologyResource?.invalidateScope({ principalId, workspaceId });
    return bumpKnowledgeRevision();
  }, [bumpKnowledgeRevision, ontologyResource, principalId, workspaceId]);

  const refreshPackageList = useCallback(
    async ({
      signal,
      principalId: targetPrincipalId = principalId,
      workspaceId: targetWorkspaceId = workspaceId,
    } = {}) => {
      const targetScope = {
        principalId: String(targetPrincipalId || ''),
        workspaceId: String(targetWorkspaceId || ''),
      };
      if (targetScope.principalId && targetScope.workspaceId) {
        ontologyResource?.invalidateScope(targetScope);
      }

      const { current: currentListContext } = currentListContextRef;
      const listContextChanged =
        !currentListContext?.listEnabled ||
        !currentListContext.workspaceDataReady ||
        currentListContext.principalId !== targetScope.principalId ||
        currentListContext.workspaceId !== targetScope.workspaceId;
      if (listContextChanged) {
        return false;
      }

      const revision = bumpKnowledgeRevision();
      return loadPackagesRef.current?.({ signal, revision });
    },
    [bumpKnowledgeRevision, ontologyResource, principalId, workspaceId],
  );

  // 所有会改变知识包文件内容的入口共用这一刷新边界。revision 让已选中的
  // raw-file 即使路径未变也会重新读取；treeReloadKey 刷新目录与文件元数据。
  const refreshKnowledge = useCallback(
    async ({
      ontologyId = pkgId,
      refreshPackages = true,
      followActive = false,
      selectTarget = false,
    } = {}) => {
      const requestScope = scopeRef.current;
      const requestSelectionEpoch = selectionEpochRef.current;
      const revision = advanceKnowledgeRevision();
      if (refreshPackages) {
        await loadPackages({ revision });
      }
      if (
        requestScope !== scopeRef.current ||
        (!selectTarget && requestSelectionEpoch !== selectionEpochRef.current)
      ) {
        return false;
      }
      if (selectTarget && ontologyId) {
        selectionEpochRef.current += 1;
        setPkgId(ontologyId);
      }
      setVersionOverride((current) =>
        resolveKnowledgeVersionOverride(current, followActive),
      );
      setTreeReloadKey((value) => value + 1);
      return true;
    },
    [advanceKnowledgeRevision, loadPackages, pkgId],
  );

  const executeImport = useCallback(
    async (flowState) => {
      if (!flowState?.file || flowState.phase !== IMPORT_PHASE.UPLOADING) {
        return;
      }
      importAbortRef.current?.abort();
      const controller = new AbortController();
      importAbortRef.current = controller;
      const requestId = ++importRequestRef.current;
      const requestScope = scopeRef.current;
      const isCurrent = () =>
        !controller.signal.aborted &&
        requestId === importRequestRef.current &&
        requestScope === scopeRef.current;
      try {
        const form = new FormData();
        form.append('file', flowState.file);
        Object.entries(importRequestFields(flowState.request)).forEach(
          ([key, value]) => form.append(key, value),
        );
        const res = await authFetch('/agentic/ontologies/import-zip', {
          method: 'POST',
          body: form,
          signal: controller.signal,
        });
        const data = await res.json().catch(() => ({}));
        if (!isCurrent()) {
          return;
        }
        if (res.status === 409 && data.requires_confirmation) {
          setImportFlow((current) =>
            reduceImportFlow(current, { type: 'CONFLICT', payload: data }),
          );
          return;
        }
        if (!res.ok) {
          setImportFlow((current) =>
            reduceImportFlow(current, {
              type: 'FAILURE',
              error: data.error || data.detail || `请求失败（${res.status}）`,
              errorCode: data.error_code,
            }),
          );
          return;
        }
        setImportFlow((current) =>
          reduceImportFlow(current, { type: 'SUCCESS', payload: data }),
        );
        await refreshKnowledge({
          ontologyId:
            data.ontology_id || flowState.conflict?.ontologyId || pkgId,
          followActive: true,
          selectTarget: true,
        });
      } catch (error) {
        if (!isCurrent() || error?.name === 'AbortError') {
          return;
        }
        setImportFlow((current) =>
          reduceImportFlow(current, {
            type: 'FAILURE',
            error: friendlyRequestError(error, '知识包导入失败'),
          }),
        );
      } finally {
        if (importAbortRef.current === controller) {
          importAbortRef.current = null;
        }
      }
    },
    [pkgId, refreshKnowledge],
  );

  const importZip = useCallback((file) => {
    setImportFlow((current) =>
      reduceImportFlow(current, { type: 'SELECT_FILE', file }),
    );
  }, []);

  const setImportAcknowledged = useCallback((value) => {
    setImportFlow((current) =>
      reduceImportFlow(current, { type: 'SET_ACKNOWLEDGED', value }),
    );
  }, []);

  const confirmImportSafety = useCallback(() => {
    const next = reduceImportFlow(importFlow, { type: 'START_UPLOAD' });
    setImportFlow(next);
    if (next.phase === IMPORT_PHASE.UPLOADING) {
      executeImport(next);
    }
  }, [executeImport, importFlow]);

  const setImportNewVersion = useCallback((value) => {
    setImportFlow((current) =>
      reduceImportFlow(current, { type: 'SET_NEW_VERSION', value }),
    );
  }, []);

  const resolveImportConflict = useCallback(
    (action) => {
      const next = reduceImportFlow(importFlow, {
        type: 'RESOLVE_CONFLICT',
        action,
      });
      setImportFlow(next);
      if (next.phase === IMPORT_PHASE.UPLOADING) {
        executeImport(next);
      }
    },
    [executeImport, importFlow],
  );

  const retryImport = useCallback(() => {
    const next = reduceImportFlow(importFlow, { type: 'RETRY' });
    setImportFlow(next);
    if (next.phase === IMPORT_PHASE.UPLOADING) {
      executeImport(next);
    }
  }, [executeImport, importFlow]);

  const cancelImport = useCallback(() => {
    const stoppedWaiting = importFlow.phase === IMPORT_PHASE.UPLOADING;
    importAbortRef.current?.abort();
    importAbortRef.current = null;
    importRequestRef.current += 1;
    setImportFlow(reduceImportFlow(importFlow, { type: 'CANCEL' }));
    if (!stoppedWaiting) {
      return;
    }

    if (importReconcileTimerRef.current) {
      window.clearTimeout(importReconcileTimerRef.current);
    }
    const requestScope = scopeRef.current;
    importReconcileTimerRef.current = window.setTimeout(async () => {
      try {
        await refreshKnowledge({ followActive: true });
      } finally {
        importReconcileTimerRef.current = null;
        if (requestScope === scopeRef.current) {
          setImportFlow((current) =>
            reduceImportFlow(current, { type: 'RECONCILED' }),
          );
        }
      }
    }, 2500);
  }, [importFlow, refreshKnowledge]);

  const clearImportPreview = useCallback(() => {
    setImportFlow((current) =>
      reduceImportFlow(current, { type: 'CLEAR_PREVIEW' }),
    );
  }, []);

  const setPkgEnabled = useCallback(
    async (enabled) => {
      if (!pkgId) {
        return;
      }
      const requestScope = scopeRef.current;
      setOpBusy('status');
      setOpNotice('');
      try {
        const res = await authFetch(
          `/agentic/ontologies/${encodeURIComponent(pkgId)}/status`,
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
          },
        );
        const data = await res.json().catch(() => ({}));
        if (requestScope !== scopeRef.current) {
          return;
        }
        if (res.ok) {
          setOpNotice(
            enabled ? '知识包已启用' : '知识包已禁用，Agent 将不再使用该知识包',
          );
          await refreshPackageList();
        } else {
          setOpNotice(`操作失败：${data.error || data.detail || res.status}`);
        }
      } catch (error) {
        if (requestScope === scopeRef.current) {
          setOpNotice(friendlyRequestError(error, '知识包状态更新失败'));
        }
      } finally {
        if (requestScope === scopeRef.current) {
          setOpBusy('');
        }
      }
    },
    [pkgId, refreshPackageList],
  );

  const setBusinessCheckpointEnabled = useCallback(
    async (enabled) => {
      if (!pkgId) {
        return;
      }
      const requestScope = scopeRef.current;
      const expectedRevision = String(pkg?.business_checkpoint?.revision || '');
      setOpBusy('businessCheckpoint');
      setOpNotice('');
      try {
        const res = await authFetch(
          `/agentic/ontologies/${encodeURIComponent(pkgId)}/capabilities/business-checkpoint`,
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              enabled,
              expected_revision: expectedRevision,
            }),
          },
        );
        const data = await res.json().catch(() => ({}));
        if (requestScope !== scopeRef.current) {
          return;
        }
        if (!res.ok) {
          if (res.status === 409) {
            await refreshPackageList();
            if (requestScope !== scopeRef.current) {
              return;
            }
          }
          setOpNotice(`操作失败：${data.error || data.detail || res.status}`);
          return;
        }
        setOpNotice(
          enabled ? '业务阶段 CheckPoint 已开启' : '业务阶段 CheckPoint 已关闭',
        );
        await refreshPackageList();
      } catch (error) {
        if (requestScope === scopeRef.current) {
          setOpNotice(friendlyRequestError(error, 'CheckPoint 状态更新失败'));
        }
      } finally {
        if (requestScope === scopeRef.current) {
          setOpBusy('');
        }
      }
    },
    [pkg, pkgId, refreshPackageList],
  );

  const activateVersion = useCallback(async () => {
    if (!pkgId || !viewVersion) {
      return;
    }
    const requestScope = scopeRef.current;
    setOpBusy('activate');
    setOpNotice('');
    try {
      const res = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(pkgId)}/active-version`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ version: viewVersion }),
        },
      );
      const data = await res.json().catch(() => ({}));
      if (requestScope !== scopeRef.current) {
        return;
      }
      if (res.ok) {
        setOpNotice(`已激活版本 ${viewVersion}`);
        setVersionOverride(null);
        await refreshPackageList();
      } else {
        setOpNotice(`激活失败：${data.error || data.detail || res.status}`);
      }
    } catch (error) {
      if (requestScope === scopeRef.current) {
        setOpNotice(friendlyRequestError(error, '版本激活失败'));
      }
    } finally {
      if (requestScope === scopeRef.current) {
        setOpBusy('');
      }
    }
  }, [pkgId, refreshPackageList, viewVersion]);

  const deleteVersion = useCallback(async () => {
    if (!pkgId || !viewVersion) {
      return;
    }
    const requestScope = scopeRef.current;
    setOpBusy('deleteVersion');
    setOpNotice('');
    try {
      const res = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(pkgId)}/versions/${encodeURIComponent(viewVersion)}`,
        { method: 'DELETE' },
      );
      const data = await res.json().catch(() => ({}));
      if (requestScope !== scopeRef.current) {
        return;
      }
      if (res.ok) {
        setOpNotice(`已删除版本 ${viewVersion}`);
        setVersionOverride(null);
        await refreshPackageList();
      } else {
        setOpNotice(`删除失败：${data.error || data.detail || res.status}`);
      }
    } catch (error) {
      if (requestScope === scopeRef.current) {
        setOpNotice(friendlyRequestError(error, '版本删除失败'));
      }
    } finally {
      if (requestScope === scopeRef.current) {
        setOpBusy('');
      }
    }
  }, [pkgId, refreshPackageList, viewVersion]);

  const deletePackage = useCallback(async () => {
    if (!pkgId) {
      return;
    }
    const requestScope = scopeRef.current;
    setOpBusy('deletePackage');
    setOpNotice('');
    try {
      const res = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(pkgId)}`,
        { method: 'DELETE' },
      );
      const data = await res.json().catch(() => ({}));
      if (requestScope !== scopeRef.current) {
        return;
      }
      if (res.ok) {
        setOpNotice(`已删除知识包 ${pkgId}`);
        setPkgId(null);
        await refreshPackageList();
      } else {
        setOpNotice(`删除失败：${data.error || data.detail || res.status}`);
      }
    } catch (error) {
      if (requestScope === scopeRef.current) {
        setOpNotice(friendlyRequestError(error, '知识包删除失败'));
      }
    } finally {
      if (requestScope === scopeRef.current) {
        setOpBusy('');
      }
    }
  }, [pkgId, refreshPackageList]);

  const downloadPkg = useCallback(async () => {
    if (!pkgId) {
      return;
    }
    const requestScope = scopeRef.current;
    setOpBusy('download');
    setOpNotice('');
    try {
      const q = viewVersion ?
        `?version=${encodeURIComponent(viewVersion)}` :
        '';
      const res = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(pkgId)}/download${q}`,
      );
      if (requestScope !== scopeRef.current) {
        return;
      }
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setOpNotice(`下载失败：${data.error || data.detail || res.status}`);
        return;
      }
      const blob = await res.blob();
      if (requestScope !== scopeRef.current) {
        return;
      }
      const disposition = res.headers.get('Content-Disposition') || '';
      const fileName = resolveZipDownloadFileName(
        pkgId,
        viewVersion,
        disposition,
      );
      startBlobDownload(blob, fileName);
      setOpNotice(describeZipDownload(fileName));
    } catch (error) {
      if (requestScope === scopeRef.current) {
        setOpNotice(friendlyRequestError(error, '知识包下载失败'));
      }
    } finally {
      if (requestScope === scopeRef.current) {
        setOpBusy('');
      }
    }
  }, [pkgId, viewVersion]);

  const clearOperationNotice = useCallback(() => setOpNotice(''), []);

  return {
    canImport,
    canActivate,
    canManage,
    packages: listStateCurrent ? packages : [],
    pkgsLoading: listStateCurrent ?
      pkgsLoading :
      Boolean(listEnabled && workspaceDataReady),
    packagesError: listStateCurrent ? packagesError : '',
    retryPackages: refreshPackageList,
    pkg,
    pkgId: visiblePkgId,
    tree: listStateCurrent ? tree : [],
    treeLoading: listStateCurrent ? treeLoading : false,
    treeError: listStateCurrent ? treeError : '',
    retryTree: () => setTreeReloadKey((value) => value + 1),
    sel,
    selectPkg,
    selectDir,
    selectFile,
    importZip,
    importing: importFlow.phase === IMPORT_PHASE.UPLOADING,
    importReconciling: Boolean(importFlow.reconciliationPending),
    importLocked:
      [
        IMPORT_PHASE.CONFIRMING,
        IMPORT_PHASE.UPLOADING,
        IMPORT_PHASE.CONFLICT,
      ].includes(importFlow.phase) || Boolean(importFlow.reconciliationPending),
    notice: importFlowNotice(importFlow),
    noticeTone: importFlowNoticeTone(importFlow),
    importFlow,
    setImportAcknowledged,
    confirmImportSafety,
    setImportNewVersion,
    resolveImportConflict,
    retryImport,
    reopenImportConflict: () =>
      setImportFlow((current) =>
        reduceImportFlow(current, { type: 'REOPEN_CONFLICT' }),
      ),
    cancelImport,
    cancelImportSelection: cancelImport,
    importPreview: importFlow.result?.preview || null,
    clearImportPreview,
    reloadTree,
    knowledgeRevision,
    refreshKnowledge,
    viewVersion,
    viewingActive,
    versionOverride,
    selectVersion,
    setPkgEnabled,
    setBusinessCheckpointEnabled,
    activateVersion,
    deleteVersion,
    deletePackage,
    downloadPkg,
    clearOperationNotice,
    opBusy,
    opNotice,
  };
}

// 在树中按路径找目录节点（'' 返回根）
export function findDirEntries(tree, dirPath) {
  if (!dirPath) {
    return tree;
  }
  let entries = tree;
  for (const part of dirPath.split('/')) {
    const node = entries.find((e) => e.type === 'dir' && e.name === part);
    if (!node) {
      return [];
    }
    entries = node.children || [];
  }
  return entries;
}
