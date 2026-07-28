import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PACKAGE_DATA_SOURCE_HELP } from '../frontend/src/package-data-source.js';

test('interview: each batch gets an independent answer form', async () => {
  const { PendingInterviewBatch } = await import('../frontend/src/M2Panels.jsx');
  const sharedQuestion = { id: 'q1', q: 'Question', kind: 'single', topic: 'Topic', options: [] };
  const onSubmit = () => {};

  const first = PendingInterviewBatch({
    batch: { id: 'batch-1', questions: [sharedQuestion] },
    displayOrdinal: 0,
    busy: false,
    onSubmit,
  });
  const second = PendingInterviewBatch({
    batch: { id: 'batch-2', questions: [sharedQuestion] },
    displayOrdinal: 1,
    busy: false,
    onSubmit,
  });

  assert.equal(first.key, 'batch-1');
  assert.equal(second.key, 'batch-2');
  assert.notEqual(first.key, second.key);
  assert.equal(first.props.displayOrdinal, 0);
  assert.equal(second.props.displayOrdinal, 1);
});

test('package data source: help icon exposes the resource mapping tooltip', async () => {
  const { PackageDataSourceHelp } = await import('../frontend/src/M2Panels.jsx');

  const help = PackageDataSourceHelp();
  const [icon, tooltip] = help.props.children;

  assert.equal(icon.type, 'button');
  assert.equal(icon.props.type, 'button');
  assert.equal(icon.props['aria-label'], '查看数据源映射说明');
  assert.equal(icon.props['aria-describedby'], tooltip.props.id);
  assert.equal(tooltip.props.role, 'tooltip');
  assert.equal(tooltip.props.children, PACKAGE_DATA_SOURCE_HELP);
  assert.match(PACKAGE_DATA_SOURCE_HELP, /mapping\/object-data-mappings\.json/);
  assert.match(PACKAGE_DATA_SOURCE_HELP, /NL2SQL\/MindsDB.*来源 Sheet/);
  assert.match(PACKAGE_DATA_SOURCE_HELP, /Neo4j.*object key/);
});
