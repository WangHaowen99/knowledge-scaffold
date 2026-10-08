export async function copyMcpText(
  value,
  {
    navigatorRef = globalThis.navigator,
    documentRef = globalThis.document,
  } = {},
) {
  const text = String(value ?? '');
  if (!text) return { ok: false, error: '没有可复制的内容' };

  if (navigatorRef?.clipboard?.writeText) {
    try {
      await navigatorRef.clipboard.writeText(text);
      return { ok: true, error: '' };
    } catch {
      // Clipboard API may be present but blocked outside a secure context.
    }
  }

  if (!documentRef?.createElement || !documentRef?.body?.appendChild) {
    return { ok: false, error: '浏览器不支持自动复制，请手工选择内容' };
  }

  let textarea;
  try {
    textarea = documentRef.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.inset = '-9999px auto auto -9999px';
    documentRef.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    return documentRef.execCommand?.('copy') === true
      ? { ok: true, error: '' }
      : { ok: false, error: '复制失败，请手工选择内容' };
  } catch {
    return { ok: false, error: '复制失败，请手工选择内容' };
  } finally {
    if (textarea?.parentNode) textarea.parentNode.removeChild(textarea);
  }
}
