// 操作向导的会话级“已看过”标记：每次新登录（新会话）都会重新弹出。
const SEEN_KEY = 'mw_wizard_seen';

export function wizardSeen() {
  try {
    return !!sessionStorage.getItem(SEEN_KEY);
  } catch {
    return true;
  }
}

export function markWizardSeen() {
  try {
    sessionStorage.setItem(SEEN_KEY, '1');
  } catch {
    // Storage can be unavailable in privacy-restricted browser contexts.
  }
}
