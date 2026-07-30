import { useCallback, useEffect, useRef, useState } from 'react';

export const MARKDOWN_UPLOAD_CANCEL_NOTICE = '已取消选择 Markdown 文件';
export const ZIP_DOWNLOAD_NOTICE_PREFIX = '已发起 ZIP 下载：';

export function createNativeFilePickerController({ onSelect, onCancel } = {}) {
  let pending = false;
  let selectHandler = onSelect;
  let cancelHandler = onCancel;

  const clearPending = () => {
    pending = false;
  };

  return {
    setHandlers(next = {}) {
      selectHandler = next.onSelect;
      cancelHandler = next.onCancel;
    },
    begin() {
      pending = true;
    },
    handleChange(file) {
      clearPending();
      if (file) {
        selectHandler?.(file);
        return 'selected';
      }
      cancelHandler?.('empty');
      return 'cancelled';
    },
    handleNativeCancel() {
      if (!pending) return false;
      clearPending();
      cancelHandler?.('native');
      return true;
    },
    handleWindowFocus(readFiles) {
      if (!pending) return false;
      const files = readFiles?.() || [];
      if (files.length > 0) return false;
      clearPending();
      cancelHandler?.('fallback');
      return true;
    },
    isPending() {
      return pending;
    },
  };
}

export function useNativeFilePicker({ onSelect, onCancel } = {}) {
  const inputRef = useRef(null);
  const settleTimerRef = useRef(null);
  const [controller] = useState(() => createNativeFilePickerController({ onSelect, onCancel }));

  useEffect(() => {
    controller.setHandlers({ onSelect, onCancel });
  }, [controller, onCancel, onSelect]);

  const open = useCallback(() => {
    if (!inputRef.current) return;
    controller.begin();
    inputRef.current.click();
  }, [controller]);

  const handleChange = useCallback((event) => {
    const file = event.target.files?.[0] || null;
    controller.handleChange(file);
    event.target.value = '';
  }, [controller]);

  const handleCancel = useCallback(() => {
    if (inputRef.current) inputRef.current.value = '';
    controller.handleNativeCancel();
  }, [controller]);

  const setInputRef = useCallback((node) => {
    if (inputRef.current === node) return;
    inputRef.current?.removeEventListener('cancel', handleCancel);
    inputRef.current = node;
    inputRef.current?.addEventListener('cancel', handleCancel);
  }, [handleCancel]);

  useEffect(() => {
    const settle = () => {
      if (settleTimerRef.current) window.clearTimeout(settleTimerRef.current);
      settleTimerRef.current = window.setTimeout(() => {
        settleTimerRef.current = null;
        const cancelled = controller.handleWindowFocus(() => (
          Array.from(inputRef.current?.files || [])
        ));
        if (cancelled && inputRef.current) inputRef.current.value = '';
      }, 50);
    };
    window.addEventListener('focus', settle);
    return () => {
      window.removeEventListener('focus', settle);
      if (settleTimerRef.current) window.clearTimeout(settleTimerRef.current);
      settleTimerRef.current = null;
    };
  }, [controller]);

  return { inputRef: setInputRef, open, handleChange };
}

export function resolveZipDownloadFileName(pkgId, viewVersion, disposition = '') {
  const encodedName = contentDispositionParam(disposition, 'filename*');
  if (encodedName) {
    return decodeHeaderFileName(encodedName.replace(/^UTF-8'[^']*'/i, ''));
  }
  const fallbackName = contentDispositionParam(disposition, 'filename');
  if (fallbackName) {
    return decodeHeaderFileName(fallbackName);
  }
  return `${pkgId}${viewVersion ? `-${viewVersion}` : ''}.zip`;
}

export function describeZipDownload(fileName) {
  return `${ZIP_DOWNLOAD_NOTICE_PREFIX}${fileName}`;
}

export function resolveKnowledgeWorkbenchNotices(fileNotice = '', operationNotice = '') {
  if (fileNotice === MARKDOWN_UPLOAD_CANCEL_NOTICE) {
    return { inlineMessage: '', toastMessage: fileNotice, toastTone: 'info' };
  }
  if (operationNotice.startsWith(ZIP_DOWNLOAD_NOTICE_PREFIX)) {
    return { inlineMessage: '', toastMessage: operationNotice, toastTone: 'success' };
  }
  return {
    inlineMessage: fileNotice || operationNotice,
    toastMessage: '',
    toastTone: 'info',
  };
}

export function startBlobDownload(blob, fileName, options = {}) {
  const doc = options.doc || globalThis.document;
  const urlApi = options.urlApi || globalThis.URL;
  const schedule = options.schedule || globalThis.setTimeout.bind(globalThis);
  if (!doc?.body || typeof doc.createElement !== 'function' || typeof urlApi?.createObjectURL !== 'function') {
    return { fileName, objectUrl: '' };
  }

  const objectUrl = urlApi.createObjectURL(blob);
  const link = doc.createElement('a');
  link.href = objectUrl;
  link.download = fileName;
  link.rel = 'noopener';

  try {
    doc.body.appendChild(link);
    link.click();
  } finally {
    if (link.parentNode === doc.body) doc.body.removeChild(link);
    schedule(() => {
      try {
        urlApi.revokeObjectURL(objectUrl);
      } catch {
        // ignore revoke failures
      }
    }, 0);
  }

  return { fileName, objectUrl };
}

function contentDispositionParam(disposition, key) {
  const pattern = new RegExp(`(?:^|;)\\s*${key.replace('*', '\\*')}=(?:"([^"]*)"|([^;]*))`, 'i');
  const match = pattern.exec(disposition || '');
  return (match?.[1] ?? match?.[2] ?? '').trim();
}

function decodeHeaderFileName(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
