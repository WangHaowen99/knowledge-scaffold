import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

import DataSourceConnectionFields from '../frontend/src/DataSourceConnectionFields.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

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
