import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';

import { provisionShadowWorkspace } from '../src/consumer/client.js';

test('Modeling uses its service credential to provision an owned shadow workspace', async () => {
  let seenAuth = '';
  let seenBody: Record<string, unknown> = {};
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    seenAuth = String(req.headers.authorization ?? '');
    seenBody = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    assert.equal(req.method, 'POST');
    assert.equal(req.url, '/agentic/internal/modeling/shadow-workspaces/ensure');
    res.statusCode = 201;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({
      created: true,
      workspace: { id: 'mw-shadow-pj-demo-123', owner: 'alice' },
    }));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));

  try {
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const result = await provisionShadowWorkspace('pj_demo-123', 'alice', {
      baseUrl: `http://127.0.0.1:${address.port}`,
      serviceToken: 's'.repeat(32),
      managedModelProfile: {
        provider: 'openai_compatible',
        baseUrl: 'https://model.example/v1',
        model: 'qwen3.5-plus',
        apiKey: 'private-key',
        timeout: 600,
        maxTokens: 8192,
        temperature: 0.2,
      },
    });

    assert.equal(seenAuth, `Bearer ${'s'.repeat(32)}`);
    assert.deepEqual(seenBody, {
      project_id: 'pj_demo-123',
      owner: 'alice',
      managed_model_profile: {
        provider: 'openai_compatible',
        base_url: 'https://model.example/v1',
        model: 'qwen3.5-plus',
        api_key: 'private-key',
        timeout: 600,
        max_tokens: 8192,
        temperature: 0.2,
      },
    });
    assert.equal(result.created, true);
    assert.equal(result.workspace.id, 'mw-shadow-pj-demo-123');
  } finally {
    server.close();
  }
});
