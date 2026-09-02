import assert from 'node:assert/strict';
import test from 'node:test';

import { ontologyRequestPolicy } from './ontologyRequestPolicy.js';


test('ontology request policy enables list only for subscribers and tree only for knowledge', () => {
  assert.deepEqual(ontologyRequestPolicy({ surface: 'chat' }), {
    listEnabled: true,
    treeEnabled: false,
  });
  assert.deepEqual(ontologyRequestPolicy({ surface: 'kb' }), {
    listEnabled: true,
    treeEnabled: true,
  });
  assert.deepEqual(ontologyRequestPolicy({
    surface: 'config',
    configScope: 'workspace',
    configSection: 'mcp',
  }), {
    listEnabled: true,
    treeEnabled: false,
  });
  for (const surface of ['config', 'users', 'solver']) {
    assert.deepEqual(ontologyRequestPolicy({ surface }), {
      listEnabled: false,
      treeEnabled: false,
    });
  }
});
