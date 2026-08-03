import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PACKAGE_DATA_SOURCE_DETAIL, PACKAGE_DATA_SOURCE_HELP } from '../frontend/src/package-data-source.js';

test('package data source: help icon exposes the resource mapping tooltip', async () => {
  const { PackageDataSourceHelp } = await import('../frontend/src/DataSourcePanel.jsx');

  const help = PackageDataSourceHelp();
  const [icon, tooltip] = help.props.children;

  assert.equal(icon.type, 'button');
  assert.equal(icon.props.type, 'button');
  assert.equal(icon.props['aria-label'], '查看数据源映射说明');
  assert.equal(icon.props['aria-describedby'], tooltip.props.id);
  assert.equal(tooltip.props.role, 'tooltip');
  // 正文说清「为什么现在要选」，技术细节降级到后面一行，业务专家看正文即可
  const [helpText, detailNode] = tooltip.props.children;
  assert.equal(helpText, PACKAGE_DATA_SOURCE_HELP);
  assert.match(helpText, /选错需要重新生成/);
  assert.doesNotMatch(helpText, /object-data-mappings|resource|Sheet/);
  assert.match(String(detailNode.props.children), /object-data-mappings/);
  // 映射细节仍然写清楚，只是移到 DETAIL 里，实施同事需要时能查到
  assert.match(PACKAGE_DATA_SOURCE_DETAIL, /mapping\/object-data-mappings\.json/);
  assert.match(PACKAGE_DATA_SOURCE_DETAIL, /NL2SQL\/MindsDB.*来源 Sheet/);
  assert.match(PACKAGE_DATA_SOURCE_DETAIL, /Neo4j.*object key/);
});
