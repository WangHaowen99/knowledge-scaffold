import { test } from 'node:test';
import assert from 'node:assert/strict';

const project = {
  id: 'pj-delete',
  name: 'Delete project',
  background: 'Background',
  domain: 'Domain',
  stage: 'analyze',
  updatedAt: '2026-07-28T00:00:00.000Z',
};

test('project card: top-right close action does not open the project', async () => {
  const { ProjectCard } = await import('../frontend/src/ProjectList.jsx');
  let opened = 0;
  let requested = 0;
  let stopped = 0;
  const card = ProjectCard({
    project,
    onOpen: () => { opened += 1; },
    onRequestDelete: () => { requested += 1; },
  });
  const deleteButton = card.props.children.find((child: any) => child?.props?.className === 'pj-delete');

  assert.equal(deleteButton.props.children, '×');
  assert.equal(deleteButton.props.title, '删除项目');
  deleteButton.props.onClick({ stopPropagation: () => { stopped += 1; } });

  assert.equal(stopped, 1);
  assert.equal(requested, 1);
  assert.equal(opened, 0);
});

test('project deletion dialog: only confirmation triggers deletion', async () => {
  const { ProjectDeleteDialog } = await import('../frontend/src/ProjectList.jsx');
  let cancelled = 0;
  let confirmed = 0;
  const overlay = ProjectDeleteDialog({
    project,
    busy: false,
    error: '',
    onCancel: () => { cancelled += 1; },
    onConfirm: () => { confirmed += 1; },
  });
  const dialog = overlay.props.children;
  const footer = dialog.props.children.find((child: any) => child?.props?.className === 'foot');
  const [cancelButton, confirmButton] = footer.props.children;

  assert.equal(dialog.props.role, 'dialog');
  assert.equal(dialog.props['aria-modal'], 'true');
  assert.equal(cancelButton.props.children, '取消');
  assert.equal(confirmButton.props.children, '确认删除');
  cancelButton.props.onClick();
  assert.equal(cancelled, 1);
  assert.equal(confirmed, 0);
  confirmButton.props.onClick();
  assert.equal(confirmed, 1);

  const busyOverlay = ProjectDeleteDialog({
    project,
    busy: true,
    error: '',
    onCancel: () => {},
    onConfirm: () => {},
  });
  const busyFooter = busyOverlay.props.children.props.children
    .find((child: any) => child?.props?.className === 'foot');
  assert.equal(busyFooter.props.children[0].props.disabled, true);
  assert.equal(busyFooter.props.children[1].props.disabled, true);
  assert.equal(busyFooter.props.children[1].props.children, '删除中…');
});
