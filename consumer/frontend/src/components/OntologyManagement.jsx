/**
 * OntologyManagement - ontology object/operation file management.
 */

import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  AlertCircle,
  ChevronLeft,
  CheckCircle,
  Download,
  FileCode,
  FileText,
  FolderOpen,
  FolderPlus,
  Loader2,
  Power,
  RefreshCw,
  Save,
  Trash2,
  Upload,
  X,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { authFetch } from '../shared/api/auth';
import {
  ONTOLOGY_IMPORT_DISCLAIMER,
  canSubmitOntologyImport,
} from '../utils/ontologyImportDisclaimer';
import OntologySelector from './OntologySelector';

const ONTOLOGY_FOLDERS = [
  { key: 'objects', label: '实体' },
  { key: 'operations', label: '逻辑' },
];
const MAX_MD_LINES = 500;
const MAX_MD_BYTES = MAX_MD_LINES * 400;
const utf8Encoder = new TextEncoder();

function countLines(content) {
  if (!content) return 0;
  const matches = content.match(/\r\n|\r|\n/g);
  if (!matches) return 1;
  const endsWithLineBreak = /(\r\n|\r|\n)$/.test(content);
  return matches.length + (endsWithLineBreak ? 0 : 1);
}

function validateMdLimits(content, byteSize) {
  if (byteSize > MAX_MD_BYTES) {
    return `文件过大，最大约 ${MAX_MD_BYTES} 字节（按 ${MAX_MD_LINES} 行估算）`;
  }
  const lineCount = countLines(content);
  if (lineCount > MAX_MD_LINES) {
    return `文件行数超限：${lineCount} 行，最多 ${MAX_MD_LINES} 行`;
  }
  return null;
}

function getDisplayFileName(fileName) {
  return fileName.replace(/\.md$/i, '');
}

function getDisplayFileNameForFolder(fileName) {
  return getDisplayFileName(fileName);
}

function getResponseDownloadFileName(response, fallbackName) {
  const disposition = response.headers.get('content-disposition') || '';
  const encodedMatch = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encodedMatch?.[1]) {
    try {
      return decodeURIComponent(encodedMatch[1]);
    } catch {
      return fallbackName;
    }
  }
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return match?.[1] || fallbackName;
}

function formatPreviewFileSize(size) {
  const value = Number(size || 0);
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function getPreviewFiles(group) {
  return Array.isArray(group?.files) ? group.files : [];
}

function getSelectedPreviewFile(files, selectedPath) {
  if (!files.length) return null;
  return files.find((item) => item.path === selectedPath) || files[0];
}

function OntologyImportPreviewColumn({ title, group, selectedPath, onSelectPath }) {
  const files = getPreviewFiles(group);
  const selectedFile = getSelectedPreviewFile(files, selectedPath);

  useEffect(() => {
    if (!selectedPath && selectedFile?.path) {
      onSelectPath(selectedFile.path);
    }
  }, [selectedPath, selectedFile?.path, onSelectPath]);

  return (
    <section className="ontology-import-preview-column">
      <header className="ontology-import-preview-column-header">
        <div>
          <h4>{title}</h4>
          <span>{group?.total_files || files.length} 个文件{group?.truncated ? '，仅显示前 80 个' : ''}</span>
        </div>
      </header>

      <div className="ontology-import-preview-column-body">
        <div className="ontology-import-preview-file-list">
          {files.length === 0 ? (
            <div className="ontology-import-preview-empty">暂无可预览文件</div>
          ) : files.map((file) => (
            <button
              key={file.path}
              className={`ontology-import-preview-file ${selectedFile?.path === file.path ? 'active' : ''}`}
              type="button"
              onClick={() => onSelectPath(file.path)}
              title={file.path}
            >
              <FileText size={14} />
              <span>{file.path}</span>
              <small>{formatPreviewFileSize(file.size)}</small>
            </button>
          ))}
        </div>

        <div className="ontology-import-preview-content">
          {selectedFile ? (
            <>
              <div className="ontology-import-preview-content-header">
                <strong title={selectedFile.path}>{selectedFile.path}</strong>
                <span>{formatPreviewFileSize(selectedFile.size)}</span>
              </div>
              {selectedFile.binary ? (
                <div className="ontology-import-preview-empty">二进制文件不展示内容</div>
              ) : (
                <pre>{selectedFile.content || '空文件'}{selectedFile.truncated ? '\n\n...内容已截断' : ''}</pre>
              )}
            </>
          ) : (
            <div className="ontology-import-preview-empty">选择文件查看内容</div>
          )}
        </div>
      </div>
    </section>
  );
}

function OntologyImportPreviewModal({ data, onClose }) {
  const [sourcePath, setSourcePath] = useState('');
  const [resultPath, setResultPath] = useState('');

  if (!data) return null;

  return (
    <div className="ontology-confirm-modal-overlay" role="dialog" aria-modal="true">
      <div className="ontology-import-preview-modal">
        <header className="ontology-import-preview-header">
          <div>
            <h3>本体导入预览</h3>
            <p>查看 zip 包内原始文件和导入后生成的本体文件。</p>
          </div>
          <button className="btn btn-icon" type="button" onClick={onClose} title="关闭预览">
            <X size={16} />
          </button>
        </header>

        <div className="ontology-import-preview-grid">
          <OntologyImportPreviewColumn
            title="zip 原始文件"
            group={data.source}
            selectedPath={sourcePath}
            onSelectPath={setSourcePath}
          />
          <OntologyImportPreviewColumn
            title="提取结果"
            group={data.result}
            selectedPath={resultPath}
            onSelectPath={setResultPath}
          />
        </div>

        <div className="ontology-confirm-modal-actions">
          <button className="btn btn-primary btn-compact" type="button" onClick={onClose}>
            完成
          </button>
        </div>
      </div>
    </div>
  );
}

function OntologyManagement({ 
  currentUser, 
  ontologyId,
  ontologyOptions = [],
  onOntologyIdChange,
  currentWorkspaceId = '',
  onNavigateToWorkspace,
  onOntologiesChange,
}) {
  const [activeFolder, setActiveFolder] = useState('operations');
  const [files, setFiles] = useState([]);
  const [selectedFileName, setSelectedFileName] = useState(null);
  const [originalContent, setOriginalContent] = useState('');
  const [editorContent, setEditorContent] = useState('');
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [loadingContent, setLoadingContent] = useState(false);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [importingOntology, setImportingOntology] = useState(false);
  const [downloadingOntology, setDownloadingOntology] = useState(false);
  const [deletingOntology, setDeletingOntology] = useState(false);
  const [updatingOntologyStatus, setUpdatingOntologyStatus] = useState(false);
  const [updatingActiveVersion, setUpdatingActiveVersion] = useState(false);
  const [overwriteConfirmData, setOverwriteConfirmData] = useState(null);
  const [importPreviewData, setImportPreviewData] = useState(null);
  const [pendingImportFile, setPendingImportFile] = useState(null);
  const [importDisclaimerAcknowledged, setImportDisclaimerAcknowledged] = useState(false);
  const [deleteConfirmStep, setDeleteConfirmStep] = useState(0);
  const [deleteConfirmTarget, setDeleteConfirmTarget] = useState(null);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [previewMode, setPreviewMode] = useState('preview');
  const [isFileListCollapsed, setIsFileListCollapsed] = useState(false);
  const fileInputRef = useRef(null);
  const zipInputRef = useRef(null);

  const isAdmin = currentUser?.role === 'admin';
  const activeFolderLabel = ONTOLOGY_FOLDERS.find((item) => item.key === activeFolder)?.label || '';
  const currentOntology = ontologyOptions.find((item) => item.id === ontologyId);
  const currentOntologyName = currentOntology?.name || ontologyId || '';
  const currentOntologyEnabled = currentOntology?.enabled !== false;
  const currentActiveVersion = currentOntology?.active_version || currentOntology?.version || '';
  const currentVersions = useMemo(() => (
    Array.isArray(currentOntology?.versions) && currentOntology.versions.length
      ? currentOntology.versions
      : (currentActiveVersion ? [currentActiveVersion] : [])
  ), [currentOntology?.versions, currentActiveVersion]);
  const [selectedVersion, setSelectedVersion] = useState('');
  const isDirty = editorContent !== originalContent;
  const canEditActiveFile = isAdmin;

  const fetchFiles = useCallback(async (folder, preferredFileName = null) => {
    if (!ontologyId) {
      setFiles([]);
      setSelectedFileName(null);
      setOriginalContent('');
      setEditorContent('');
      return;
    }

    setLoadingFiles(true);
    setError(null);

    try {
      const params = new URLSearchParams({ folder });
      if (selectedVersion) {
        params.set('version', selectedVersion);
      }
      const response = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(ontologyId)}/files?${params.toString()}`
      );
      const data = await response.json();

      if (!response.ok) {
        setFiles([]);
        setSelectedFileName(null);
        setOriginalContent('');
        setEditorContent('');
        setError(data.error || 'Failed to fetch files');
        return;
      }

      const nextFiles = Array.isArray(data.files) ? data.files : [];
      setFiles(nextFiles);

      const hasPreferredName = (
        preferredFileName && nextFiles.some((item) => item.name === preferredFileName)
      );
      const nextSelected = hasPreferredName ? preferredFileName : (nextFiles[0]?.name || null);

      setSelectedFileName(nextSelected);
      if (!nextSelected) {
        setOriginalContent('');
        setEditorContent('');
      }
    } catch {
      setFiles([]);
      setSelectedFileName(null);
      setOriginalContent('');
      setEditorContent('');
      setError('Failed to connect to server');
    } finally {
      setLoadingFiles(false);
    }
  }, [ontologyId, selectedVersion]);

  useEffect(() => {
    const nextVersion = currentActiveVersion || currentVersions[0] || '';
    setSelectedVersion((prev) => (
      prev && currentVersions.includes(prev) ? prev : nextVersion
    ));
  }, [ontologyId, currentActiveVersion, currentVersions]);

  useEffect(() => {
    fetchFiles(activeFolder);
    setSuccess(null);
  }, [activeFolder, fetchFiles]);

  useEffect(() => {
    if (!ontologyId) {
      setFiles([]);
      setSelectedFileName(null);
      setOriginalContent('');
      setEditorContent('');
      return undefined;
    }

    if (!selectedFileName) return undefined;

    const controller = new AbortController();

    const loadFileContent = async () => {
      setLoadingContent(true);
      setError(null);

      try {
        const params = new URLSearchParams({
          folder: activeFolder,
          name: selectedFileName,
        });
        if (selectedVersion) {
          params.set('version', selectedVersion);
        }
        const response = await authFetch(
          `/agentic/ontologies/${encodeURIComponent(ontologyId)}/file?${params.toString()}`,
          { signal: controller.signal }
        );
        const data = await response.json();

        if (!response.ok) {
          setOriginalContent('');
          setEditorContent('');
          setError(data.error || 'Failed to load file');
          return;
        }

        const nextContent = data.content || '';
        setOriginalContent(nextContent);
        setEditorContent(nextContent);
      } catch (loadError) {
        if (loadError?.name === 'AbortError') {
          return;
        }
        setOriginalContent('');
        setEditorContent('');
        setError('Failed to load file');
      } finally {
        setLoadingContent(false);
      }
    };

    loadFileContent();
    return () => controller.abort();
  }, [activeFolder, ontologyId, selectedFileName, selectedVersion]);

  const confirmDiscardChanges = useCallback(() => {
    if (!isDirty) return true;
    return window.confirm('当前文件有未保存修改，确定继续切换吗？');
  }, [isDirty]);

  const handleFolderSwitch = useCallback((folderKey) => {
    if (folderKey === activeFolder) return;
    if (!confirmDiscardChanges()) return;

    setActiveFolder(folderKey);
    setSelectedFileName(null);
    setOriginalContent('');
    setEditorContent('');
    setSuccess(null);
  }, [activeFolder, confirmDiscardChanges]);

  const handleFileSelect = useCallback((fileName) => {
    if (fileName === selectedFileName) return;
    if (!confirmDiscardChanges()) return;

    setSelectedFileName(fileName);
    setSuccess(null);
  }, [confirmDiscardChanges, selectedFileName]);

  const handleSave = useCallback(async () => {
    if (!isAdmin) {
      setError('仅管理员可以保存文件');
      return;
    }
    if (!selectedFileName || !isDirty) {
      return;
    }

    const contentByteSize = utf8Encoder.encode(editorContent).length;
    const validationError = validateMdLimits(editorContent, contentByteSize);
    if (validationError) {
      setError(validationError);
      return;
    }

    setSaving(true);
    setError(null);
    setSuccess(null);

    try {
      const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/file`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          folder: activeFolder,
          name: selectedFileName,
          content: editorContent,
          version: selectedVersion,
        }),
      });
      const data = await response.json();

      if (!response.ok) {
        setError(data.error || 'Failed to save file');
        return;
      }

      setOriginalContent(editorContent);
      setSuccess(`已保存 ${selectedFileName}`);
      fetchFiles(activeFolder, selectedFileName);
    } catch {
      setError('Failed to save file');
    } finally {
      setSaving(false);
    }
  }, [isAdmin, selectedFileName, isDirty, activeFolder, ontologyId, editorContent, selectedVersion, fetchFiles]);

  const handleUpload = useCallback(async (file) => {
    if (!isAdmin) {
      setError('仅管理员可以上传文件');
      return;
    }
    if (!file) return;

    if (!file.name.toLowerCase().endsWith('.md')) {
      setError('仅支持上传 .md 文件');
      return;
    }

    if (file.size > MAX_MD_BYTES) {
      setError(`文件过大，最大约 ${MAX_MD_BYTES} 字节（按 ${MAX_MD_LINES} 行估算）`);
      return;
    }

    let content = '';
    try {
      content = await file.text();
    } catch {
      setError('读取上传文件失败');
      return;
    }

    const contentByteSize = utf8Encoder.encode(content).length;
    const validationError = validateMdLimits(content, contentByteSize);
    if (validationError) {
      setError(validationError);
      return;
    }

    setUploading(true);
    setError(null);
    setSuccess(null);

    const formData = new FormData();
    formData.append('folder', activeFolder);
    if (selectedVersion) {
      formData.append('version', selectedVersion);
    }
    formData.append('file', file);

    try {
      const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/upload`, {
        method: 'POST',
        body: formData,
      });
      const data = await response.json();

      if (!response.ok) {
        setError(data.error || 'Upload failed');
        return;
      }

      setSuccess(
        data.overwritten
          ? `已覆盖 ${activeFolderLabel} 文件：${data.name}`
          : `已上传 ${activeFolderLabel} 文件：${data.name}`
      );
      fetchFiles(activeFolder, data.name);
    } catch {
      setError('Failed to upload file');
    } finally {
      setUploading(false);
    }
  }, [isAdmin, activeFolder, activeFolderLabel, ontologyId, selectedVersion, fetchFiles]);

  const handleUploadFileSelect = useCallback((e) => {
    if (!isAdmin) {
      return;
    }
    if (e.target.files && e.target.files[0]) {
      handleUpload(e.target.files[0]);
    }
    e.target.value = '';
  }, [handleUpload, isAdmin]);

  const importOntologyZip = useCallback(async (file, options = {}) => {
    const formData = new FormData();
    formData.append('file', file);
    if (options.overwrite) {
      formData.append('overwrite', 'true');
    }
    if (options.action) {
      formData.append('action', options.action);
    }
    if (options.newVersion) {
      formData.append('new_version', options.newVersion);
    }

    const response = await authFetch('/agentic/ontologies/import-zip', {
      method: 'POST',
      body: formData,
    });
    const data = await response.json().catch(() => ({}));
    return { response, data };
  }, []);

  const applyImportResult = useCallback((data) => {
    const importedOntologyId = data.ontology_id || '';
    const importMode = data.import_mode || 'source';
    const importActionText = importMode === 'converted' ? '直接导入' : '创建并导入';
    if (importedOntologyId) {
      onOntologyIdChange?.(importedOntologyId);
    }
    if (data.version || data.active_version) {
      setSelectedVersion(data.version || data.active_version);
    }

    if (data.overwritten) {
      setSuccess(`本体已覆盖${importMode === 'converted' ? '导入' : '转换导入'}：${data.ontology_name || importedOntologyId}（实体 ${data.objects_count || 0}，逻辑 ${data.operations_count || 0}）`);
    } else {
      setSuccess(`本体已${importActionText}：${data.ontology_name || importedOntologyId}（实体 ${data.objects_count || 0}，逻辑 ${data.operations_count || 0}）`);
    }

    if (importedOntologyId && importedOntologyId === ontologyId) {
      fetchFiles(activeFolder);
    }

    onOntologiesChange?.();
    setImportPreviewData(data.preview || null);
  }, [onOntologyIdChange, ontologyId, fetchFiles, activeFolder, onOntologiesChange]);

  const handleImportZip = useCallback(async (file) => {
    if (!isAdmin) {
      setError('仅管理员可以上传本体压缩包');
      return;
    }
    if (!file) return;

    if (!file.name.toLowerCase().endsWith('.zip')) {
      setError('仅支持上传 .zip 压缩包');
      return;
    }

    setImportingOntology(true);
    setError(null);
    setSuccess(null);
    setImportPreviewData(null);

    try {
      const { response, data } = await importOntologyZip(file);

      if (response.status === 409 && data?.requires_confirmation) {
        setOverwriteConfirmData({
          file,
          ontologyId: data.ontology_id || '',
          ontologyName: data.ontology_name || '',
          conflictType: data.conflict_type || '',
          version: data.version || '',
          activeVersion: data.active_version || '',
          versions: Array.isArray(data.versions) ? data.versions : [],
          newVersion: '',
          message: data.error || '',
        });
        return;
      }

      if (!response.ok) {
        setError(data.error || '本体导入失败');
        return;
      }

      applyImportResult(data);
    } catch {
      setError('本体导入失败');
    } finally {
      setImportingOntology(false);
    }
  }, [isAdmin, importOntologyZip, applyImportResult]);

  const handleConfirmOverwriteImport = useCallback(async (action) => {
    if (!overwriteConfirmData?.file) {
      setOverwriteConfirmData(null);
      return;
    }

    const pendingFile = overwriteConfirmData.file;
    const newVersion = String(overwriteConfirmData.newVersion || '').trim();
    if (action === 'create_version' && !newVersion) {
      setError('请输入新增版本号');
      return;
    }
    setOverwriteConfirmData(null);
    setImportingOntology(true);
    setError(null);
    setSuccess(null);
    setImportPreviewData(null);

    try {
      const { response, data } = await importOntologyZip(pendingFile, {
        action,
        newVersion: action === 'create_version' ? newVersion : '',
        overwrite: action !== 'create_version',
      });
      if (!response.ok) {
        setError(data.error || '覆盖导入失败');
        return;
      }
      applyImportResult(data);
    } catch {
      setError('覆盖导入失败');
    } finally {
      setImportingOntology(false);
    }
  }, [overwriteConfirmData, importOntologyZip, applyImportResult]);

  const handleCancelOverwriteImport = useCallback(() => {
    setOverwriteConfirmData(null);
    setSuccess('已取消覆盖导入');
  }, []);

  const handleDownloadOntology = useCallback(async () => {
    if (!currentWorkspaceId) {
      setError('请先创建工作空间');
      return;
    }
    if (!ontologyId) {
      setError('请先选择要下载的本体');
      return;
    }

    setDownloadingOntology(true);
    setError(null);
    setSuccess(null);

    try {
      const params = new URLSearchParams();
      if (selectedVersion) {
        params.set('version', selectedVersion);
      }
      const response = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(ontologyId)}/download${params.toString() ? `?${params.toString()}` : ''}`
      );

      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        setError(data.error || data.detail || '本体下载失败');
        return;
      }

      const blob = await response.blob();
      const fileName = getResponseDownloadFileName(response, `${ontologyId}.zip`);
      const objectUrl = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(objectUrl);
      setSuccess(`已下载本体：${currentOntologyName || ontologyId}${selectedVersion ? `（${selectedVersion}）` : ''}`);
    } catch {
      setError('本体下载失败');
    } finally {
      setDownloadingOntology(false);
    }
  }, [currentWorkspaceId, ontologyId, currentOntologyName, selectedVersion]);

  const handleDeleteOntologyVersion = useCallback(async () => {
    if (!isAdmin) {
      setError('仅管理员可以删除本体版本');
      return;
    }
    if (!ontologyId) {
      setError('请先选择要删除版本的本体');
      return;
    }
    if (!selectedVersion) {
      setError('请先选择要删除的版本');
      return;
    }

    setDeletingOntology(true);
    setDeleteConfirmStep(0);
    setDeleteConfirmTarget(null);
    setError(null);
    setSuccess(null);

    try {
      const response = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(ontologyId)}/versions/${encodeURIComponent(selectedVersion)}`,
        {
          method: 'DELETE',
        }
      );
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        setError(data.error || data.detail || '删除本体版本失败');
        return;
      }

      const nextVersion = data.active_version || data.versions?.[0] || '';
      setSelectedVersion(nextVersion);
      setFiles([]);
      setSelectedFileName(null);
      setOriginalContent('');
      setEditorContent('');
      setSuccess(`已删除版本：${currentOntologyName || ontologyId} ${data.deleted_version || selectedVersion}`);
      onOntologiesChange?.();
    } catch {
      setError('删除本体版本失败');
    } finally {
      setDeletingOntology(false);
    }
  }, [isAdmin, ontologyId, selectedVersion, currentOntologyName, onOntologiesChange]);

  const handleDeleteOntology = useCallback(async () => {
    if (!isAdmin) {
      setError('仅管理员可以删除本体');
      return;
    }
    if (!ontologyId) {
      setError('请先选择要删除的本体');
      return;
    }

    setDeletingOntology(true);
    setDeleteConfirmStep(0);
    setDeleteConfirmTarget(null);
    setError(null);
    setSuccess(null);

    try {
      const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}`, {
        method: 'DELETE',
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        setError(data.error || data.detail || '删除本体失败');
        return;
      }

      const nextOntologyId = ontologyOptions.find((item) => item.id !== ontologyId)?.id || '';
      onOntologyIdChange?.(nextOntologyId);
      setSelectedVersion('');
      setFiles([]);
      setSelectedFileName(null);
      setOriginalContent('');
      setEditorContent('');
      setSuccess(`已删除本体：${currentOntologyName || ontologyId}`);
      onOntologiesChange?.();
    } catch {
      setError('删除本体失败');
    } finally {
      setDeletingOntology(false);
    }
  }, [isAdmin, ontologyId, ontologyOptions, onOntologyIdChange, currentOntologyName, onOntologiesChange]);

  const handleToggleOntologyStatus = useCallback(async () => {
    if (!isAdmin) {
      setError('仅管理员可以修改本体状态');
      return;
    }
    if (!ontologyId) {
      setError('请先选择本体');
      return;
    }

    const nextEnabled = !currentOntologyEnabled;
    setUpdatingOntologyStatus(true);
    setError(null);
    setSuccess(null);

    try {
      const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/status`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: nextEnabled }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        setError(data.error || data.detail || '更新本体状态失败');
        return;
      }
      setSuccess(`已${nextEnabled ? '启用' : '禁用'}本体：${currentOntologyName || ontologyId}`);
      onOntologiesChange?.();
    } catch {
      setError('更新本体状态失败');
    } finally {
      setUpdatingOntologyStatus(false);
    }
  }, [isAdmin, ontologyId, currentOntologyEnabled, currentOntologyName, onOntologiesChange]);

  const handleSetActiveVersion = useCallback(async () => {
    if (!isAdmin) {
      setError('仅管理员可以修改激活版本');
      return;
    }
    if (!ontologyId || !selectedVersion) {
      setError('请先选择本体版本');
      return;
    }

    setUpdatingActiveVersion(true);
    setError(null);
    setSuccess(null);

    try {
      const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/active-version`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: selectedVersion }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        setError(data.error || data.detail || '更新激活版本失败');
        return;
      }
      setSuccess(`已设为激活版本：${currentOntologyName || ontologyId} ${selectedVersion}`);
      onOntologiesChange?.();
    } catch {
      setError('更新激活版本失败');
    } finally {
      setUpdatingActiveVersion(false);
    }
  }, [isAdmin, ontologyId, selectedVersion, currentOntologyName, onOntologiesChange]);

  const handleImportZipSelect = useCallback((e) => {
    if (!isAdmin) {
      return;
    }
    if (e.target.files && e.target.files[0]) {
      const file = e.target.files[0];
      if (!file.name.toLowerCase().endsWith('.zip')) {
        setError('仅支持上传 .zip 压缩包');
      } else {
        setError(null);
        setSuccess(null);
        setImportPreviewData(null);
        setPendingImportFile(file);
        setImportDisclaimerAcknowledged(false);
      }
    }
    e.target.value = '';
  }, [isAdmin]);

  const handleConfirmPendingImportZip = useCallback(() => {
    if (!pendingImportFile) {
      setPendingImportFile(null);
      return;
    }
    if (!canSubmitOntologyImport({ acknowledged: importDisclaimerAcknowledged })) {
      setError('请先确认本体来源、内容和执行风险');
      return;
    }
    const file = pendingImportFile;
    setPendingImportFile(null);
    setImportDisclaimerAcknowledged(false);
    handleImportZip(file);
  }, [pendingImportFile, importDisclaimerAcknowledged, handleImportZip]);

  const handleCancelPendingImportZip = useCallback(() => {
    setPendingImportFile(null);
    setImportDisclaimerAcknowledged(false);
  }, []);

  return (
    <div className="skill-management-content">
      {!currentWorkspaceId && (
        <div className="workspace-empty-guide">
          <div className="workspace-empty-guide-card">
            <FolderPlus size={36} className="workspace-empty-guide-icon" />
            <h3 className="workspace-empty-guide-title">开始使用前，请先创建工作空间</h3>
            <p className="workspace-empty-guide-desc">工作空间用于管理本体、技能和运行资源，是使用系统的第一步。</p>
            <button
              className="btn btn-primary workspace-empty-guide-btn"
              onClick={() => onNavigateToWorkspace?.()}
              type="button"
            >
              <FolderPlus size={16} />
              <span>新建工作空间</span>
            </button>
          </div>
        </div>
      )}
      <div className="skill-management-body">
        {/* New Consolidated Management Header */}
        <header className="sops-header-line">
          <div className="sops-header-left">
            <OntologySelector 
              ontologyId={ontologyId}
              ontologyOptions={ontologyOptions}
              onOntologyIdChange={(id) => {
                onOntologyIdChange?.(id);
                setSelectedVersion('');
                setSelectedFileName(null);
                setOriginalContent('');
                setEditorContent('');
              }}
              onBeforeChange={() => confirmDiscardChanges()}
            />

            {ontologyId && (
              <>
                <label className="ontology-version-select">
                  <span>查看版本</span>
                  <select
                    value={selectedVersion}
                    onChange={(e) => {
                      if (!confirmDiscardChanges()) return;
                      setSelectedVersion(e.target.value);
                      setSelectedFileName(null);
                      setOriginalContent('');
                      setEditorContent('');
                      setSuccess(null);
                    }}
                    disabled={currentVersions.length === 0 || loadingFiles || loadingContent || saving}
                  >
                    {currentVersions.map((version) => (
                      <option key={version} value={version}>
                        {version}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="ontology-state-summary" aria-label="本体和版本状态">
                  <span className="ontology-state-item">
                    <span className="ontology-state-label">本体状态</span>
                    <span className={`ontology-status-badge ${currentOntologyEnabled ? 'enabled' : 'disabled'}`}>
                      {currentOntologyEnabled ? '已启用' : '已禁用'}
                    </span>
                  </span>
                  <span className="ontology-state-item">
                    <span className="ontology-state-label">激活版本</span>
                    <span className="ontology-version-badge">
                      {currentActiveVersion || '未设置'}
                    </span>
                  </span>
                </div>
              </>
            )}

            <div className="sops-header-divider"></div>

            <div className="sops-segmented-tabs">
              {ONTOLOGY_FOLDERS.map((folder) => (
                <button
                  key={folder.key}
                  className={`sops-segmented-tab ${activeFolder === folder.key ? 'active' : ''}`}
                  onClick={() => handleFolderSwitch(folder.key)}
                  type="button"
                >
                  {folder.label}
                </button>
              ))}
            </div>
          </div>

          <div className="sops-header-right">
            <button
              className="btn btn-secondary"
              onClick={handleDownloadOntology}
              disabled={!currentWorkspaceId || !ontologyId || downloadingOntology || importingOntology || deletingOntology || updatingOntologyStatus}
              type="button"
            >
              {downloadingOntology ? (
                <>
                  <Loader2 size={14} className="spin" />
                  <span>下载中</span>
                </>
              ) : (
                <>
                  <Download size={14} />
                  <span>{currentWorkspaceId ? '下载本体(zip)' : '请先创建工作空间'}</span>
                </>
              )}
            </button>
            <button
              className="btn btn-secondary"
              onClick={handleToggleOntologyStatus}
              disabled={!isAdmin || !ontologyId || importingOntology || downloadingOntology || deletingOntology || updatingOntologyStatus}
              type="button"
            >
              {updatingOntologyStatus ? (
                <>
                  <Loader2 size={14} className="spin" />
                  <span>更新中</span>
                </>
              ) : (
                <>
                  <Power size={14} />
                  <span>{currentOntologyEnabled ? '禁用本体' : '启用本体'}</span>
                </>
              )}
            </button>
            <button
              className="btn btn-secondary"
              onClick={handleSetActiveVersion}
              disabled={
                !isAdmin
                || !ontologyId
                || !selectedVersion
                || selectedVersion === currentActiveVersion
                || importingOntology
                || downloadingOntology
                || deletingOntology
                || updatingOntologyStatus
                || updatingActiveVersion
              }
              type="button"
            >
              {updatingActiveVersion ? (
                <>
                  <Loader2 size={14} className="spin" />
                  <span>激活中</span>
                </>
              ) : (
                <>
                  <CheckCircle size={14} />
                  <span>{selectedVersion && selectedVersion === currentActiveVersion ? '已是激活版本' : '设为激活版本'}</span>
                </>
              )}
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => isAdmin && currentWorkspaceId && zipInputRef.current?.click()}
              disabled={!isAdmin || !currentWorkspaceId || importingOntology || downloadingOntology || deletingOntology || updatingOntologyStatus}
              type="button"
            >
              {importingOntology ? (
                <>
                  <Loader2 size={14} className="spin" />
                  <span>导入中</span>
                </>
              ) : (
                <>
                  <Upload size={14} />
                  <span>{currentWorkspaceId ? '上传本体(zip)' : '请先创建工作空间'}</span>
                </>
              )}
            </button>
            <button
              className="btn btn-secondary ontology-danger-btn"
              onClick={() => {
                setDeleteConfirmTarget('version');
                setDeleteConfirmStep(1);
              }}
              disabled={
                !isAdmin
                || !ontologyId
                || !selectedVersion
                || deletingOntology
                || importingOntology
                || downloadingOntology
                || updatingOntologyStatus
              }
              type="button"
            >
              {deletingOntology ? (
                <>
                  <Loader2 size={14} className="spin" />
                  <span>删除中</span>
                </>
              ) : (
                <>
                  <Trash2 size={14} />
                  <span>删除版本</span>
                </>
              )}
            </button>
            <button
              className="btn btn-secondary ontology-danger-btn"
              onClick={() => {
                setDeleteConfirmTarget('ontology');
                setDeleteConfirmStep(1);
              }}
              disabled={
                !isAdmin
                || !ontologyId
                || deletingOntology
                || importingOntology
                || downloadingOntology
                || updatingOntologyStatus
              }
              type="button"
            >
              {deletingOntology ? (
                <>
                  <Loader2 size={14} className="spin" />
                  <span>删除中</span>
                </>
              ) : (
                <>
                  <Trash2 size={14} />
                  <span>删除本体</span>
                </>
              )}
            </button>
          </div>
        </header>

        {error && (
          <div className="skill-message error">
            <AlertCircle size={16} />
            <span>{error}</span>
          </div>
        )}
        {success && (
          <div className="skill-message success">
            <CheckCircle size={16} />
            <span>{success}</span>
          </div>
        )}
        {!isAdmin && (
          <div className="skill-message">
            <AlertCircle size={16} />
            <span>当前为只读模式，仅管理员可上传和保存。</span>
          </div>
        )}
        {isAdmin && currentWorkspaceId && (
          <div className="skill-message warning ontology-import-disclaimer">
            <AlertCircle size={16} />
            <span>{ONTOLOGY_IMPORT_DISCLAIMER.inline}</span>
          </div>
        )}

        <div className={`sops-workspace ${isFileListCollapsed ? 'file-list-collapsed' : ''}`}>
          {!isFileListCollapsed && (
            <aside className="sops-file-list-panel">
              <div className="sops-file-list-header">
                <div className="sops-file-list-title">
                  <FolderOpen size={16} />
                  <span>{activeFolderLabel}文件</span>
                  <span className="sops-file-count-badge">{files.length}</span>
                </div>
                <div className="sops-file-list-actions">
                  <button
                    className="btn btn-icon"
                    onClick={() => fetchFiles(activeFolder, selectedFileName)}
                    disabled={loadingFiles}
                    title="刷新文件列表"
                    type="button"
                  >
                    <RefreshCw size={16} className={loadingFiles ? 'spin' : ''} />
                  </button>
                  <button
                    className="btn btn-secondary btn-compact"
                    onClick={() => isAdmin && fileInputRef.current?.click()}
                    disabled={!canEditActiveFile || uploading}
                    title="上传 Markdown 文件"
                    type="button"
                  >
                    {uploading ? (
                      <>
                        <Loader2 size={14} className="spin" />
                        <span>上传中</span>
                      </>
                    ) : (
                      <>
                        <Upload size={14} />
                      </>
                    )}
                  </button>
                  <button
                    className="btn btn-icon"
                    onClick={() => setIsFileListCollapsed(true)}
                    title="折叠文件列表"
                    type="button"
                  >
                    <ChevronLeft size={16} />
                  </button>
                </div>
              </div>

              <input
                ref={fileInputRef}
                type="file"
                accept=".md"
                onChange={handleUploadFileSelect}
                style={{ display: 'none' }}
              />
              <input
                ref={zipInputRef}
                type="file"
                accept=".zip"
                onChange={handleImportZipSelect}
                style={{ display: 'none' }}
              />

              {loadingFiles ? (
                <div className="skill-loading">
                  <Loader2 size={22} className="spin" />
                  <span>加载文件中...</span>
                </div>
              ) : files.length === 0 ? (
                <div className="skill-empty">
                  <FileCode size={28} />
                  <span>暂无文件</span>
                </div>
              ) : (
                <div className="skill-list">
                  {files.map((file) => (
                    <button
                      key={file.name}
                      className={`skill-item ${selectedFileName === file.name ? 'selected' : ''}`}
                      onClick={() => handleFileSelect(file.name)}
                      type="button"
                    >
                      <div className="skill-item-icon">
                        <FileCode size={18} />
                      </div>
                      <div className="skill-item-content">
                        <div className="skill-item-name">{getDisplayFileNameForFolder(file.name, activeFolder)}</div>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </aside>
          )}

          <section className="sops-editor-panel">
            <div className="sops-editor-header">
              <div className="sops-editor-title-row">
                {isFileListCollapsed && (
                  <button
                    className="sops-file-list-expand-btn"
                    onClick={() => setIsFileListCollapsed(false)}
                    aria-label="展开文件列表"
                    type="button"
                  >
                    <FolderOpen size={15} />
                  </button>
                )}
                <div className="sops-editor-file">{selectedFileName || '未选择文件'}</div>
              </div>
              <div className="sops-editor-actions">
                <div className="sops-preview-modes">
                  <button
                    className={`sops-preview-mode ${previewMode === 'edit' ? 'active' : ''}`}
                    onClick={() => setPreviewMode('edit')}
                    type="button"
                  >
                    编辑
                  </button>
                  <button
                    className={`sops-preview-mode ${previewMode === 'preview' ? 'active' : ''}`}
                    onClick={() => setPreviewMode('preview')}
                    type="button"
                  >
                    渲染
                  </button>
                  <button
                    className={`sops-preview-mode ${previewMode === 'split' ? 'active' : ''}`}
                    onClick={() => setPreviewMode('split')}
                    type="button"
                  >
                    同步
                  </button>
                </div>
                <button
                  className="btn btn-primary"
                  onClick={handleSave}
                  disabled={!canEditActiveFile || !selectedFileName || !isDirty || saving || loadingContent}
                  type="button"
                  title="保存修改"
                >
                  {saving ? (
                    <>
                      <Loader2 size={14} className="spin" />
                      <span>保存中</span>
                    </>
                  ) : (
                    <>
                      <Save size={14} />
                      <span>保存修改</span>
                    </>
                  )}
                </button>
              </div>
            </div>

            {!selectedFileName ? (
              <div className="sops-editor-empty">请选择一个 Markdown 文件</div>
            ) : loadingContent ? (
              <div className="sops-editor-empty">
                <Loader2 size={22} className="spin" />
                <span>加载内容中...</span>
              </div>
            ) : previewMode === 'edit' ? (
              <textarea
                className="sops-editor-textarea"
                value={editorContent}
                onChange={(e) => {
                  setEditorContent(e.target.value);
                  setSuccess(null);
                }}
                readOnly={!canEditActiveFile}
                spellCheck={false}
              />
            ) : previewMode === 'preview' ? (
              <div className="sops-markdown-preview">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {editorContent || '*空内容*'}
                </ReactMarkdown>
              </div>
            ) : (
              <div className="sops-editor-split">
                <textarea
                  className="sops-editor-textarea split"
                  value={editorContent}
                  onChange={(e) => {
                    setEditorContent(e.target.value);
                    setSuccess(null);
                  }}
                  readOnly={!canEditActiveFile}
                  spellCheck={false}
                />
                <div className="sops-markdown-preview split">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {editorContent || '*空内容*'}
                  </ReactMarkdown>
                </div>
              </div>
            )}
          </section>
        </div>
      </div>

      {overwriteConfirmData && (
        <div className="ontology-confirm-modal-overlay" role="dialog" aria-modal="true">
          <div className="ontology-confirm-modal">
            <h3>本体版本冲突</h3>
            <p>{overwriteConfirmData.message || `本体 ${overwriteConfirmData.ontologyId} 已存在，是否覆盖导入？`}</p>
            <p className="ontology-confirm-warning">{ONTOLOGY_IMPORT_DISCLAIMER.body}</p>
            <label className="ontology-version-input">
              <span>新增版本号</span>
              <input
                value={overwriteConfirmData.newVersion || ''}
                onChange={(e) => setOverwriteConfirmData((prev) => (
                  prev ? { ...prev, newVersion: e.target.value } : prev
                ))}
                placeholder="例如 1.0.1"
              />
            </label>
            <div className="ontology-confirm-modal-actions">
              <button
                className="btn btn-secondary btn-compact"
                type="button"
                onClick={handleCancelOverwriteImport}
              >
                取消
              </button>
              <button
                className="btn btn-secondary btn-compact"
                type="button"
                onClick={() => handleConfirmOverwriteImport('create_version')}
              >
                新增版本
              </button>
              <button
                className="btn btn-primary btn-compact"
                type="button"
                onClick={() => handleConfirmOverwriteImport(
                  overwriteConfirmData.conflictType === 'missing_version_existing'
                    ? 'overwrite_active'
                    : 'overwrite_matching'
                )}
              >
                {overwriteConfirmData.conflictType === 'missing_version_existing'
                  ? `覆盖激活版本${overwriteConfirmData.activeVersion ? ` ${overwriteConfirmData.activeVersion}` : ''}`
                  : `覆盖版本${overwriteConfirmData.version ? ` ${overwriteConfirmData.version}` : ''}`}
              </button>
            </div>
          </div>
        </div>
      )}

      {pendingImportFile && (
        <div className="ontology-confirm-modal-overlay" role="dialog" aria-modal="true">
          <div className="ontology-confirm-modal">
            <h3>{ONTOLOGY_IMPORT_DISCLAIMER.title}</h3>
            <p>{ONTOLOGY_IMPORT_DISCLAIMER.body}</p>
            <label className="ontology-import-acknowledgement">
              <input
                type="checkbox"
                checked={importDisclaimerAcknowledged}
                onChange={(event) => setImportDisclaimerAcknowledged(event.target.checked)}
              />
              <span>{ONTOLOGY_IMPORT_DISCLAIMER.acknowledgement}</span>
            </label>
            <div className="ontology-confirm-modal-actions">
              <button
                className="btn btn-secondary btn-compact"
                type="button"
                onClick={handleCancelPendingImportZip}
              >
                取消
              </button>
              <button
                className="btn btn-primary btn-compact"
                type="button"
                onClick={handleConfirmPendingImportZip}
                disabled={!canSubmitOntologyImport({ acknowledged: importDisclaimerAcknowledged }) || importingOntology}
              >
                我已确认，继续导入
              </button>
            </div>
          </div>
        </div>
      )}

      {importPreviewData && (
        <OntologyImportPreviewModal
          data={importPreviewData}
          onClose={() => setImportPreviewData(null)}
        />
      )}

      {deleteConfirmStep > 0 && (
        <div className="ontology-confirm-modal-overlay" role="dialog" aria-modal="true">
          <div className="ontology-confirm-modal ontology-confirm-modal-danger">
            <h3>
              {deleteConfirmStep === 1
                ? (deleteConfirmTarget === 'ontology' ? '确认删除本体？' : '确认删除版本？')
                : '请再次确认删除'}
            </h3>
            <p>
              {deleteConfirmStep === 1 && deleteConfirmTarget === 'ontology'
                ? `将删除本体“${currentOntologyName || ontologyId}”（ID: ${ontologyId}）及其全部版本、实体、逻辑和脚本。`
                : null}
              {deleteConfirmStep === 1 && deleteConfirmTarget !== 'ontology'
                ? `将删除本体“${currentOntologyName || ontologyId}”（ID: ${ontologyId}）的版本 ${selectedVersion}，以及该版本的全部实体/逻辑/脚本。`
                : null}
              {deleteConfirmStep !== 1 && deleteConfirmTarget === 'ontology'
                ? `删除后不可恢复。确认彻底删除“${currentOntologyName || ontologyId}”？`
                : null}
              {deleteConfirmStep !== 1 && deleteConfirmTarget !== 'ontology'
                ? `删除后不可恢复。确认删除版本 ${selectedVersion}？`
                : null}
            </p>
            <div className="ontology-confirm-modal-actions">
              <button
                className="btn btn-secondary btn-compact"
                type="button"
                onClick={() => {
                  setDeleteConfirmStep(0);
                  setDeleteConfirmTarget(null);
                }}
              >
                取消
              </button>
              {deleteConfirmStep === 1 ? (
                <button
                  className="btn btn-secondary btn-compact ontology-danger-btn"
                  type="button"
                  onClick={() => setDeleteConfirmStep(2)}
                >
                  继续
                </button>
              ) : (
                <button
                  className="btn btn-secondary btn-compact ontology-danger-btn"
                  type="button"
                  onClick={
                    deleteConfirmTarget === 'ontology'
                      ? handleDeleteOntology
                      : handleDeleteOntologyVersion
                  }
                >
                  确认删除
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default OntologyManagement;
