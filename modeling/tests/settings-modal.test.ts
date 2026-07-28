import { test } from 'node:test';
import assert from 'node:assert/strict';

test('settings modal: backdrop does not close the modal', async () => {
  const { SettingsModalOverlay } = await import('../frontend/src/SettingsModal.jsx');
  const child = { type: 'modal-content' };

  const overlay = SettingsModalOverlay({ children: child });

  assert.equal(overlay.props.className, 'modal-ov');
  assert.equal(overlay.props.onClick, undefined);
  assert.equal(overlay.props.children, child);
});
