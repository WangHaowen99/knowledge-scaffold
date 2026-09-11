import { useRef, useState } from 'react';

import { Modal, SectionCard } from '../../../shared/ui/primitives';
import { fetchSystemConfigTemplate, importSystemConfig } from './api';
import { parseSystemConfigImport, SYSTEM_CONFIG_SECTION_LABELS } from './model';
import { SystemConfigReadOnlyNotice } from './SystemConfigModule';
import './systemConfig.css';

export const MAX_SYSTEM_CONFIG_IMPORT_BYTES = 2 * 1024 * 1024;

function saveBlob(blob, fileName) {
  const objectUrl = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(objectUrl);
}

function sectionNames(sections) {
  return sections
    .map((section) => SYSTEM_CONFIG_SECTION_LABELS[section] || section)
    .join('、');
}

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
export default function SystemConfigTransfer({
  canEdit = false,
  request,
  onImported,
  className = '',
}) {
  const inputRef = useRef(null);
  const [busyAction, setBusyAction] = useState('');
  const [feedback, setFeedback] = useState(null);
  const [pendingImport, setPendingImport] = useState(null);
  const [overwriteConfirmed, setOverwriteConfirmed] = useState(false);

  const closePreview = () => {
    if (busyAction === 'import') {
      return;
    }
    setPendingImport(null);
    setOverwriteConfirmed(false);
  };

  const downloadTemplate = async () => {
    if (!canEdit || busyAction) {
      return;
    }
    setBusyAction('download');
    setFeedback(null);
    try {
      const result = await fetchSystemConfigTemplate(request);
      saveBlob(result.blob, result.fileName);
      setFeedback({ type: 'success', message: '系统配置模板已下载' });
    } catch (error) {
      setFeedback({
        type: 'error',
        message: error?.message || '下载系统配置模板失败',
      });
    } finally {
      setBusyAction('');
    }
  };

  const chooseImportFile = () => {
    if (!canEdit || busyAction) {
      return;
    }
    setFeedback(null);
    if (!inputRef.current) {
      return;
    }
    inputRef.current.value = '';
    inputRef.current.click();
  };

  const readImportFile = async (event) => {
    const [file] = event.target.files || [];
    if (!file || !canEdit) {
      return;
    }
    setFeedback(null);
    setPendingImport(null);
    setOverwriteConfirmed(false);
    try {
      if (file.size > MAX_SYSTEM_CONFIG_IMPORT_BYTES) {
        throw new Error('导入文件不能超过 2 MB');
      }
      const preview = parseSystemConfigImport(await file.text());
      setPendingImport({ ...preview, fileName: file.name });
    } catch (error) {
      setFeedback({
        type: 'error',
        message: error?.message || '解析系统配置文件失败',
      });
    }
  };

  const confirmImport = async () => {
    const importBlocked =
      !canEdit || !pendingImport || !overwriteConfirmed || busyAction;
    if (importBlocked) {
      return;
    }
    setBusyAction('import');
    setFeedback(null);
    try {
      const payload = await importSystemConfig(pendingImport.config, request);
      const importedSections = Array.isArray(payload?.imported_sections) ?
        payload.imported_sections :
        pendingImport.sections;
      setFeedback({
        type: 'success',
        message: `配置导入成功，已覆盖：${sectionNames(importedSections)}`,
      });
      setPendingImport(null);
      setOverwriteConfirmed(false);
      onImported?.(payload?.config || {}, payload);
    } catch (error) {
      setFeedback({
        type: 'error',
        message: error?.message || '导入系统配置失败',
      });
    } finally {
      setBusyAction('');
    }
  };

  return (
    <SectionCard
      title="配置迁移"
      description="下载后端模板，或预览并覆盖导入合法的系统配置 section。空密钥占位不会覆盖已保存密钥。"
      className={`system-config-transfer ${className}`.trim()}
      actions={
        <div className="system-config-transfer-actions">
          <button
            type="button"
            className="btn"
            disabled={!canEdit || Boolean(busyAction)}
            onClick={downloadTemplate}
          >
            {busyAction === 'download' ? '下载中…' : '下载 JSON 模板'}
          </button>
          <button
            type="button"
            className="btn btn-p"
            disabled={!canEdit || Boolean(busyAction)}
            onClick={chooseImportFile}
          >
            预览并导入
          </button>
          <input
            ref={inputRef}
            className="system-config-file-input"
            type="file"
            accept=".json,application/json"
            aria-label="选择系统配置 JSON 文件"
            onChange={readImportFile}
          />
        </div>
      }
    >
      {!canEdit && <SystemConfigReadOnlyNotice />}
      {feedback && (
        <div
          className={`system-config-feedback ${feedback.type}`}
          role={feedback.type === 'error' ? 'alert' : 'status'}
          aria-live="polite"
        >
          <span>{feedback.message}</span>
        </div>
      )}
      <p className="system-config-transfer-note">
        导入会覆盖预览中列出的模块。未出现在文件中的模块保持不变；不支持的
        section 会在提交前被阻止。
      </p>

      {pendingImport && (
        <Modal
          wide
          title="确认覆盖系统配置"
          description={`文件：${pendingImport.fileName}`}
          onClose={closePreview}
          actions={
            <>
              <button
                type="button"
                className="btn"
                disabled={busyAction === 'import'}
                onClick={closePreview}
              >
                取消
              </button>
              <button
                type="button"
                className="btn btn-p"
                disabled={!overwriteConfirmed || busyAction === 'import'}
                onClick={confirmImport}
              >
                {busyAction === 'import' ? '导入中…' : '确认覆盖并导入'}
              </button>
            </>
          }
        >
          <div className="system-config-import-summary">
            <strong>将覆盖 {pendingImport.sections.length} 个模块</strong>
            <span>{sectionNames(pendingImport.sections)}</span>
          </div>
          {pendingImport.removedSecretPaths.length > 0 && (
            <div className="system-config-import-note" role="note">
              已移除 {pendingImport.removedSecretPaths.length}{' '}
              个空密钥占位，现有密钥将保持不变。
            </div>
          )}
          {pendingImport.omittedSections.map((item) => (
            <div
              className="system-config-import-note"
              role="note"
              key={item.section}
            >
              {SYSTEM_CONFIG_SECTION_LABELS[item.section] || item.section}：
              {item.reason}
            </div>
          ))}
          <label className="system-config-overwrite-confirm">
            <input
              type="checkbox"
              checked={overwriteConfirmed}
              onChange={(event) => setOverwriteConfirmed(event.target.checked)}
            />
            <span>我已核对预览内容，确认覆盖以上系统配置模块。</span>
          </label>
          <details className="system-config-json-preview" open>
            <summary>JSON 预览（密钥已掩码）</summary>
            <pre>{JSON.stringify(pendingImport.maskedConfig, null, 2)}</pre>
          </details>
        </Modal>
      )}
    </SectionCard>
  );
}
