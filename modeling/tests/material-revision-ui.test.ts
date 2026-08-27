import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

import { IngestView } from '../frontend/src/PreparationPanels.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('建模资料列表展示当前修订并提供历史入口', () => {
  const html = renderToStaticMarkup(React.createElement(IngestView, {
    project: { id: 'project-material' },
    documents: [{
      id: 'doc-rev2', sourceKey: 'material-source', revisionNo: 2,
      filename: 'policy.md', parseStatus: 'parsed', kind: 'markdown', createdAt: new Date().toISOString(),
    }],
    onChanged: () => {},
  }));

  assert.match(html, /修订 2/);
  assert.match(html, />历史</);
  assert.match(html, />下载</);
  assert.match(html, /title="下载 policy.md"/);
  assert.match(html, /已解析/);
});
