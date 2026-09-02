import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import test from 'node:test';

import DataSourceConnectionFields from '../frontend/src/DataSourceConnectionFields.jsx';
import { PackageDataSourceHelp } from '../frontend/src/DataSourcePanel.jsx';
import EvaluationDatabaseUrlConfirmModal from '../frontend/src/EvaluationDatabaseUrlConfirmModal.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');
const stylesSource = readFileSync(new URL('../frontend/src/styles.css', import.meta.url), 'utf8');

test('数据源字段提供稳定的标签关联和可访问名称', () => {
  const html = renderToStaticMarkup(React.createElement(DataSourceConnectionFields, {
    form: {
      provider: 'nl2sql_http', url: '', scene: '', token: '', evaluation_workspace_id: '',
    },
    busy: false,
    syncingOntologyId: false,
    creatingDatabase: false,
    onFieldChange: () => () => {},
    onProviderChange: () => {},
    onCreateProjectDatabase: () => {},
    onSyncOntologyId: () => {},
  }));

  assert.match(html, /aria-label="数据服务 URL"/);
  assert.match(html, /<label for="[^"]+">数据服务 URL<\/label>/);
  assert.match(html, /<input[^>]+id="[^"]+"/);
  assert.match(html, /aria-label="评测工作空间 ID"/);
  assert.match(html, /<label for="[^"]+">Provider<\/label>/);
});

test('数据源 Provider 下拉可按知识包数据源类型收窄', () => {
  const html = renderToStaticMarkup(React.createElement(DataSourceConnectionFields, {
    form: {
      provider: 'neo4j', uri: '', database: '', username: '', password: '',
      workspace_property: '', workspace_value: '', ontology_property: '', ontology_value: '',
    },
    busy: false,
    syncingOntologyId: false,
    creatingDatabase: false,
    providerOptions: [{ value: 'neo4j', label: 'Neo4j' }],
    onFieldChange: () => () => {},
    onProviderChange: () => {},
    onCreateProjectDatabase: () => {},
    onSyncOntologyId: () => {},
  }));

  assert.match(html, />Neo4j<\/option>/);
  assert.doesNotMatch(html, /NL2SQL Template/);
  assert.doesNotMatch(html, /NL2SQL HTTP/);
});

test('数据源问号说明默认隐藏，并支持鼠标悬停与键盘聚焦展示', () => {
  const html = renderToStaticMarkup(React.createElement(PackageDataSourceHelp));

  assert.match(html, /class="pkg-source-help-icon"/);
  assert.match(html, /class="pkg-source-tooltip" role="tooltip"/);
  assert.match(stylesSource, /\.mwr \.pkg-source-tooltip\s*\{[^}]*visibility:\s*hidden/s);
  assert.match(stylesSource, /\.mwr \.pkg-source-help:hover \.pkg-source-tooltip/);
  assert.match(stylesSource, /\.mwr \.pkg-source-help:focus-within \.pkg-source-tooltip/);
});

test('Neo4j Ontology 对应值与同步知识包按钮使用专用同排布局', () => {
  const html = renderToStaticMarkup(React.createElement(DataSourceConnectionFields, {
    form: {
      provider: 'neo4j', uri: '', database: '', username: '', password: '',
      workspace_property: '', workspace_value: '', ontology_property: '', ontology_value: '',
    },
    busy: false,
    syncingOntologyId: false,
    creatingDatabase: false,
    onFieldChange: () => () => {},
    onProviderChange: () => {},
    onCreateProjectDatabase: () => {},
    onSyncOntologyId: () => {},
  }));

  assert.match(html, /class="field-control-row field-control-row--ontology"/);
  assert.match(html, />同步知识包id<\/button>/);
  assert.match(stylesSource, /\.mwr \.field-control-row--ontology\s*\{[^}]*grid-template-columns:/s);
});

test('NL2SQL HTTP 和 Template 的 Scene 输入框与项目数据库按钮保持同排', () => {
  for (const provider of ['nl2sql_http', 'nl2sql_template']) {
    const html = renderToStaticMarkup(React.createElement(DataSourceConnectionFields, {
      form: {
        provider, url: '', scene: '', token: '', evaluation_workspace_id: '',
      },
      busy: false,
      syncingOntologyId: false,
      creatingDatabase: false,
      onFieldChange: () => () => {},
      onProviderChange: () => {},
      onCreateProjectDatabase: () => {},
      onSyncOntologyId: () => {},
    }));

    assert.match(html, /class="field-control-row field-control-row--scene"/);
    assert.match(html, />使用项目专用数据库<\/button>/);
  }
  assert.match(stylesSource, /\.mwr \.field-control-row--scene\s*\{[^}]*grid-template-columns:/s);
});

test('项目数据库确认弹窗为连接选项、长 URL 和窄屏提供稳定布局', () => {
  const html = renderToStaticMarkup(React.createElement(EvaluationDatabaseUrlConfirmModal, {
    title: '使用项目专用数据库',
    message: '确认数据库连接',
    databaseName: 'mv_shadow_project_123',
    configured: false,
    databaseUrlPreview: '',
    confirmLabel: '确认并使用',
    busy: false,
    error: '',
    onCancel: () => {},
    onConfirm: () => {},
  }));

  assert.match(html, /class="modal evaluation-db-url-modal"/);
  assert.match(html, /class="evaluation-db-url-option selected"/);
  assert.match(html, /class="field evaluation-db-custom-url"/);
  assert.match(stylesSource, /\.mwr \.evaluation-db-url-modal\s*\{[^}]*width:/s);
  assert.match(stylesSource, /\.mwr \.evaluation-db-url-option\s*\{[^}]*display:\s*grid/s);
  assert.match(stylesSource, /\.mwr \.evaluation-db-url-option code\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(stylesSource, /\.mwr \.project-db-fixed-name\s*\{[^}]*padding:/s);
  assert.match(stylesSource, /\.mwr \.project-db-create-hint\s*\{[^}]*line-height:/s);
});
