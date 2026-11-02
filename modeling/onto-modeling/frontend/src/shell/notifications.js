// 用户流 notification（EU-01）合进通知列表：同组未读的旧通知被新的这条替换时，事件带 replaces（被替换的编号），
// 先从列表里拿掉它们、未读数减去其中没读的，再把新的放到最前面
export function applyNotification(state, data) {
  const gone = new Set(data?.replaces || []);
  const kept = (state.items || []).filter((n) => n.id !== data.id && !gone.has(n.id));
  const dropped = (state.items || []).filter((n) => gone.has(n.id) && !n.read).length;
  const { replaces: _ignored, ...item } = data;
  return { items: [item, ...kept], unread: Math.max(0, (state.unread || 0) - dropped + (item.read ? 0 : 1)) };
}
