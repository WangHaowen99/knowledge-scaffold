import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ONTOLOGY_IMPORT_DISCLAIMER,
  canSubmitOntologyImport,
} from './ontologyImportDisclaimer.js';

test('ontology import disclaimer states execution and uploader responsibility boundaries', () => {
  assert.match(ONTOLOGY_IMPORT_DISCLAIMER.body, /严格按照.*本体.*执行/);
  assert.match(ONTOLOGY_IMPORT_DISCLAIMER.body, /来源可信/);
  assert.match(ONTOLOGY_IMPORT_DISCLAIMER.body, /内容正确/);
  assert.match(ONTOLOGY_IMPORT_DISCLAIMER.body, /安全风险/);
});

test('ontology import submission requires explicit acknowledgement', () => {
  assert.equal(canSubmitOntologyImport({ acknowledged: false }), false);
  assert.equal(canSubmitOntologyImport({ acknowledged: true }), true);
});
