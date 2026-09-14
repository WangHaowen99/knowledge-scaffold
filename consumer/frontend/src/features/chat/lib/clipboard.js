export async function writeClipboardText(text, options = {}) {
  const value = String(text ?? '');
  if (!value) {
    return { ok: false, method: '', error: '没有可复制的内容' };
  }

  const navigatorRef = options.navigatorRef ?? globalThis.navigator;
  const documentRef = options.documentRef ?? globalThis.document;
  if (navigatorRef?.clipboard?.writeText) {
    try {
      await navigatorRef.clipboard.writeText(value);
      return { ok: true, method: 'clipboard', error: '' };
    } catch {
      // Some browsers expose Clipboard API but deny it outside a secure or
      // user-initiated context. Continue with the selection fallback.
    }
  }

  if (!documentRef?.createElement || !documentRef?.body?.appendChild) {
    return { ok: false, method: '', error: '浏览器不支持复制，请手动选择内容' };
  }

  let textarea = null;
  try {
    textarea = documentRef.createElement('textarea');
    textarea.value = value;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.inset = '-9999px auto auto -9999px';
    documentRef.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    const copied = documentRef.execCommand?.('copy') === true;
    return copied ?
      { ok: true, method: 'selection', error: '' } :
      { ok: false, method: 'selection', error: '复制失败，请手动选择内容' };
  } catch {
    return {
      ok: false,
      method: 'selection',
      error: '复制失败，请手动选择内容',
    };
  } finally {
    if (textarea?.parentNode) {
      textarea.parentNode.removeChild(textarea);
    }
  }
}
