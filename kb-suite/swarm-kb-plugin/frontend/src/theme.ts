// Mirror JiuwenSwarm's design tokens into this iframe.
//
// The plugin iframe is same-origin with the Swarm page and has no sandbox,
// so we can copy every CSS custom property from the parent's root element
// and follow theme switches (data-theme / data-color-mode on <html>).

const MIRRORED_ATTRS = ['data-theme', 'data-color-mode', 'class'];

function parentRoot(): HTMLElement | null {
  try {
    if (window.parent === window) return null;
    return window.parent.document.documentElement;
  } catch {
    return null;
  }
}

function copyTokens(source: HTMLElement): void {
  const target = document.documentElement;
  for (const attr of MIRRORED_ATTRS) {
    const value = source.getAttribute(attr);
    if (value === null) target.removeAttribute(attr);
    else target.setAttribute(attr, value);
  }
  const style = window.parent.getComputedStyle(source);
  for (let i = 0; i < style.length; i += 1) {
    const name = style[i];
    if (name.startsWith('--')) target.style.setProperty(name, style.getPropertyValue(name));
  }
}

export function syncThemeWithHost(): void {
  const root = parentRoot();
  if (!root) return;
  copyTokens(root);
  const observer = new MutationObserver(() => copyTokens(root));
  observer.observe(root, { attributes: true, attributeFilter: MIRRORED_ATTRS });
}
