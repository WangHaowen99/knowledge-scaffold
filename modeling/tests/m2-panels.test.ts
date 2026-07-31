import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PACKAGE_DATA_SOURCE_HELP } from '../frontend/src/package-data-source.js';

test('package data source: help icon exposes the resource mapping tooltip', async () => {
  const { PackageDataSourceHelp } = await import('../frontend/src/DataSourcePanel.jsx');

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
